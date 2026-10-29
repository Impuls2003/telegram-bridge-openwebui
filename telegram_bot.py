"""
Open WebUI Telegram Bridge (v1.2.4).

Two-way Telegram bot that proxies conversations through Open WebUI's native
chat API. Replies on Telegram appear in your real Open WebUI chat history
and vice versa.

Inbound:  Telegram updates -> OWUI /api/v1/chats/new + /api/chat/completions
Outbound: OWUI tool -> bridge /api/outbound -> Telegram Bot API

The outbound path is critical for continuity: when an OWUI agent sends a
Telegram message via the tool, the bridge remembers which Telegram message
id was used. When the user replies to that message, the bridge looks up
the originating OWUI chat id and continues the conversation there.

Features:
- Model auto-resolution: env var -> user OWUI default -> first available
- Streaming responses with throttled message edits
- Multi-message chunking for long replies
- Image (photo) support via OWUI file upload
- Per-user system prompt (/system)
- Model switching at runtime (/model)
- Persistent sessions across container restarts
- Real HTTP /health endpoint for container orchestration
- Optional webhook mode (set WEBHOOK_URL)
- Optional bearer-token auth on /api/outbound
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, AsyncIterator, Optional

import httpx
import requests
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram_bridge.config import (
    ALLOWED_USER_IDS,
    BRIDGE_OUTBOUND_TOKEN,
    CONNECTED_TOOLS,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    HEALTH_PORT,
    LAST_OUTBOUND_TTL_SECONDS,
    LINK_TO_CHAT_IN_EVERY_MESSAGE,
    OPENWEBUI_API_KEY,
    OPENWEBUI_BASE_URL,
    OUTBOUND_LISTEN,
    OUTBOUND_PORT,
    REQUEST_TIMEOUT,
    SESSIONS_FILE,
    STREAM_THROTTLE,
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_LISTEN,
    WEBHOOK_PORT,
    WEBHOOK_URL,
    log,
)
from telegram_bridge.sessions import SessionStore
from telegram_bridge.openwebui import OpenWebUI, image_file_ref
from telegramify_markdown import split_entities

# Shared Open WebUI client
owui = OpenWebUI(
    OPENWEBUI_BASE_URL,
    OPENWEBUI_API_KEY,
    REQUEST_TIMEOUT,
    CONNECTED_TOOLS,
)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------
@dataclass
class ResolvedModel:
    name: str
    source: str  # one of: "env", "user-settings", "first-available", "explicit", "user setting"

    def __str__(self) -> str:
        return f"{self.name} (from {self.source})"


async def resolve_model(specific: Optional[str] = None) -> ResolvedModel:
    """
    Decide which model to use. Priority:
    1. Explicit `specific` arg (from /model command)
    2. DEFAULT_MODEL env var
    3. User's OWUI default (ui.models[0] from /api/v1/users/user/settings)
    4. First available model from /api/models
    """
    if specific:
        models = await owui.list_models()
        valid_ids = {m.get("id") for m in models}
        if specific not in valid_ids:
            log.warning("Model '%s' not in available list (proceeding anyway)", specific)
        return ResolvedModel(specific, "explicit (/model command)")

    if DEFAULT_MODEL:
        return ResolvedModel(DEFAULT_MODEL, "env DEFAULT_MODEL")

    user_default = await owui.get_user_default_model()
    if user_default:
        return ResolvedModel(user_default, "user settings (ui.models[0])")

    models = await owui.list_models()
    if models:
        first_id = models[0].get("id")
        if first_id:
            return ResolvedModel(first_id, "first from /api/models")

    raise RuntimeError(
        "No model could be resolved. Set DEFAULT_MODEL in .env, or configure "
        "a default model in Open WebUI user settings, or expose at least one model."
    )


# ---------------------------------------------------------------------------
# Persistent user store
# ---------------------------------------------------------------------------
sessions = SessionStore(SESSIONS_FILE)


async def model_for_user(user_id: int) -> ResolvedModel:
    """Return the user's selected model, resolving and saving a default on first use."""
    model_name = sessions.get(user_id, "settings.model")
    if model_name:
        return ResolvedModel(str(model_name), "user setting")

    model = await resolve_model()
    # Resolving is async, so a /model command may have saved a choice
    # while we were waiting. Keep that newer per-user choice if so.
    model_name = sessions.get(user_id, "settings.model")
    if model_name:
        return ResolvedModel(str(model_name), "user setting")

    sessions.set(user_id, "settings.model", model.name)
    log.info("Resolved default model for user %s: %s", user_id, model)
    return ResolvedModel(model.name, "user setting")


# ---------------------------------------------------------------------------
# HTTP health + outbound server
# ---------------------------------------------------------------------------
class _BaseHandler(BaseHTTPRequestHandler):
    """Shared handler for both the /health and /api/outbound endpoints."""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the default request logger; we have our own.
        return

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        if self.path == "/health":
            self._write_json(
                200,
                {
                    "status": "ok",
                    "outbound_endpoint": "ready",
                },
            )
            return
        self._write_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/api/outbound":
            self._handle_outbound()
            return
        self._write_json(404, {"error": "not found"})

    def _handle_outbound(self) -> None:
        """Receive a message from the OWUI tool, send via Telegram, remember the mapping."""
        # Auth check (only enforced when BRIDGE_OUTBOUND_TOKEN is set)
        if BRIDGE_OUTBOUND_TOKEN:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {BRIDGE_OUTBOUND_TOKEN}":
                log.warning("Rejected /api/outbound: bad/missing auth header")
                self._write_json(401, {"error": "unauthorized"})
                return

        # Parse body
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            self._write_json(400, {"error": f"invalid JSON: {e}"})
            return

        chat_id = body.get("chat_id", "").strip()
        message = body.get("message", "").strip()
        title = body.get("title", "").strip() or None
        include_backlink = bool(body.get("include_backlink", True))
        openwebui_url = body.get("openwebui_url", "").strip().rstrip("/") or OPENWEBUI_BASE_URL

        if not chat_id:
            self._write_json(400, {"error": "chat_id is required"})
            return
        if not message:
            self._write_json(400, {"error": "message is required"})
            return

        # Compose the text (same format the v1.0 tool used, for compat)
        parts: list[str] = []
        if title:
            parts.append(f"*{title}*\n")
        parts.append(message)
        if include_backlink:
            parts.append(f"\n🔗 Open conversation in Open WebUI →: {openwebui_url}/c/{chat_id}")
        text = "\n".join(parts).strip()

        # Determine the Telegram recipient chat_id.
        # Priority: explicit telegram_chat_id in body > single ALLOWED_USER_IDS entry.
        # Multi-user setups MUST pass telegram_chat_id explicitly — there's no
        # other way for the bridge to know which Telegram user the OWUI agent
        # is sending on behalf of.
        telegram_chat_id = str(body.get("telegram_chat_id", "") or "").strip()
        if not telegram_chat_id:
            if len(ALLOWED_USER_IDS) == 1:
                # Single-user deployment: fall back to the allowed user.
                telegram_chat_id = str(next(iter(ALLOWED_USER_IDS)))
                log.info(
                    "Outbound: telegram_chat_id not in body, "
                    "using sole ALLOWED_USER_IDS=%s", telegram_chat_id,
                )
            else:
                # Empty or multi-user: we have no way to pick. Bail out cleanly.
                err = (
                    "telegram_chat_id is required in the request body "
                    "(or set ALLOWED_USER_IDS to a single Telegram user id)"
                )
                log.error("Outbound rejected: %s", err)
                self._write_json(400, {"error": err})
                return

        # Send via Telegram Bot API (sync, since we run in a thread)
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                data={
                    "chat_id": telegram_chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": "true",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except Exception as e:
            log.exception("Telegram send failed")
            self._write_json(502, {"error": f"telegram request failed: {e}"})
            return

        if resp.status_code != 200:
            try:
                err = resp.json().get("description", resp.text)
            except Exception:
                err = resp.text
            log.error("Telegram API error %s: %s", resp.status_code, err)
            self._write_json(502, {"error": f"telegram: {err}"})
            return

        telegram_message_id = resp.json()["result"]["message_id"]
        # Remember the mapping so a Telegram reply routes back to the OWUI chat.
        sessions.set(
            telegram_chat_id,
            f"to_telegram.messages.{telegram_message_id}.owui_chat_id",
            chat_id,
        )
        log.info(
            "Outbound: telegram_msg_id=%s -> owui_chat_id=%s (%d chars)",
            telegram_message_id, chat_id, len(text),
        )
        self._write_json(
            200,
            {"ok": True, "telegram_message_id": telegram_message_id, "chat_id": chat_id},
        )


def start_http_server() -> None:
    """
    Start two HTTP listeners:
    - /health on HEALTH_PORT (default 8088) bound to all interfaces, so Docker healthchecks work.
    - /api/outbound on OUTBOUND_PORT (default 8089) bound to OUTBOUND_LISTEN (default 127.0.0.1)
      so the OWUI tool inside the same host can reach it but the internet can't.
    """
    # Health server — public-ish, read-only
    health_server = HTTPServer(("0.0.0.0", HEALTH_PORT), _BaseHandler)
    threading.Thread(
        target=health_server.serve_forever, daemon=True, name="health-server",
    ).start()
    log.info("Health endpoint: http://0.0.0.0:%d/health", HEALTH_PORT)

    # Outbound API — loopback by default for safety
    outbound_server = HTTPServer((OUTBOUND_LISTEN, OUTBOUND_PORT), _BaseHandler)
    threading.Thread(
        target=outbound_server.serve_forever, daemon=True, name="outbound-server",
    ).start()
    log.info(
        "Outbound API:   http://%s:%d/api/outbound (auth: %s)",
        OUTBOUND_LISTEN, OUTBOUND_PORT,
        "bearer token" if BRIDGE_OUTBOUND_TOKEN else "disabled — relies on bind address",
    )


# ---------------------------------------------------------------------------
# Authorization decorator
# ---------------------------------------------------------------------------
def authorized(func: Any) -> Any:
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        user = update.effective_user
        if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
            log.warning("Rejected unauthorized user id=%s username=%s", user.id, user.username)
            return
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Connected to your Open WebUI.\n\n"
        "• Send any message — starts a new chat.\n"
        "• Reply to one of my messages — continues that chat.\n"
        "• Send a photo with caption — image gets attached to a new chat.\n"
        "• `/newchat` — start a fresh chat.\n"
        "• `/id` — show a link to the current chat.\n"
        "• `/model [name|default]` — show or switch your model.\n"
        "• `/system [show|clear|<prompt>]` — manage your system prompt.\n",
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized
async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    sessions.set(user_id, "owui.current_chat_id", None)
    sessions.set(user_id, "from_telegram.last_message_at", None)
    await update.message.reply_text("🆕 Next message will start a fresh chat.")


@authorized
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = sessions.get(
        update.effective_user.id, "owui.current_chat_id"
    )
    if not chat_id:
        await update.message.reply_text("No active chat yet — send a message to start one.")
        return
    url = f"{OPENWEBUI_BASE_URL}/c/{chat_id}"
    await update.message.reply_text(f"Current chat: {url}", disable_web_page_preview=True)


@authorized
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    user_id = update.effective_user.id

    if not args:
        try:
            models = await owui.list_models()
            selected_model = await model_for_user(user_id)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not list models: {e}")
            return
        model_lines = "\n".join(f"  • `{m.get('id')}`" for m in models[:25])
        await update.message.reply_text(
            f"Current model: `{selected_model.name}`\n"
            f"Source: {selected_model.source}\n\n"
            f"Available models (first 25):\n{model_lines}\n\n"
            f"Use `/model <name>` to switch or `/model default` to reset.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    requested = args[0]
    try:
        if requested.lower() in {"default", "reset"}:
            sessions.set(user_id, "settings.model", None)
            default_model = await model_for_user(user_id)
            await update.message.reply_text(
                f"✅ Using the default model: `{default_model.name}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        new_model = await resolve_model(specific=requested)
        sessions.set(user_id, "settings.model", new_model.name)
        await update.message.reply_text(
            f"✅ Switched to: `{new_model.name}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Could not switch model: {e}")


@authorized
async def cmd_system(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    user_id = update.effective_user.id

    if not args or args[0].lower() == "show":
        prompt = sessions.get(user_id, "settings.system_prompt", DEFAULT_SYSTEM_PROMPT or "(none — using OWUI default behavior)")
        await update.message.reply_text(f"Current system prompt:\n\n{prompt}")
        return

    if args[0].lower() in ("clear", "reset", "none"):
        sessions.set(user_id, "settings.system_prompt", None)
        await update.message.reply_text("✅ System prompt reset to the configured default.")
        return

    prompt = " ".join(args)
    sessions.set(user_id, "settings.system_prompt", prompt)
    await update.message.reply_text(
        f"✅ System prompt set. It will be applied to your next message:\n\n{prompt}"
    )


# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------
def _resolve_target_chat_id(update: Update, user_id: int) -> Optional[str]:
    """
    Decide whether an incoming Telegram message continues the current OWUI chat.

    Priority:
    1. An explicit reply to a Telegram message mapped to an OWUI chat uses
       that chat.
    2. Otherwise, continue the user's current OWUI chat only when the previous
       incoming Telegram message was within LAST_OUTBOUND_TTL_SECONDS.
    3. If neither condition applies, the caller creates a new chat.
    """
    msg = update.message

    # ---- (1) + (2): explicit Telegram reply ---------------------------
    if msg and msg.reply_to_message:
        reply_id = msg.reply_to_message.message_id
        telegram_chat_id = update.effective_chat.id
        mapped = sessions.get(
            telegram_chat_id,
            f"to_telegram.messages.{reply_id}.owui_chat_id",
        )
        if mapped:
            log.info(
                "Routing reply via outbound map: tg_msg=%s -> owui_chat=%s",
                reply_id, mapped,
            )
            return mapped

    # Continue based on the time of the last incoming Telegram message.
    last_message_at = sessions.get(
        user_id, "from_telegram.last_message_at"
    )
    current_chat_id = sessions.get(user_id, "owui.current_chat_id")
    if last_message_at is not None and current_chat_id:
        age = msg.date.timestamp() - float(last_message_at)
        if 0 <= age <= LAST_OUTBOUND_TTL_SECONDS:
            log.info(
                "Continuing current OWUI chat after Telegram message (age=%ds): %s",
                int(age), current_chat_id,
            )
            return current_chat_id

    if last_message_at is not None:
        log.info(
            "Starting a new OWUI chat (last Telegram message age=%ds)",
            int(msg.date.timestamp() - float(last_message_at)),
        )

    return None


async def _stream_reply_to_telegram(
    update: Update, chat_id: str, stream: AsyncIterator[str]
) -> None:
    """Edit a placeholder message as tokens arrive, then chunk if needed.

    The Telegram message id is not needed for conversation continuity; that
    is tracked by the user's current OWUI chat and incoming-message timestamp.
    """
    placeholder = await update.message.reply_text("…")
    accumulated = ""
    last_edit = 0.0

    async for chunk in stream:
        accumulated += chunk
        now = time.monotonic()
        if now - last_edit >= STREAM_THROTTLE:
            preview = accumulated if len(accumulated) <= 4000 else accumulated[-4000:]
            try:
                await placeholder.edit_text(preview + " …")
            except Exception as e:
                log.warning("Could not update Telegram response preview: %s", e)
            last_edit = now

    if not accumulated.strip():
        log.warning(
            "Open WebUI stream contained no text for chat %s; checking saved chat",
            chat_id,
        )
        try:
            accumulated = await owui.get_latest_assistant_text(chat_id)
        except Exception as e:
            log.warning("Could not retrieve saved Open WebUI response: %s", e)

    if not accumulated.strip():
        log.error("No assistant text was available for Telegram (chat %s)", chat_id)
        await placeholder.edit_text("⚠️ Open WebUI returned no readable response text.")
        chunks = []
    else:
        chunks = [
            chunk_text
            for chunk_text, _ in split_entities(
                accumulated, [], max_utf16_len=3500
            )
        ]
        try:
            await placeholder.edit_text(chunks[0])
        except Exception as e:
            log.warning("Could not replace Telegram response placeholder: %s", e)
            await update.message.reply_text(chunks[0])

    if chunks and len(chunks) > 1:
        for extra in chunks[1:]:
            await update.message.reply_text(extra)

    if LINK_TO_CHAT_IN_EVERY_MESSAGE:
        chat_url = f"{OPENWEBUI_BASE_URL}/c/{chat_id}"
        await update.message.reply_text(
            f"🔗 {chat_url}", disable_web_page_preview=True,
        )


@authorized
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = update.message.text or ""
    if not text:
        return

    system_prompt = sessions.get(user.id, "settings.system_prompt", DEFAULT_SYSTEM_PROMPT or None)
    target_chat_id = _resolve_target_chat_id(update, user.id)
    sessions.set(
        user.id, "from_telegram.last_message_at", update.message.date.timestamp()
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    model: Optional[ResolvedModel] = None
    try:
        model = await model_for_user(user.id)
        if target_chat_id:
            chat_id = target_chat_id
            sessions.set(user.id, "owui.current_chat_id", chat_id)
            parent_message_id = await owui.get_current_message_id(chat_id)
            if not parent_message_id:
                raise RuntimeError(
                    f"Open WebUI chat {chat_id} has no active message to continue"
                )
            assistant_message_id = str(uuid.uuid4())
            user_message = {
                "id": str(uuid.uuid4()),
                "parentId": parent_message_id,
                "childrenIds": [assistant_message_id],
                "role": "user",
                "content": text,
                "models": [model.name],
                "timestamp": int(time.time()),
            }
            stream = await owui.complete(
                chat_id, model.name, text,
                system_prompt=system_prompt,
                assistant_message_id=assistant_message_id,
                user_message=user_message,
                stream=True,
            )
        else:
            sessions.set(user.id, "owui.current_chat_id", None)
            chat_id, assistant_message_id, user_message = await owui.new_chat(
                model.name, text
            )
            sessions.set(user.id, "owui.current_chat_id", chat_id)
            stream = await owui.complete(
                chat_id, model.name, text,
                system_prompt=system_prompt,
                assistant_message_id=assistant_message_id,
                user_message=user_message,
                stream=True,
            )
        await _stream_reply_to_telegram(update, chat_id, stream)
    except httpx.HTTPStatusError as e:
        log.exception("OWUI API error")
        snippet = (e.response.text[:300] if e.response is not None else str(e))
        model_name = model.name if model else "unresolved"
        await update.message.reply_text(
            f"❌ Open WebUI returned `{e.response.status_code}`\n"
            f"Model: `{model_name}`\n"
            f"Response: `{snippet}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        log.exception("Unhandled error")
        model_name = model.name if model else "unresolved"
        await update.message.reply_text(
            f"❌ {type(e).__name__}: {e}\nModel: `{model_name}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return


@authorized
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download the largest photo, upload to OWUI, attach to a new chat."""
    user = update.effective_user
    caption = update.message.caption or "What's in this image?"
    system_prompt = sessions.get(user.id, "settings.system_prompt", DEFAULT_SYSTEM_PROMPT or None)
    target_chat_id = _resolve_target_chat_id(update, user.id)
    sessions.set(
        user.id, "from_telegram.last_message_at", update.message.date.timestamp()
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        model = await model_for_user(user.id)
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        file_id = await owui.upload_file(file_bytes, f"tg-{photo.file_unique_id}.jpg")
        log.info("Uploaded photo, OWUI file_id=%s (%d bytes)", file_id, len(file_bytes))
        image_data_url = "data:image/jpeg;base64," + base64.b64encode(file_bytes).decode("ascii")

        if target_chat_id:
            chat_id = target_chat_id
            sessions.set(user.id, "owui.current_chat_id", chat_id)
            parent_message_id = await owui.get_current_message_id(chat_id)
            if not parent_message_id:
                raise RuntimeError(
                    f"Open WebUI chat {chat_id} has no active message to continue"
                )
            assistant_message_id = str(uuid.uuid4())
            user_message = {
                "id": str(uuid.uuid4()),
                "parentId": parent_message_id,
                "childrenIds": [assistant_message_id],
                "role": "user",
                "content": caption,
                "models": [model.name],
                "timestamp": int(time.time()),
                "files": [image_file_ref(file_id)],
            }
        else:
            sessions.set(user.id, "owui.current_chat_id", None)
            chat_id, assistant_message_id, user_message = await owui.new_chat(
                model.name, caption, file_ids=[file_id],
            )
            sessions.set(user.id, "owui.current_chat_id", chat_id)

        stream = await owui.complete(
            chat_id, model.name, caption,
            system_prompt=system_prompt,
            file_ids=[file_id],
            assistant_message_id=assistant_message_id,
            user_message=user_message,
            image_data_url=image_data_url,
            stream=True,
        )
        await _stream_reply_to_telegram(update, chat_id, stream)
    except Exception as e:
        log.exception("Photo handling failed")
        await update.message.reply_text(
            f"❌ Failed to process image: {type(e).__name__}: {e}",
        )


# ---------------------------------------------------------------------------
# Lifecycle hooks & entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not OPENWEBUI_API_KEY:
        raise SystemExit("OPENWEBUI_API_KEY is required")

    log.info("Starting Telegram → Open WebUI bridge (v1.2.4)")
    log.info("Open WebUI:    %s", OPENWEBUI_BASE_URL)
    log.info("Sessions file: %s", SESSIONS_FILE)
    
    log.info(
        "Allowed users: %s",
        sorted(ALLOWED_USER_IDS) or "(all — set ALLOWED_USER_IDS!)",
    )

    start_http_server()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("newchat", cmd_newchat))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("system", cmd_system))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if WEBHOOK_URL:
        log.info("Mode: webhook (%s)", WEBHOOK_URL)
        app.run_webhook(
            listen=WEBHOOK_LISTEN,
            port=WEBHOOK_PORT,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Mode: polling (set WEBHOOK_URL to switch)")
        app.run_polling(
            allowed_updates=Update.ALL_TYPES,
            # Drop any pending updates AND force Telegram to release the
            # previous bot's polling session. Without this, fast restarts
            # trigger a 409 Conflict that can persist for several minutes.
            drop_pending_updates=True,
        )


if __name__ == "__main__":
    try:
        main()
    finally:
        asyncio.run(owui.aclose())
