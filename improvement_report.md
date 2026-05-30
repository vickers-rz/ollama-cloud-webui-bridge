# ollama-cloud-webui-bridge 改进分析

## 总览

这个项目整体设计简洁、目标明确，是一个将 Ollama Cloud 模型暴露给 Open WebUI 的代理桥接层。核心功能运作良好，但在**可靠性、安全性、可观测性**和**功能完整性**上有明显的改进空间。

---

## 🔴 优先级高 — 可靠性问题

### 1. 忙等循环占满线程（`chrome_ai_bridge.py` L221–252）

**问题：** 主线程在等待 Chrome worker 返回结果时，用 `time.sleep(0.1)` 轮询，每个请求会在一个线程里占用最多 `TASK_TIMEOUT_SECONDS`（默认 180 秒）。  
`ThreadingHTTPServer` 每个连接一个线程，高并发下会耗尽线程资源。

```python
# 现在：忙等
while time.time() < deadline:
    result = results.pop(task_id, None)
    if result:
        ...
    time.sleep(0.1)
```

**建议：** 用 `threading.Event` 代替轮询。

```python
# 改进后
pending: dict[str, threading.Event] = {}
results: dict[str, dict] = {}

# 提交任务时
event = threading.Event()
pending[task_id] = event
tasks.put({"id": task_id, "prompt": ...})
fired = event.wait(timeout=TASK_TIMEOUT_SECONDS)

# worker 返回结果时
results[payload["id"]] = payload
ev = pending.pop(payload["id"], None)
if ev:
    ev.set()
```

### 2. `results` 字典无限增长

**问题：** `results` 是进程级全局字典，任务结果写入后永不清理（即使超时也不删）。长时间运行会泄漏内存。

**建议：** 在超时路径中也 `pop` 对应的 key，并在 worker 写入时使用 `pending` event 机制（见上条）。

```python
# 超时路径需补充清理
results.pop(task_id, None)
pending.pop(task_id, None)
self._send({"error": "timeout"}, status=504)
```

### 3. `_proxy` 不处理网络连接异常

**问题：** `_proxy()` 只捕获了 `urllib.error.HTTPError`，但 `urllib.error.URLError`（连接拒绝/DNS 失败/超时）会向上抛出，导致 500 或连接直接断开，Open WebUI 会看到无意义的错误。

```python
# 现在只处理 HTTP 错误
except urllib.error.HTTPError as err:
    ...
# URLError / timeout 未处理 ↑
```

**建议：**

```python
except urllib.error.HTTPError as err:
    data = err.read()
    self._send_json({"error": str(err)}, status=err.code)
except urllib.error.URLError as err:
    self._send_json({"error": f"Upstream unreachable: {err.reason}"}, status=502)
except TimeoutError:
    self._send_json({"error": "Upstream timeout"}, status=504)
```

---

## 🟠 优先级中 — 功能缺失

### 4. 不支持流式输出（Streaming）

**问题：** `chrome_ai_bridge.py` 直接拒绝 streaming 请求（`status=400`），`ollama_cloud_proxy.py` 在代理 Ollama Cloud 时会把整个响应体缓冲再一次性返回，没有 chunked 转发。

这意味着：
- Open WebUI 对 `chrome-gemini-nano` 没有打字机效果
- 极长响应时浏览器会长时间白屏

**建议（chrome_ai_bridge 侧）：**  
利用 Chrome `LanguageModel` 的 `promptStreaming()` API 逐块返回 SSE 事件，配合 `Transfer-Encoding: chunked`。

**建议（ollama_cloud_proxy 侧）：**  
对 Ollama Cloud 的流式响应应流式转发，避免缓冲：

```python
# 替换 _proxy/_proxy_payload 中的 data = resp.read()
with urllib.request.urlopen(req, timeout=600) as resp:
    self.send_response(resp.status)
    for key, value in resp.headers.items():
        if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
            self.send_header(key, value)
    self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()
    while chunk := resp.read(4096):
        self.wfile.write(f"{len(chunk):x}\r\n".encode())
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
    self.wfile.write(b"0\r\n\r\n")
```

### 5. `modified_at` 日期硬编码

**问题：** `model_payload()` 里 `"modified_at": "2026-05-30T00:00:00Z"` 是硬编码。

**建议：** 用启动时间或 `datetime.utcnow().isoformat() + "Z"` 动态生成。

```python
from datetime import datetime, timezone

_STARTUP_TIME = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def model_payload(model_id):
    ...
    "modified_at": _STARTUP_TIME,
```

### 6. Chrome worker 轮询用 GET 传结果不安全

**问题：** 结果通过 GET query string 传回（`/worker/result?id=...&content=...`），大响应可能超出 URL 长度限制（浏览器通常限制 2000–8000 字节）。

**建议：** 改为 POST JSON：

```javascript
// worker 侧
await fetch("/worker/result", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({id: task.id, content}),
});
```

---

## 🟡 优先级中低 — 可观测性

### 7. 日志完全静默

**问题：** 两个文件都重写了 `log_message` 直接 `return`，完全没有任何日志。出错时只能靠 `curl` 手动排查。

**建议：** 改用 `logging` 模块，只记录关键事件：

```python
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

def log_message(self, fmt, *args):
    logger.info("%s %s", self.path, fmt % args)
```

### 8. `/health` 端点信息不够丰富

**问题：** 当前 `/health` 只返回 `worker_connected`，不区分"Python 进程正常但 Chrome tab 未连接"和"任务队列堆积"。

**建议：**

```python
{
    "ok": True,
    "model": MODEL_ID,
    "worker_connected": ...,
    "queue_depth": tasks.qsize(),
    "pending_tasks": len(pending),
    "uptime_seconds": int(time.time() - START_TIME),
}
```

---

## 🔵 优先级低 — 代码质量

### 9. `docker-compose.yml` 两个服务用同一镜像

**问题：**

```yaml
ollama-cloud-proxy:
  image: ghcr.io/open-webui/open-webui:main   # ← open-webui 的 1.5GB+ 镜像
```

proxy 本身只是一个 Python HTTP 服务器，用 `open-webui` 镜像运行是因为镜像里有 Python，但这意味着每次启动都要拉取一个巨大的镜像，且 proxy 只用到了其中的 `python` 可执行文件。

**建议：** 给 proxy 写一个单独的轻量 Dockerfile：

```dockerfile
FROM python:3.12-slim
COPY work/ollama_cloud_proxy.py /proxy/ollama_cloud_proxy.py
CMD ["python", "/proxy/ollama_cloud_proxy.py"]
```

这样镜像从 ~1.5 GB 降到 ~60 MB，启动更快，攻击面也更小。

### 10. `chrome_ai_bridge.py` 中 `messages_to_prompt` 丢失 system prompt

**问题：** 当前实现把所有消息拼成 `role: content` 字符串发给 Chrome `LanguageModel.prompt()`，这样 `system` 角色消息会作为普通文本插入，行为不符合预期。

**建议：** 利用 `LanguageModel.create({ systemPrompt })` 和 `session.prompt()` 的分离：

```javascript
async function runMessages(messages) {
  const system = messages.find(m => m.role === "system");
  const userMessages = messages.filter(m => m.role !== "system");
  const session = await LanguageModel.create({
    systemPrompt: system?.content ?? "",
  });
  try {
    const prompt = userMessages.map(m => `${m.role}: ${m.content}`).join("\n") + "\nassistant:";
    return await session.prompt(prompt);
  } finally {
    session.destroy?.();
  }
}
```

### 11. 缺少 `requirements.txt` / `pyproject.toml`

**问题：** `chrome_ai_bridge.py` 只用标准库，但没有任何 Python 环境声明。未来添加依赖时容易出问题。

**建议：** 添加一个 `pyproject.toml`（或至少 `requirements.txt`）声明 Python 版本要求（`>=3.11`，需要 `walrus operator` 和 `match`）。

---

## 改进优先级汇总

| # | 问题 | 优先级 | 影响 |
|---|------|--------|------|
| 1 | 忙等轮询耗尽线程 | 🔴 高 | 高并发崩溃 |
| 2 | results 字典内存泄漏 | 🔴 高 | 长期运行 OOM |
| 3 | URLError 未捕获 | 🔴 高 | 上游不可用时 crash |
| 4 | 不支持流式输出 | 🟠 中 | 用户体验差 |
| 5 | modified_at 硬编码 | 🟠 中 | 轻微 |
| 6 | GET 传大结果可能截断 | 🟠 中 | 长回复时丢数据 |
| 7 | 完全无日志 | 🟡 中低 | 排障困难 |
| 8 | /health 信息单薄 | 🟡 中低 | 运维体验 |
| 9 | proxy 用 open-webui 镜像 | 🔵 低 | 镜像太大 |
| 10 | system prompt 处理错误 | 🔵 低 | 模型理解偏差 |
| 11 | 缺少 Python 版本声明 | 🔵 低 | 可维护性 |
