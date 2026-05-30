# ollama-cloud-webui-bridge 改进分析

## 总览

这个项目整体设计简洁、目标明确，是一个将 Ollama Cloud 模型暴露给 Open WebUI 的代理桥接层。

**状态说明：**
- ✅ 已实现
- ⏳ 待实现

---

## 🔴 优先级高 — 可靠性

### 1. ✅ 忙等循环占满线程（`chrome_ai_bridge.py`）

~~`time.sleep(0.1)` 轮询，高并发下耗尽线程。~~

**已修复：** 改用 `threading.Event`，线程在 `event.wait(timeout=...)` 上阻塞，不占 CPU。

```python
# 当前实现（_wait_result）
event = threading.Event()
with _state_lock:
    _pending[task_id] = event
fired = event.wait(timeout=TASK_TIMEOUT_SECONDS)
```

---

### 2. ✅ `results` 字典内存泄漏

~~超时任务的结果永不清理，长时间运行会 OOM。~~

**已修复：** `_wait_result()` 在成功和超时两条路径都执行 `_results.pop(task_id, None)` 和 `_pending.pop(task_id, None)`，保证状态不泄漏。

---

### 3. ✅ `_proxy` 不处理网络连接异常

~~`_proxy()` 只捕获 `HTTPError`，`URLError`（连接拒绝/DNS/超时）会 crash。~~

**已修复：** `_proxy()`、`_proxy_payload()`、`_post_json()` 全部补上 `except urllib.error.URLError`，返回 502 而非崩溃。

```python
except urllib.error.URLError as exc:
    log.error("_proxy %s -> URLError: %s", self.path, exc)
    self._send_json({"error": f"upstream unavailable: {exc}"}, status=502)
```

---

## 🟠 优先级中 — 功能缺失

### 4. ⏳ 不支持流式输出（Streaming）

**现状：** `chrome_ai_bridge.py` 直接拒绝 streaming（返回 400）；`ollama_cloud_proxy.py` 全量缓冲再返回，Open WebUI 没有打字机效果。

**待做：**
- proxy 侧：改用 chunked 转发，去掉 `data = resp.read()` 全量缓冲
- chrome 侧：接入 `LanguageModel.promptStreaming()`，转换为 SSE 或 Ollama NDJSON 格式

```python
# proxy 流式转发示意
with urllib.request.urlopen(req, timeout=600) as resp:
    self.send_response(resp.status)
    self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()
    while chunk := resp.read(4096):
        self.wfile.write(f"{len(chunk):x}\r\n".encode())
        self.wfile.write(chunk)
        self.wfile.write(b"\r\n")
    self.wfile.write(b"0\r\n\r\n")
```

---

### 5. ✅ `modified_at` 日期硬编码

~~`model_payload()` 里写死了 `"2026-05-30T00:00:00Z"`。~~

**已修复：** 改为进程启动时动态生成：

```python
_STARTED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

---

### 6. ✅ Chrome worker 用 GET 传结果，URL 长度受限

~~`/worker/result?id=...&content=...` 大响应会超出 URL 长度限制（2000–8000 字节）。~~

**已修复：** JS worker 改为 POST JSON 回传：

```javascript
await postJson("/worker/result", {id: task.id, content});
```

服务端 `POST /worker/result` 接收并调用 `_put_result()` 触发 Event。旧 GET 路径保留兼容但打印 warning。

---

## 🟡 优先级中低 — 可观测性

### 7. ✅ 日志完全静默

~~两个文件都把 `log_message` 覆盖为空。~~

**已修复：** 两个文件均接入 `logging` 模块：

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("proxy")
```

关键路径（启动参数、模型列表请求、chat 请求分发、上游错误）均有记录。

---

### 8. ✅ `/health` 端点信息单薄

~~只返回 `worker_connected`，无法区分队列堆积和 Chrome 断连。~~

**已修复：** 现在返回：

```json
{
  "ok": true,
  "model": "chrome-gemini-nano",
  "worker_connected": true,
  "worker_last_seen_seconds_ago": 2.1,
  "queue_depth": 0,
  "pending_tasks": 0,
  "uptime_seconds": 120
}
```

---

## 🔵 优先级低 — 代码质量

### 9. ✅ proxy 容器使用 open-webui 大镜像（~1.5 GB）

~~proxy 只运行一个 Python 脚本，却拉 open-webui 全量镜像。~~

**已修复：** 新增 `Dockerfile.proxy`，改用 `python:3.12-slim`（~60 MB）：

```dockerfile
FROM python:3.12-slim
WORKDIR /proxy
COPY work/ollama_cloud_proxy.py .
EXPOSE 11434
CMD ["python", "ollama_cloud_proxy.py"]
```

`docker-compose.yml` 改为本地 build，镜像名 `ollama-cloud-proxy:local`。

---

### 10. ⏳ `chrome_ai_bridge.py` 中 system prompt 处理不当

**现状：** `messages_to_prompt()` 把 `system` 角色消息当普通文本拼入 prompt，未利用 `LanguageModel.create({ systemPrompt })` 的独立传参。

**待做：**

```javascript
async function runMessages(messages) {
  const system = messages.find(m => m.role === "system");
  const rest = messages.filter(m => m.role !== "system");
  const session = await LanguageModel.create({
    systemPrompt: system?.content ?? "",
  });
  try {
    return await session.prompt(
      rest.map(m => `${m.role}: ${m.content}`).join("\n") + "\nassistant:"
    );
  } finally {
    session.destroy?.();
  }
}
```

---

### 11. ⏳ 缺少 Python 版本声明

**现状：** 无 `pyproject.toml` 或 `requirements.txt`，未来添加依赖时可能出问题。

**待做：** 添加 `pyproject.toml` 声明 `requires-python = ">=3.11"`（walrus operator `chunk := ...` 需要 3.8+，type hint `dict[str, ...]` 需要 3.9+）。

---

## 改进优先级汇总

| # | 问题 | 优先级 | 状态 |
|---|------|--------|------|
| 1 | 忙等轮询耗尽线程 | 🔴 高 | ✅ 已完成 |
| 2 | results 字典内存泄漏 | 🔴 高 | ✅ 已完成 |
| 3 | URLError 未捕获导致 crash | 🔴 高 | ✅ 已完成 |
| 4 | 不支持流式输出 | 🟠 中 | ⏳ 待实现 |
| 5 | modified_at 硬编码 | 🟠 中 | ✅ 已完成 |
| 6 | GET 传大结果可能截断 | 🟠 中 | ✅ 已完成 |
| 7 | 完全无日志 | 🟡 中低 | ✅ 已完成 |
| 8 | /health 信息单薄 | 🟡 中低 | ✅ 已完成 |
| 9 | proxy 用 open-webui 大镜像 | 🔵 低 | ✅ 已完成 |
| 10 | system prompt 处理不当 | 🔵 低 | ⏳ 待实现 |
| 11 | 缺少 Python 版本声明 | 🔵 低 | ⏳ 待实现 |
| — | 本地模型自动合并（新增） | — | ✅ 已完成 |
