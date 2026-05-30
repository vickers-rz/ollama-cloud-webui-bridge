from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import queue
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse


# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [chrome-ai] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("chrome_ai")

# ── 配置 ──────────────────────────────────────────────────────────────────────
HOST = os.environ.get("CHROME_AI_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHROME_AI_PORT", "8766"))
MODEL_ID = os.environ.get("CHROME_AI_MODEL", "chrome-gemini-nano")
TASK_TIMEOUT_SECONDS = int(os.environ.get("CHROME_AI_TASK_TIMEOUT", "180"))

# ── 全局状态 ──────────────────────────────────────────────────────────────────
tasks: queue.Queue = queue.Queue()

# #1 #2 用 Event 替代忙等：task_id -> threading.Event
_pending: dict[str, threading.Event] = {}
_results: dict[str, dict] = {}
_state_lock = threading.Lock()          # 保护 _pending / _results 的并发访问

worker_seen_at: float = 0
_started_at: float = time.time()


def _put_result(task_id: str, payload: dict) -> None:
    """worker 回传结果时调用，线程安全。"""
    with _state_lock:
        _results[task_id] = payload
        event = _pending.get(task_id)
    if event:
        event.set()


def _wait_result(task_id: str, timeout: float) -> dict | None:
    """
    等待 task_id 对应的结果，最多等 timeout 秒。
    返回结果 dict，超时返回 None。超时后自动清理状态，不泄漏内存。
    """
    event = threading.Event()
    with _state_lock:
        _pending[task_id] = event

    fired = event.wait(timeout=timeout)

    with _state_lock:
        _pending.pop(task_id, None)
        result = _results.pop(task_id, None)   # #2 无论成功/超时都清理

    if not fired:
        return None
    return result


# ── 消息格式化 ────────────────────────────────────────────────────────────────
def messages_to_prompt(messages: list) -> str:
    lines = []
    for message in messages:
        role = message.get("role", "user")
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in {None, "text"}
            )
        lines.append(f"{role}: {content}")
    lines.append("assistant:")
    return "\n".join(lines)


# ── Worker HTML（JS 改为 POST 回传结果）────────────────────────────────────────
WORKER_HTML = """
<!doctype html>
<meta charset="utf-8">
<title>Chrome AI Bridge Worker</title>
<style>
body { font: 14px system-ui, sans-serif; margin: 2rem; line-height: 1.4; }
pre { background: #f6f8fa; padding: 1rem; white-space: pre-wrap; }
</style>
<h1>Chrome AI Bridge Worker</h1>
<p>Keep this Chrome tab open while Open WebUI uses <code>chrome-gemini-nano</code>.</p>
<pre id="log">starting...</pre>
<script>
const logEl = document.getElementById("log");
function log(line) {
  logEl.textContent = new Date().toISOString() + " " + line + "\\n" + logEl.textContent;
}

async function postJson(url, body) {
  const response = await fetch(new URL(url, location.origin).href, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch (error) {
    throw new Error(
      "Expected JSON from " + url + ", got status " + response.status + ": " +
      text.slice(0, 160)
    );
  }
}

async function fetchNext() {
  // #5 改用 POST /worker/next 取任务
  return postJson("/worker/next", {});
}

async function runPrompt(prompt) {
  if (!window.LanguageModel) {
    throw new Error("LanguageModel API is not available in this Chrome profile");
  }
  const availability = await LanguageModel.availability();
  if (availability !== "available") {
    throw new Error("LanguageModel availability is " + availability);
  }
  const session = await LanguageModel.create();
  try {
    return await session.prompt(prompt);
  } finally {
    if (typeof session.destroy === "function") session.destroy();
  }
}

async function loop() {
  while (true) {
    try {
      const task = await fetchNext();
      if (task && task.id) {
        log("task " + task.id + " received");
        try {
          const content = await runPrompt(task.prompt);
          // #5 改用 POST JSON 回传结果，避免 URL 长度限制
          await postJson("/worker/result", {id: task.id, content});
          log("task " + task.id + " done (" + content.length + " chars)");
        } catch (error) {
          await postJson("/worker/result", {
            id: task.id,
            error: String(error && (error.stack || error.message || error)),
          });
          log("task " + task.id + " failed: " + error);
        }
      }
    } catch (error) {
      log("poll failed: " + error);
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
}

loop();
</script>
"""


# ── 请求处理器 ────────────────────────────────────────────────────────────────
class ChromeAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, body, status=200, content_type="application/json"):
        if isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_OPTIONS(self):
        self._send({}, content_type="application/json")

    def do_GET(self):
        parsed = urlparse(self.path)
        if self.path == "/":
            self._send(WORKER_HTML, content_type="text/html; charset=utf-8")
            return
        if self.path == "/health":
            # #7 丰富 /health 信息
            now = time.time()
            with _state_lock:
                pending_count = len(_pending)
            self._send(
                {
                    "ok": True,
                    "model": MODEL_ID,
                    "worker_connected": worker_seen_at > 0 and now - worker_seen_at < 15,
                    "worker_last_seen_seconds_ago": round(now - worker_seen_at, 1) if worker_seen_at else None,
                    "queue_depth": tasks.qsize(),        # #7 队列深度
                    "pending_tasks": pending_count,      # #7 等待结果的任务数
                    "uptime_seconds": round(now - _started_at),  # #7 运行时长
                }
            )
            return
        if self.path == "/v1/models":
            self._send(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "created": 0,
                            "owned_by": "chrome",
                        }
                    ],
                }
            )
            return
        # GET /worker/result 已废弃，保留兼容但记录警告
        if parsed.path == "/worker/result":
            log.warning("Deprecated GET /worker/result received — worker should use POST")
            query = parse_qs(parsed.query)
            task_id = query.get("id", [""])[0]
            _put_result(task_id, {
                "id": task_id,
                "content": query.get("content", [""])[0],
                "error": query.get("error", [None])[0],
            })
            self._send({"ok": True})
            return
        self._send({"error": "not found"}, status=404)

    def do_POST(self):
        global worker_seen_at

        if self.path in {"/worker/next", "/worker/poll"}:
            # #5 worker 改用 POST 取任务
            worker_seen_at = time.time()
            try:
                task = tasks.get(timeout=1)
                log.debug("dispatching task %s to worker", task.get("id"))
            except queue.Empty:
                task = {}
            self._send(task)
            return

        if self.path == "/worker/result":
            # #5 worker 用 POST JSON 回传结果
            payload = self._read_json()
            task_id = payload.get("id", "")
            if not task_id:
                self._send({"error": "missing id"}, status=400)
                return
            log.info("worker result received task_id=%s error=%s", task_id, bool(payload.get("error")))
            _put_result(task_id, payload)
            self._send({"ok": True})
            return

        if self.path == "/v1/chat/completions":
            payload = self._read_json()
            if payload.get("model") != MODEL_ID:
                self._send({"error": f"unknown model: {payload.get('model')}"}, status=404)
                return
            if payload.get("stream"):
                self._send({"error": "streaming is not implemented"}, status=400)
                return

            task_id = str(uuid.uuid4())
            prompt = messages_to_prompt(payload.get("messages", []))
            log.info("chat request task_id=%s prompt_len=%d", task_id, len(prompt))
            tasks.put({"id": task_id, "prompt": prompt})

            # #1 用 Event 等待，不再忙等
            result = _wait_result(task_id, timeout=TASK_TIMEOUT_SECONDS)

            if result is None:
                # #2 超时时状态已在 _wait_result 内清理
                log.warning("task %s timed out after %ds", task_id, TASK_TIMEOUT_SECONDS)
                self._send(
                    {
                        "error": (
                            "Chrome AI worker timed out. Open "
                            f"http://{HOST}:{PORT}/ in Chrome and keep the tab active."
                        )
                    },
                    status=504,
                )
                return

            if result.get("error"):
                log.warning("task %s worker error: %s", task_id, result["error"])
                self._send({"error": result["error"]}, status=500)
                return

            now = int(time.time())
            content = result.get("content", "")
            log.info("task %s completed content_len=%d", task_id, len(content))
            self._send(
                {
                    "id": f"chatcmpl-{task_id}",
                    "object": "chat.completion",
                    "created": now,
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": content,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            )
            return

        self._send({"error": "not found"}, status=404)

    def log_message(self, fmt, *args):
        # 已由 logging 模块接管
        pass


if __name__ == "__main__":
    log.info("Chrome AI bridge listening on http://%s:%d", HOST, PORT)
    log.info("Open this URL in Chrome and keep the worker tab open.")
    ThreadingHTTPServer((HOST, PORT), ChromeAIHandler).serve_forever()
