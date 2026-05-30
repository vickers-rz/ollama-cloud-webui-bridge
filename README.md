# Ollama Cloud WebUI Bridge

Expose Ollama Cloud models, Chrome Built-in AI, and local Ollama models to Open WebUI through a single lightweight proxy container.

## Why This Exists

Ollama's own desktop chat UI can use cloud models such as:

- `gemma4:31b-cloud`
- `nemotron-3-super:cloud`

Open WebUI depends on `/api/tags` for model discovery, but Ollama Cloud models do not appear in that endpoint's response. This project adds a tiny proxy container that:

1. Returns virtual entries for cloud models and `chrome-gemini-nano` in `/api/tags`
2. Fetches the real local model list from the upstream Ollama and merges it (so local models like `qwen3.5:4b` also appear automatically)
3. Forwards chat/generate requests for cloud models to the real host Ollama
4. Routes `chrome-gemini-nano` requests to a local Chrome AI bridge

## Architecture

```text
Open WebUI container
  → http://ollama-cloud-proxy:11434  (Docker-internal)
      ├── GET /api/tags
      │     virtual cloud models  +  chrome-gemini-nano
      │     + real local models fetched live from host Ollama
      │
      ├── cloud model requests (gemma4:31b-cloud, nemotron-3-super:cloud …)
      │     → http://host.docker.internal:11434
      │           → macOS Ollama → Ollama Cloud
      │
      ├── local model requests (qwen3.5:4b, llama3 …)
      │     → http://host.docker.internal:11434
      │           → macOS Ollama (local GGUF inference)
      │
      └── chrome-gemini-nano requests
            → http://host.docker.internal:8766
                  → Chrome AI bridge on macOS
                        → Chrome LanguageModel API
                              → Chrome-managed Gemini Nano model
```

## Included Models

The proxy surfaces:

| Model | Type | Source |
|---|---|---|
| `gemma4:31b-cloud` | Cloud | Ollama Cloud via host Ollama |
| `nemotron-3-super:cloud` | Cloud | Ollama Cloud via host Ollama |
| `chrome-gemini-nano` | On-device | Chrome LanguageModel API |
| *(any local model)* | Local | Host Ollama — auto-discovered |

Local models (pulled with `ollama pull`) appear in Open WebUI automatically on every refresh. No configuration change required.

## Quick Start

Prerequisites:

- OrbStack or Docker Desktop
- Ollama installed on macOS
- Ollama's "Expose Ollama to network" enabled
- A working Ollama Cloud sign-in in the Ollama app

Open WebUI must be on the same Docker network as the proxy. The
[orbstack-open-webui compose](../Codex/2026-05-30/orbstack-open-webui/docker-compose.yml)
declares `ollama-cloud-webui-bridge_default` as an external network.

Start the proxy:

```bash
cd /Users/vickers/Documents/ollama-cloud-webui-bridge
docker compose up -d
```

Start Open WebUI (separate compose project):

```bash
cd /Users/vickers/Documents/Codex/2026-05-30/orbstack-open-webui
docker compose up -d
```

Start the Chrome AI bridge on macOS if you want `chrome-gemini-nano`:

```bash
python3 work/chrome_ai_bridge.py
```

Then open this URL in Chrome and keep the tab open:

```text
http://127.0.0.1:8766/
```

Open WebUI:

```text
http://localhost:3000
```

## Daily Use Runbook

Recommended startup order:

1. **Start OrbStack** — `restart: unless-stopped` means containers resume automatically.

   ```bash
   docker ps   # confirm open-webui and ollama-cloud-proxy are up
   ```

2. **Start the Chrome AI bridge** (only needed for `chrome-gemini-nano`):

   Foreground:
   ```bash
   python3 work/chrome_ai_bridge.py
   ```

   Background:
   ```bash
   nohup python3 work/chrome_ai_bridge.py > /tmp/chrome_ai_bridge.log 2>&1 &
   ```

3. **Open the Chrome worker page** and keep it open:

   ```text
   http://127.0.0.1:8766/
   ```

4. **Open Open WebUI**:

   ```text
   http://localhost:3000
   ```

## Emergency Bypass: Proxy Down

If `ollama-cloud-proxy` is stopped or broken and you need Open WebUI to work
with local Ollama models only, bypass the proxy by pointing Open WebUI directly
at the host Ollama.

**Step 1 — Edit the Open WebUI compose file:**

```bash
# file: /Users/vickers/Documents/Codex/2026-05-30/orbstack-open-webui/docker-compose.yml
```

Change:

```yaml
OLLAMA_BASE_URL: "http://ollama-cloud-proxy:11434"
```

To:

```yaml
OLLAMA_BASE_URL: "http://host.docker.internal:11434"
```

And add under the service:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

**Step 2 — Recreate the container:**

```bash
cd /Users/vickers/Documents/Codex/2026-05-30/orbstack-open-webui
docker compose up -d --force-recreate
```

**Step 3 — Restore when proxy is back:**

Revert the two changes above and recreate again:

```bash
docker compose up -d --force-recreate
```

> Note: In bypass mode, cloud models (`gemma4:31b-cloud`, `nemotron-3-super:cloud`,
> `chrome-gemini-nano`) will not appear. Only models pulled via `ollama pull` are visible.

## Add More Cloud Models

Edit `docker-compose.yml`:

```yaml
CLOUD_MODELS: "gemma4:31b-cloud,nemotron-3-super:cloud,qwen3.5:cloud"
```

Recreate:

```bash
docker compose up -d --force-recreate
```

## Add More Local Models

Local Ollama models are discovered automatically. Just pull:

```bash
ollama pull llama3.2
```

Refresh Open WebUI — the model appears immediately, no config change needed.

## Verify

Check containers:

```bash
docker compose ps
```

Check what Open WebUI sees through the proxy:

```bash
docker exec open-webui python3 -c "
import urllib.request, json
d = json.loads(urllib.request.urlopen('http://ollama-cloud-proxy:11434/api/tags', timeout=5).read())
for m in d['models']: print(m['name'], m['details']['format'])
"
```

Check the Chrome AI bridge:

```bash
curl -s http://127.0.0.1:8766/health
```

Ready output:

```json
{
  "ok": true,
  "model": "chrome-gemini-nano",
  "worker_connected": true,
  "queue_depth": 0,
  "pending_tasks": 0,
  "uptime_seconds": 42
}
```

If `worker_connected` is `false`, open `http://127.0.0.1:8766/` in Chrome.

Check proxy logs:

```bash
docker logs ollama-cloud-proxy --tail 30
```

Test Chrome model end-to-end:

```bash
docker exec open-webui python3 -c "
import urllib.request, json
req = urllib.request.Request(
    'http://ollama-cloud-proxy:11434/api/chat',
    data=json.dumps({'model':'chrome-gemini-nano','stream':False,
                     'messages':[{'role':'user','content':'Reply: bridge OK'}]}).encode(),
    headers={'Content-Type':'application/json'}, method='POST')
print(json.loads(urllib.request.urlopen(req, timeout=30).read()))
"
```

## Notes

This is not an Ollama replacement and not a model server.

It is a compatibility shim for Open WebUI model discovery. Cloud models are callable by name through Ollama but do not appear in `/api/tags` — this proxy patches that gap while also surfacing real local models automatically.

No passwords, Ollama account tokens, Open WebUI credentials, or Chrome profile data are stored in this project.

The Chrome AI path is intentionally minimal:

- non-streaming chat only
- one local Chrome worker tab should stay open
- no direct access to `weights.bin`
- no browser cookies, history, bookmarks, passwords, or profile stores are read
- if Chrome closes or the worker tab is closed, `chrome-gemini-nano` requests will time out

## Project Name

The first working directory name was `Ollama_unlocker`, but that name suggests bypassing or unlocking restrictions. This project does not bypass authentication or usage policy. It only bridges a model-discovery mismatch, so `ollama-cloud-webui-bridge` is a clearer and safer name.
