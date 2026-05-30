# Ollama Cloud WebUI Bridge

Expose Ollama Cloud models to Open WebUI when Ollama can run the model but does not list it in `/api/tags`.

## Why This Exists

Ollama's own desktop chat UI can use cloud models such as:

- `gemma4:31b-cloud`
- `nemotron-3-super:cloud`

On this machine, the local Ollama service was healthy and reachable:

```text
Ollama 0.24.0
Listening on 0.0.0.0:11434
Expose Ollama to network: enabled
```

Open WebUI was deployed in OrbStack and configured to reach the host Ollama service. The obvious configuration was:

```text
OLLAMA_BASE_URL=http://host.docker.internal:11434
```

That verified correctly from inside the Open WebUI container:

```text
GET http://host.docker.internal:11434/api/version -> {"version":"0.24.0"}
```

But Open WebUI still showed no Ollama models, because Ollama's discovery endpoint returned an empty list:

```json
{"models":[]}
```

At the same time, Ollama itself could identify cloud models directly:

```text
ollama show gemma4:31b-cloud
ollama show nemotron-3-super:cloud
```

So the problem was not that Ollama Cloud was unusable. The problem was narrower:

```text
Open WebUI depends on /api/tags for Ollama model discovery.
Ollama Cloud models can be usable without appearing in /api/tags.
```

This project adds a tiny bridge container between Open WebUI and the host Ollama service.
It can also route one browser-managed Chrome Built-in AI model into Open WebUI.

## Architecture

```text
Open WebUI container
  -> http://ollama-cloud-proxy:11434
      -> Ollama cloud models
          -> http://host.docker.internal:11434
              -> macOS Ollama
                  -> Ollama Cloud
      -> chrome-gemini-nano
          -> http://host.docker.internal:8766
              -> Chrome AI bridge on macOS
                  -> Chrome LanguageModel API
                      -> Chrome-managed Gemini Nano model
```

The proxy does only two special things:

1. `GET /api/tags` and `GET /v1/models` return configured Ollama Cloud model names plus `chrome-gemini-nano`.
2. Requests for Ollama Cloud models are forwarded unchanged to the real host Ollama API.
3. Requests for `chrome-gemini-nano` are routed to the Chrome AI bridge.

That means model discovery is patched for Open WebUI, while actual chat and generation still go through the normal local Ollama service.
For `chrome-gemini-nano`, actual chat goes through a Chrome tab that calls the browser's Built-in AI API.

## Included Models

The current compose file exposes:

```text
gemma4:31b-cloud
nemotron-3-super:cloud
chrome-gemini-nano
```

`gemma4:31b-cloud` is useful as the default general model.

`nemotron-3-super:cloud` is useful for harder reasoning, planning, coding, and agent-style workflows.

`chrome-gemini-nano` is the Chrome-managed on-device Gemini Nano model. It is useful for lightweight local tasks when Chrome's `LanguageModel` API is available.

## Quick Start

Prerequisites:

- OrbStack or Docker Desktop
- Ollama installed on macOS
- Ollama's "Expose Ollama to network" enabled
- A working Ollama Cloud sign-in in the Ollama app

Start:

```bash
docker compose up -d
```

Start the Chrome AI bridge on macOS if you want to use `chrome-gemini-nano`:

```bash
python3 work/chrome_ai_bridge.py
```

Then open this URL in Chrome and keep the tab open:

```text
http://127.0.0.1:8766/
```

The worker page polls the local bridge for tasks, calls `LanguageModel.create()` in Chrome, and returns the response to Open WebUI.

Open:

```text
http://localhost:3000
```

Open WebUI should discover the configured cloud models through:

```text
http://ollama-cloud-proxy:11434
```

That hostname is Docker-internal. It is not meant to be opened in the macOS browser.

## Add More Cloud Models

Edit `docker-compose.yml`:

```yaml
CLOUD_MODELS: "gemma4:31b-cloud,nemotron-3-super:cloud,qwen3.5:cloud"
```

Then recreate:

```bash
docker compose up -d --force-recreate
```

## Verify

Check containers:

```bash
docker compose ps
```

Check what Open WebUI sees:

```bash
docker exec open-webui curl -s http://ollama-cloud-proxy:11434/api/tags
```

Check the Chrome AI bridge from macOS:

```bash
curl -s http://127.0.0.1:8766/health
```

Check the Chrome model through the Docker bridge:

```bash
docker exec open-webui curl -s http://ollama-cloud-proxy:11434/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"model":"chrome-gemini-nano","stream":false,"messages":[{"role":"user","content":"Reply with exactly: bridge OK"}]}'
```

Check host Ollama from inside Open WebUI:

```bash
docker exec open-webui curl -s http://ollama-cloud-proxy:11434/api/version
```

## Notes

This is not an Ollama replacement and not a model server.

It is a compatibility shim for Open WebUI model discovery. It exists because the cloud models are callable by name through Ollama but may not be listed by Ollama's local model inventory endpoint.

No passwords, Ollama account tokens, Open WebUI credentials, or Chrome profile data are stored in this project.

The Chrome AI path is intentionally minimal:

- non-streaming chat only
- one local Chrome worker tab should stay open
- no direct access to `weights.bin`
- no browser cookies, history, bookmarks, passwords, or profile stores are read
- if Chrome closes or the worker tab is closed, `chrome-gemini-nano` requests will time out

## Project Name

The first working directory name was `Ollama_unlocker`, but that name suggests bypassing or unlocking restrictions. This project does not bypass authentication or usage policy. It only bridges a model-discovery mismatch, so `ollama-cloud-webui-bridge` is a clearer and safer name.
