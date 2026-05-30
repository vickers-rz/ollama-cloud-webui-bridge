from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import queue
import time
import uuid
from urllib.parse import parse_qs, urlparse


HOST = os.environ.get("CHROME_AI_HOST", "127.0.0.1")
PORT = int(os.environ.get("CHROME_AI_PORT", "8766"))
MODEL_ID = os.environ.get("CHROME_AI_MODEL", "chrome-gemini-nano")
TASK_TIMEOUT_SECONDS = int(os.environ.get("CHROME_AI_TASK_TIMEOUT", "180"))

tasks = queue.Queue()
results = {}
worker_seen_at = 0


def messages_to_prompt(messages):
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

async function fetchJson(url) {
  const response = await fetch(new URL(url, location.origin).href, {
    cache: "no-store",
    headers: {"Cache-Control": "no-store"},
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
      const task = await fetchJson("/worker/next");
      if (task && task.id) {
        log("task " + task.id + " received");
        try {
          const content = await runPrompt(task.prompt);
          await fetchJson(
            "/worker/result?id=" + encodeURIComponent(task.id) +
            "&content=" + encodeURIComponent(content)
          );
          log("task " + task.id + " done");
        } catch (error) {
          await fetchJson(
            "/worker/result?id=" + encodeURIComponent(task.id) +
            "&error=" + encodeURIComponent(String(error && (error.stack || error.message || error)))
          );
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
        if parsed.path == "/worker/next":
            global worker_seen_at
            worker_seen_at = time.time()
            try:
                task = tasks.get(timeout=1)
            except queue.Empty:
                task = {}
            self._send(task)
            return
        if parsed.path == "/worker/result":
            query = parse_qs(parsed.query)
            task_id = query.get("id", [""])[0]
            results[task_id] = {
                "id": task_id,
                "content": query.get("content", [""])[0],
                "error": query.get("error", [None])[0],
            }
            self._send({"ok": True})
            return
        if self.path == "/":
            self._send(WORKER_HTML, content_type="text/html; charset=utf-8")
            return
        if self.path == "/health":
            self._send(
                {
                    "ok": True,
                    "model": MODEL_ID,
                    "worker_connected": worker_seen_at > 0
                    and time.time() - worker_seen_at < 15,
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
        self._send({"error": "not found"}, status=404)

    def do_POST(self):
        global worker_seen_at
        if self.path == "/worker/next":
            worker_seen_at = time.time()
            try:
                task = tasks.get(timeout=1)
            except queue.Empty:
                task = {}
            self._send(task)
            return
        if self.path == "/worker/result":
            payload = self._read_json()
            results[payload["id"]] = payload
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
            tasks.put({"id": task_id, "prompt": messages_to_prompt(payload.get("messages", []))})
            deadline = time.time() + TASK_TIMEOUT_SECONDS
            while time.time() < deadline:
                result = results.pop(task_id, None)
                if result:
                    if result.get("error"):
                        self._send({"error": result["error"]}, status=500)
                        return
                    now = int(time.time())
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
                                        "content": result.get("content", ""),
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
                time.sleep(0.1)
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
        self._send({"error": "not found"}, status=404)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    print(f"Chrome AI bridge listening on http://{HOST}:{PORT}")
    print("Open this URL in Chrome and keep the worker tab open.")
    ThreadingHTTPServer((HOST, PORT), ChromeAIHandler).serve_forever()
