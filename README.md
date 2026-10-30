<div align="center">

# Telegram Bride for Open WebUI

**Two-way Telegram bridge that proxies conversations through Open WebUI's native chat API.**
Replies on Telegram appear in your real Open WebUI chat history — and vice versa.

[![MIT License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![python-telegram-bot 21.x](https://img.shields.io/badge/python--telegram--bot-21.x-blue.svg)](https://docs.python-telegram-bot.org/)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](Dockerfile)
[![v2.0.0](https://img.shields.io/badge/version-2.0.0-blue.svg)](CHANGELOG.md)

[Features](#features) • [How it works](#how-it-works) • [Quick start](#quick-start) • [Configuration](#configuration) • [Commands](#commands) • [Troubleshooting](#troubleshooting)

</div>

---

## Why this exists

Open WebUI has a built-in Telegram *tool* that lets the model push notifications to your phone — perfect for "ping me when you find something". But there's no built-in way to **reply** to those notifications and have the answer flow back into the same chat in Open WebUI.

This bridge fills that gap. It runs as a small sidecar service that:

- Receives your Telegram messages (long polling by default, webhooks optional)
- Creates real, persistent chats in Open WebUI via its native `/api/v1/chats/new` endpoint
- Streams the model's reply back to Telegram with a clickable link to the conversation
- Continues the same Open WebUI chat when you reply to one of the bot's messages
- Resolves the right model automatically from your Open WebUI user settings

The result: Telegram becomes a remote control for Open WebUI, and every chat you have over Telegram shows up in your sidebar with full history, tools, system prompts, and RAG — exactly as if you typed it in the web UI.

## Features

- 🚀 **Zero-friction setup** — single container, ~5 env vars, done
- 🔁 **True two-way sync** — every Telegram reply becomes a user message in your Open WebUI chat history
- 🔗 **Clickable backlinks** — every bot reply includes a `https://your-host/c/<chat-id>` link so you can jump straight back to the web UI
- 🧬 **Agent continuity** — when an OWUI agent sends you a Telegram message, your reply continues *that* chat, not a new one (v1.2.0)
- 🧠 **Auto model resolution** — env var → your OWUI default → first available. No more `Model not found`.
- ⚡ **Streaming responses** — replies appear token-by-token in Telegram via `editMessageText`, throttled to respect rate limits
- 📦 **Multi-message chunking** — long replies split at paragraph boundaries, not truncated
- 🖼️ **Image support** — send a photo on Telegram, it gets uploaded to OWUI and attached to a new chat
- 🎯 **Per-chat system prompt** — `/system <prompt>` to scope behavior per Telegram chat
- 🧠 **Per-session model switching** — `/model <name>` to change models mid-conversation
- 🌐 **English and Russian interface** — follows your Telegram language by default; change it with `/language`
- 💾 **Persistent sessions** — chat pointers survive container restarts via a JSON file
- 🩺 **HTTP `/health` endpoint** — for Docker healthchecks, Uptime Kuma, K8s liveness probes
- 🔒 **Per-user allowlist** — lock the bot to your Telegram user ID only
- 🛠️ **Tools pass-through** — Open WebUI tools (web search, knowledge bases, your `send_telegram` notifier) work in Telegram-initiated chats too
- 🐳 **Tiny footprint** — `python:3.12-slim` base, runs on ~50 MB RAM

## How it works

```
                                   OWUI agent or user
                                          │
                                          │ tool call (send_telegram)
                                          ▼
┌────────────┐    updates    ┌─────────────────────┐    REST     ┌──────────────┐
│  Telegram  │ ────────────► │  telegram-bridge    │ ──────────► │  Open WebUI  │
│  (your     │               │  (this container)   │             │  /api/v1/... │
│   phone)   │ ◄──────────── │                     │ ◄────────── │              │
└────────────┘   bot reply   └─────────────────────┘  completion  └──────────────┘
        ▲                          │  ▲
        │                          │  │
        │      /api/outbound       │  │ /api/chat/completions (streamed)
        └──────────────────────────┘  │
                                     │
                                     │ creates / appends to a real chat
                                     ▼
                              Open WebUI chat DB
                              (visible in your sidebar)
```

**Inbound** (phone → OWUI): Telegram updates → bridge → `/api/v1/chats/new` + `/api/chat/completions`. The bridge creates real, persistent chats.

**Outbound** (OWUI → phone, optional): the companion `send_telegram` OWUI tool (in `owui_send_telegram_tool.py`) POSTs to the bridge's `/api/outbound`. The bridge forwards to Telegram **and remembers the `telegram_message_id → owui_chat_id` mapping** so when you reply to the agent's message, the bridge routes your reply back to the originating OWUI chat.

| Event | Open WebUI call |
|---|---|
| Telegram: new message | `POST /api/v1/chats/new` → `POST /api/chat/completions` (streamed) |
| Telegram: reply to bridge's message | `POST /api/chat/completions` (streamed, continues existing chat) |
| Telegram: reply to **agent-sent** message | `POST /api/chat/completions` (streamed, continues the *agent's* chat via outbound map) |
| Telegram: photo upload | `POST /api/v1/files/` → `POST /api/v1/chats/new` with file ref → `POST /api/chat/completions` |
| OWUI tool: `send_telegram` | `POST /api/outbound` on bridge → Telegram Bot API → bridge records `msg_id → chat_id` |

## Quick start

### Prerequisites

- A running [Open WebUI](https://github.com/open-webui/open-webui) instance (v0.4.0+)
- A Telegram bot token (get one from [@BotFather](https://t.me/BotFather))
- Your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))
- Docker + Docker Compose (or Python 3.12+ if you prefer to run it bare)

### 1. Clone

```bash
git clone https://github.com/NopeNix/telegram-bridge-openwebui.git
cd telegram-bridge-openwebui
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in the **required** values:

```dotenv
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
OPENWEBUI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
OPENWEBUI_BASE_URL=http://host.docker.internal:3000
ALLOWED_USER_IDS=123456789
```

`OPENWEBUI_BASE_URL` notes:

| Open WebUI location | Use this value |
|---|---|
| On the host (not in Docker) | `http://host.docker.internal:3000` |
| Sibling container (same Docker network) | `http://open-webui:8080` |
| Remote server | `https://openwebui.your-domain.com` |
| Both running bare metal | `http://localhost:3000` |

`DEFAULT_MODEL` can be left empty — the bot will auto-resolve from your OWUI user settings.

### 3. Run

```bash
docker compose up -d
```

Watch the logs:

```bash
docker compose logs -f telegram-bridge
```

You should see:

```
INFO  owui-telegram: Resolving model (env → user settings → first available)…
INFO  owui-telegram: ✓ Model resolved: gpt-4o (from user settings (ui.models[0]))
INFO  owui-telegram: Application started
```

### 4. Test

Open Telegram, find your bot, send `/start`. Then send any message — within a few seconds you'll get a streamed reply with a link to your brand new Open WebUI chat.

## Configuration

All configuration is via environment variables (see [`.env.example`](.env.example)).

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `OPENWEBUI_API_KEY` | ✅ | — | Default Open WebUI API key; used when no personal key is configured |
| `OPENWEBUI_API_KEY_<TG_ID>` | | — | Optional personal API key for a Telegram user, e.g. `OPENWEBUI_API_KEY_123456789`; overrides the default key for that user |
| `OPENWEBUI_BASE_URL` | | `http://localhost:3000` | Base URL of your Open WebUI instance |
| `DEFAULT_MODEL` | | auto | Model to use. If empty, uses OWUI user default → first available |
| `DEFAULT_SYSTEM_PROMPT` | | (none) | Per-chat system prompt applied to every Telegram message |
| `ALLOWED_USER_IDS` | ⭐ | (all) | Comma-separated Telegram user IDs. **Strongly recommended.** |
| `REQUEST_TIMEOUT` | | `120` | HTTP timeout in seconds for OWUI API calls |
| `SESSIONS_FILE` | | `/app/data/sessions.json` | Persistent session storage path |
| `LOG_LEVEL` | | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `STREAM_THROTTLE_SECONDS` | | `1.0` | Min seconds between Telegram message edits while streaming |
| `WEBHOOK_URL` | | (polling) | When set, switches from long polling to webhook mode |
| `WEBHOOK_PORT` | | `8089` | Port to listen on for Telegram webhooks |
| `HEALTH_PORT` | | `8088` | Port for the HTTP `/health` endpoint |

### Model auto-resolution chain

When `DEFAULT_MODEL` is empty, the bot tries these in order:

1. **`/api/v1/users/user/settings`** → reads `ui.models[0]` (your OWUI default)
2. **`/api/models`** → picks the first available model
3. **Fails loudly** at startup if neither yields a model — better than failing mid-conversation

### Bare-metal / non-Docker run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)
python telegram_bot.py
```

## Commands

| Command | Description |
|---|---|
| `/start` | Show usage help |
| `/newchat` | Start a fresh chat on the next message |
| `/id` | Show a link to the current chat |
| `/model` | List available models + show current model |
| `/model <name>` | Switch the active model mid-session |
| `/system show` | Show the active system prompt for this chat |
| `/system <prompt>` | Set a system prompt for this chat (persists) |
| `/system clear` | Remove the system prompt (use OWUI default) |
| `/language` | Show the current bot language |
| `/language en` or `/language ru` | Set the bot language |
| `/language default` | Follow the Telegram language (English if unsupported) |

### Conversation flow

```
You:        "What's the best Python ORM in 2025?"
Bot:        "SQLAlchemy 2.0 and SQLModel..."   ← streamed, new chat created
            🔗 http://host:3000/c/abc-123

You:        (reply to that message) "Show me a SQLModel example"
Bot:        "Here's a quick example..."         ← streamed, same chat continued
            🔗 http://host:3000/c/abc-123

You:        /newchat
Bot:        "🆕 Next message will start a fresh chat."

You:        /model llama3.1
Bot:        "✅ Switched to: llama3.1"

You:        (sends a photo of code with caption "what does this do?")
Bot:        "This code reads a CSV file..."     ← image uploaded, new chat
            🔗 http://host:3000/c/def-456
```

### Health endpoint

```bash
curl http://localhost:8088/health
# {"status": "ok", "model": "gpt-4o (from user settings (ui.models[0]))"}
```

Returns `200` when the bot is fully initialized, `503` while still booting.

## Troubleshooting

### Bot doesn't reply

1. Check `docker compose logs telegram-bridge` for errors.
2. Verify `TELEGRAM_BOT_TOKEN` is correct — `/start` should always respond even if OWUI is down.
3. Verify `ALLOWED_USER_IDS` contains your Telegram user ID. If empty, all users are allowed.

### `❌ Open WebUI returned 400 — Model not found`

`DEFAULT_MODEL` is empty and auto-resolution picked a model ID that doesn't exist. Either:
- Set a real `DEFAULT_MODEL` in `.env`, or
- Configure your default model in Open WebUI (Settings → Interface → Default Model), or
- Run `/model <existing-model-id>` in Telegram to override

### `❌ Open WebUI returned 401`

Wrong or expired `OPENWEBUI_API_KEY`. Generate a new one in Settings → Account → API Keys.

### `❌ Open WebUI returned 403`

The API key's user lacks permission. Make sure the key belongs to an admin or has the required group permissions.

### `❌ Open WebUI returned 404`

`OPENWEBUI_BASE_URL` doesn't resolve from inside the container. Try `http://host.docker.internal:3000` (host deployment) or the Docker service name (sibling container).

### Bot works, links don't open

`OPENWEBUI_BASE_URL` is set to a URL that isn't reachable from your phone. If your OWUI is behind a domain like `https://openwebui.example.com`, use that — not `http://localhost:3000`.

### Sessions forgotten after restart

You're not mounting the `data/` volume. `docker-compose.yml` includes `./data:/app/data` — check that directory exists and is writable by the container user.

## Limitations

- **Voice messages** are not yet handled. (Image support shipped in v1.1.0; voice is next.)
- **Single active chat per Telegram user.** Use `/newchat` to switch contexts.
- **Long polling by default** — fine for single-user setups. Webhook mode available via `WEBHOOK_URL`.

## Contributing

Contributions are welcome. Please open an issue first to discuss substantial changes.

```bash
git clone https://github.com/NopeNix/telegram-bridge-openwebui.git
cd telegram-bridge-openwebui
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in your values
python telegram_bot.py
```

## Security

- **Always set `ALLOWED_USER_IDS`.** Without it, anyone who finds your bot can use it.
- The bot reads/writes your Open WebUI data. Treat `OPENWEBUI_API_KEY` like a password.
- The image runs as a non-root user (`uid 1000`) inside the container.
- No data is logged except operational metadata (user IDs, error responses).
- The `/health` endpoint exposes the current model name. Don't expose port `8088` to the public internet.

## Companion: OWUI `send_telegram` tool

The repo ships a companion OWUI tool at [`owui_send_telegram_tool.py`](owui_send_telegram_tool.py). It defaults to **bridge mode** and POSTs to `bridge_url/api/outbound` (default `http://host.docker.internal:8089`). It falls back to direct Telegram Bot API calls if `bridge_url` is empty.

Install it in OWUI:
1. Workspace → Tools → + Create
2. Paste the contents of `owui_send_telegram_tool.py`
3. Save and configure the **Valves**:
   - `bridge_url` = `http://host.docker.internal:8089` (default)
   - `bridge_token` = leave empty unless you set `BRIDGE_OUTBOUND_TOKEN` on the bridge
   - `openwebui_url` = `https://openwebui.example.com` (your OWUI base URL)
4. Enable the tool on your model or per-chat

With this tool installed, OWUI agents can ping you on Telegram **and** when you reply, the bridge routes your reply back into the same chat the agent was working in.

## CI/CD

Every push to `main` (and every `v*` tag) triggers [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml), which builds the image with Docker Buildx and pushes it to Docker Hub as `nopenix/telegram-bridge-openwebui`.

Tags pushed:
- `latest` — every build from `main`
- `1.2.0`, `1.2`, etc. — on `v*` tag pushes
- short SHA — every build

**One-time setup for the repo maintainer:**

1. Create a Docker Hub access token: https://hub.docker.com/settings/security → New Access Token.
2. Add it to GitHub: repo → Settings → Secrets and variables → Actions → New repository secret.
   - Name: `DOCKER_HUB_TOKEN`
   - Value: the token from step 1
3. That's it. The next push to `main` will trigger the build.

To pull the published image:

```bash
docker pull nopenix/telegram-bridge-openwebui:latest
```

## Changelog

### v2.0.0

- **Per-user Open WebUI accounts** — a configured `OPENWEBUI_API_KEY_<TG_ID>` is
  used for that Telegram user; users without a personal key use the default
  `OPENWEBUI_API_KEY` account.
- **Per-user Telegram routing** — updates are routed to the handler and Open WebUI
  client associated with the sender's Telegram ID.

### v1.2.0
- **`POST /api/outbound`** endpoint on the bridge — OWUI tool proxies here instead of calling Telegram directly
- **`telegram_message_id → owui_chat_id` mapping** persisted in `sessions.json`
- **Reply routing** checks the outbound map first, so replies to agent-sent messages continue the agent's chat
- One-time migration from v1.1's flat session layout
- Optional bearer-token auth (`BRIDGE_OUTBOUND_TOKEN`) for cross-host deployments
- Companion `send_telegram` tool v2.0.0 (in `owui_send_telegram_tool.py`)

### v1.1.0

- **Auto model resolution** — env var → OWUI user default → first available
- **Streaming responses** with throttled message edits
- **Multi-message chunking** for replies over 3500 chars
- **Image (photo) support** via OWUI file upload
- **`/model [name]`** command to list and switch models
- **`/system <prompt>`** command for per-chat system prompts
- **Persistent sessions** via JSON file (`/app/data/sessions.json`)
- **HTTP `/health` endpoint** on port 8088
- **Webhook mode** via `WEBHOOK_URL` env var
- **Better error messages** showing model name and OWUI response body
- **Real healthcheck** in docker-compose (probes `/api/models`)

### v1.0.0

- Initial release: long polling, text only, single chat per user

## License

[MIT](LICENSE) — do whatever you want, no warranty.

## Acknowledgements

- [Open WebUI](https://github.com/open-webui/open-webui) — the platform this bridges to
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — the Telegram client library
- Inspired by the gap left by [Sid-Sun/openwebui-telegram](https://github.com/Sid-Sun/openwebui-telegram), which uses the OpenAI-compatible API and therefore doesn't create persistent chats in Open WebUI.

---

<div align="center">

Made with too much coffee and a stubborn refusal to switch apps mid-thought.

</div>
