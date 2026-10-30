# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-10-29

### Added
- **Per-user Open WebUI API keys.** A Telegram user with a matching
  `OPENWEBUI_API_KEY_<TG_ID>` uses that Open WebUI account. Users without a
  personal key use the default `OPENWEBUI_API_KEY` account.
- **Per-user Telegram update routing** through a dedicated handler router.

### Changed
- **Version banner** bumped to v2.0.0.

## [1.2.2] - 2026-06-27

### Fixed
- **409 Conflict on fast restart.** When the bot restarts within ~30s of being killed
  (or even minutes later, depending on Telegram's session cleanup), the new `getUpdates`
  call gets 409 because the previous session is still alive on Telegram's side.
  Fix: pass `drop_pending_updates=True` to `run_polling()` — this tells Telegram
  to drop pending updates AND reset the polling session, eliminating the 409.
- **Bumped version banner to v1.2.2.**

## [1.2.1] - 2026-06-27

### Fixed
- **Empty error on user-settings fetch failure.** `Could not fetch user settings: ` was
  logged with an empty exception string because `httpx` exceptions with no args
  stringify to `''`. Replaced with typed exception handlers (`TimeoutException`,
  `HTTPError`, generic `Exception`) that log `type(e).__name__` and the actual
  message — never empty again.
- **Two-minute startup hang on OWUI unreachable.** The user-settings request used
  the default `REQUEST_TIMEOUT` (120s). If OWUI was unreachable or restarting, the
  bot would hang for two minutes before logging anything useful. Now uses a tight
  5-second timeout for this single call.
- **Container crash-loops on resolution failure.** `post_init` re-raised when
  model resolution failed, which caused the container to exit and Docker to
  restart-loop it forever. Now logs the error and starts anyway; `/health`
  returns 503 until a model becomes resolvable, and incoming messages get a
  clear error instead of a silent crash.

## [1.2.0] - 2026-06-27

### Added
- **`POST /api/outbound` HTTP endpoint** on the bridge (default port 8089) that accepts
  outbound messages from the OWUI `send_telegram` tool. The bridge forwards to Telegram
  and **stores the `telegram_message_id → owui_chat_id` mapping** in its persistent
  session file.
- **Continuity for agent-initiated chats**: when an OWUI agent sends a Telegram message
  via the bridged tool, the bridge remembers the mapping. When you reply to that
  Telegram message, the bridge looks up the originating OWUI chat and appends your
  reply there — so the conversation continues in both directions regardless of who
  started it.
- **`OUTBOUND_PORT`**, **`OUTBOUND_LISTEN`**, **`BRIDGE_OUTBOUND_TOKEN`** env vars
  for configuring the outbound API.
- **Auto-save of session-file migrations** on boot.

### Changed
- **`SessionStore`** now keeps two top-level keys: `sessions` (per-user chat pointer
  and system prompt) and `outbound` (Telegram message id → OWUI chat id map). One-time
  migration from v1.1's flat layout runs on first boot.
- **HTTP server refactor**: now serves both `/health` (port 8088, all interfaces) and
  `/api/outbound` (port 8089, default bound to `127.0.0.1`) from the same `BaseHTTPRequestHandler`.
- **`docker-compose.yml`**: explicitly does NOT expose port 8089 to the host. The OWUI
  tool reaches the bridge via `host.docker.internal:8089` from inside its container.
- **`requirements.txt`**: added `requests==2.32.3` for the sync outbound HTTP call.

### Security
- The outbound API defaults to `127.0.0.1` bind — only reachable from inside the bridge
  container, or from the OWUI container via `host.docker.internal:8089`.
- Optional `BRIDGE_OUTBOUND_TOKEN` enables bearer-token auth on `/api/outbound` for
  deployments where the bridge and OWUI live on different hosts.

### Companion change: OWUI `send_telegram` tool v2.0.0
The OWUI tool now lives in `owui_send_telegram_tool.py`. It defaults to **bridge mode**
(`bridge_url` valve) and falls back to direct Telegram Bot API calls if the bridge URL
is empty. Drop the new file into Workspace → Tools in Open WebUI, replacing v1.0.

## [1.1.0] - 2026-06-27

### Added
- **Auto model resolution**: env var → OWUI user default (`/api/v1/users/user/settings` → `ui.models[0]`) → first available from `/api/models`. Fails loudly at startup if none can be resolved.
- **Streaming responses**: replies stream into Telegram via `editMessageText`, throttled by `STREAM_THROTTLE_SECONDS` to respect rate limits.
- **Multi-message chunking**: replies over 3500 chars are split at paragraph / line / word boundaries.
- **Image (photo) support**: photos uploaded to OWUI via `/api/v1/files/`, attached to a new chat with caption as the user prompt.
- **`/model [name]`** command: lists available models and switches the active model mid-session.
- **`/system <show|clear|prompt>`** command: per-chat system prompt that persists across sessions.
- **Persistent session storage**: JSON file at `SESSIONS_FILE` (default `/app/data/sessions.json`), mounted via `./data:/app/data` in docker-compose.
- **HTTP `/health` endpoint** on `HEALTH_PORT` (default 8088), returns `200` once the bot is fully initialized with the resolved model name.
- **Webhook mode**: when `WEBHOOK_URL` is set, switches from long polling to Telegram webhooks.
- **Real OWUI healthcheck** in `docker-compose.yml`: probes `/api/models` rather than just checking env-var presence.
- **Better error messages**: Telegram error replies now include the model name and the first 300 chars of the OWUI error response body.

### Changed
- `docker-compose.yml`: added persistent `./data` volume mount, exposed `/health` port, image tag now versioned (`v1.1.0`).
- `Dockerfile`: pre-creates `/app/data` and hands ownership to the unprivileged `bot` user.
- `.env.example`: rewritten with grouped sections and inline guidance.

## [1.0.0] - 2026-06-27

### Added
- Initial release.
- Long-polling Telegram bridge using `python-telegram-bot` 21.x.
- Native Open WebUI chat creation via `/api/v1/chats/new` so chats persist in the user's sidebar.
- Per-user session bookkeeping (in-memory only).
- `/start`, `/newchat`, `/id`, `/model` commands.
- Allowlist via `ALLOWED_USER_IDS`.

[2.0.0]: #200---2026-10-29
[1.2.0]: #120---2026-06-27
[1.1.0]: #110---2026-06-27
[1.0.0]: #100---2026-06-27
