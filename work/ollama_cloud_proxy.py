from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import urllib.error
import urllib.request


UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://host.docker.internal:11434")
MODEL_IDS = [
    model.strip()
    for model in os.environ.get(
        "CLOUD_MODELS",
        "gemma4:31b-cloud,nemotron-3-super:cloud",
    ).split(",")
    if model.strip()
]


def model_payload(model_id):
    family = model_id.split(":", 1)[0]
    return {
        "name": model_id,
        "model": model_id,
        "modified_at": "2026-05-30T00:00:00Z",
        "size": 0,
        "digest": "ollama-cloud",
        "details": {
            "parent_model": "",
            "format": "cloud",
            "family": family,
            "families": [family],
            "parameter_size": "cloud",
            "quantization_level": "cloud",
        },
    }


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

    def do_GET(self):
        if self.path in {"/api/tags", "/api/ps"}:
            self._send_json({"models": [model_payload(model_id) for model_id in MODEL_IDS]})
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
                            "owned_by": "ollama",
                        }
                        for model_id in MODEL_IDS
                    ],
                }
            )
            return
        self._proxy()

    def do_POST(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 11434), ProxyHandler).serve_forever()
