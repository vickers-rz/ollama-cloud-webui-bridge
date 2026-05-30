from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone


# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("proxy")

# ── 配置 ──────────────────────────────────────────────────────────────────────
UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://host.docker.internal:11434")
CHROME_AI_UPSTREAM = os.environ.get(
    "CHROME_AI_UPSTREAM",
    "http://host.docker.internal:8766",
)
CHROME_AI_MODEL = os.environ.get("CHROME_AI_MODEL", "chrome-gemini-nano")
MODEL_IDS = [
    model.strip()
    for model in os.environ.get(
        "CLOUD_MODELS",
        "gemma4:31b-cloud,nemotron-3-super:cloud",
    ).split(",")
    if model.strip()
]
ALL_MODEL_IDS = MODEL_IDS + [CHROME_AI_MODEL]

# 启动时间，用于 modified_at 字段
_STARTED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

log.info("upstream=%s cloud_models=%s chrome_model=%s", UPSTREAM, MODEL_IDS, CHROME_AI_MODEL)


# ── 工具函数 ──────────────────────────────────────────────────────────────────
def fetch_upstream_models():
    """从上游 Ollama 获取真实模型列表，失败时返回空列表。"""
    try:
        req = urllib.request.Request(
            f"{UPSTREAM}/api/tags",
            method="GET",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("models", [])
    except Exception as exc:
        log.warning("fetch_upstream_models failed: %s", exc)
        return []


def model_payload(model_id):
    family = model_id.split(":", 1)[0]
    model_format = "chrome-ai" if model_id == CHROME_AI_MODEL else "cloud"
    size = 4 * 1024 * 1024 * 1024 if model_id == CHROME_AI_MODEL else 0
    parameter_size = "browser-managed" if model_id == CHROME_AI_MODEL else "cloud"
    return {
        "name": model_id,
        "model": model_id,
        "modified_at": _STARTED_AT,   # #10 动态生成，不再硬编码日期
        "size": size,
        "digest": model_format,
        "details": {
            "parent_model": "",
            "format": model_format,
            "family": family,
            "families": [family],
            "parameter_size": parameter_size,
            "quantization_level": model_format,
        },
    }


def _502_body(exc):
    return json.dumps({"error": f"upstream unavailable: {exc}"}).encode("utf-8")


# ── 请求处理器 ────────────────────────────────────────────────────────────────
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        req = urllib.request.Request(
            f"{UPSTREAM}{self.path}",
            data=body if body else None,
            method=self.command,
            headers={
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "content-length", "accept-encoding"}
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in {"transfer-encoding", "connection"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as err:
            data = err.read()
            self.send_response(err.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as exc:          # #3 新增：网络/连接异常
            log.error("_proxy %s %s -> URLError: %s", self.command, self.path, exc)
            body = _502_body(exc)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _post_json(self, url, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.URLError as exc:          # #3 新增
            log.error("_post_json %s -> URLError: %s", url, exc)
            raise

    def _read_json(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _proxy_chrome_ai_openai_chat(self, payload):
        payload["model"] = CHROME_AI_MODEL
        try:
            status, headers, data = self._post_json(
                f"{CHROME_AI_UPSTREAM}/v1/chat/completions",
                payload,
            )
        except urllib.error.URLError as exc:
            self._send_json({"error": f"chrome_ai_upstream unavailable: {exc}"}, status=502)
            return
        self.send_response(status)
        for key, value in headers.items():
            if key.lower() not in {"transfer-encoding", "connection"}:
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _proxy_chrome_ai_ollama_chat(self, payload):
        messages = payload.get("messages") or []
        openai_payload = {
            "model": CHROME_AI_MODEL,
            "messages": messages,
            "stream": False,
        }
        if "options" in payload:
            openai_payload["options"] = payload["options"]
        try:
            status, _headers, data = self._post_json(
                f"{CHROME_AI_UPSTREAM}/v1/chat/completions",
                openai_payload,
            )
        except urllib.error.URLError as exc:
            self._send_json({"error": f"chrome_ai_upstream unavailable: {exc}"}, status=502)
            return
        if status >= 400:
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        result = json.loads(data.decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        self._send_json(
            {
                "model": CHROME_AI_MODEL,
                "created_at": result.get("created_at", _STARTED_AT),
                "message": {"role": "assistant", "content": content},
                "done": True,
            }
        )

    def _proxy_chrome_ai_ollama_generate(self, payload):
        prompt = payload.get("prompt", "")
        openai_payload = {
            "model": CHROME_AI_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        try:
            status, _headers, data = self._post_json(
                f"{CHROME_AI_UPSTREAM}/v1/chat/completions",
                openai_payload,
            )
        except urllib.error.URLError as exc:
            self._send_json({"error": f"chrome_ai_upstream unavailable: {exc}"}, status=502)
            return
        if status >= 400:
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        result = json.loads(data.decode("utf-8"))
        content = result["choices"][0]["message"]["content"]
        self._send_json(
            {
                "model": CHROME_AI_MODEL,
                "created_at": result.get("created_at", _STARTED_AT),
                "response": content,
                "done": True,
            }
        )

    def do_GET(self):
        if self.path in {"/api/tags", "/api/ps"}:
            virtual_models = [model_payload(model_id) for model_id in ALL_MODEL_IDS]
            virtual_names = {m["name"] for m in virtual_models}
            upstream_models = [
                m for m in fetch_upstream_models()
                if m.get("name") not in virtual_names
            ]
            all_models = virtual_models + upstream_models
            log.info("GET %s -> %d models", self.path, len(all_models))
            self._send_json({"models": all_models})
            return
        if self.path == "/v1/models":
            self._send_json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": model_id,
                            "object": "model",
                            "created": 0,
                            "owned_by": "chrome" if model_id == CHROME_AI_MODEL else "ollama",
                        }
                        for model_id in ALL_MODEL_IDS
                    ],
                }
            )
            return
        self._proxy()

    def do_POST(self):
        if self.path == "/api/show":
            payload = self._read_json()
            if payload.get("model") == CHROME_AI_MODEL or payload.get("name") == CHROME_AI_MODEL:
                self._send_json(model_payload(CHROME_AI_MODEL))
                return
            self._proxy_payload(payload)
            return
        if self.path == "/v1/chat/completions":
            payload = self._read_json()
            if payload.get("model") == CHROME_AI_MODEL:
                self._proxy_chrome_ai_openai_chat(payload)
                return
            self._proxy_payload(payload)
            return
        if self.path == "/api/chat":
            payload = self._read_json()
            if payload.get("model") == CHROME_AI_MODEL:
                self._proxy_chrome_ai_ollama_chat(payload)
                return
            log.info("POST /api/chat model=%s -> upstream", payload.get("model"))
            self._proxy_payload(payload)
            return
        if self.path == "/api/generate":
            payload = self._read_json()
            if payload.get("model") == CHROME_AI_MODEL:
                self._proxy_chrome_ai_ollama_generate(payload)
                return
            log.info("POST /api/generate model=%s -> upstream", payload.get("model"))
            self._proxy_payload(payload)
            return
        self._proxy()

    def _proxy_payload(self, payload):
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{UPSTREAM}{self.path}",
            data=body,
            method=self.command,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in {"transfer-encoding", "connection"}:
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as err:
            data = err.read()
            self.send_response(err.code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.URLError as exc:          # #3 新增
            log.error("_proxy_payload %s -> URLError: %s", self.path, exc)
            body = _502_body(exc)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_DELETE(self):
        self._proxy()

    def log_message(self, fmt, *args):
        # 已由 logging 模块接管，屏蔽 BaseHTTPRequestHandler 的默认输出
        pass


if __name__ == "__main__":
    log.info("ollama-cloud-proxy listening on 0.0.0.0:11434")
    ThreadingHTTPServer(("0.0.0.0", 11434), ProxyHandler).serve_forever()
