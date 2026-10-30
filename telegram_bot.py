"""Application entry point for the Open WebUI Telegram bridge."""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram_bridge.config import (
    ALLOWED_USER_IDS,
    CONNECTED_TOOLS,
    OPENWEBUI_API_KEY,
    OPENWEBUI_API_KEYS,
    OPENWEBUI_BASE_URL,
    REQUEST_TIMEOUT,
    SESSIONS_FILE,
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_LISTEN,
    WEBHOOK_PORT,
    WEBHOOK_URL,
    log,
)
from telegram_bridge.sessions import SessionStore
from telegram_bridge.openwebui import OpenWebUI
from telegram_bridge.http_server import start_http_server
from telegram_bridge.telegram_handlers import TelegramHandlers
from telegram_bridge.telegram_router import TelegramHandlerRouter

sessions = SessionStore(SESSIONS_FILE)
owui_clients: list[OpenWebUI] = []
shared_owui = OpenWebUI(
    OPENWEBUI_BASE_URL,
    OPENWEBUI_API_KEY,
    REQUEST_TIMEOUT,
    CONNECTED_TOOLS,
)
owui_clients.append(shared_owui)
default_handlers = TelegramHandlers(shared_owui, sessions)

owui_by_user_id = {
    user_id: OpenWebUI(
        OPENWEBUI_BASE_URL,
        api_key,
        REQUEST_TIMEOUT,
        CONNECTED_TOOLS,
    )
    for user_id, api_key in OPENWEBUI_API_KEYS.items()
}
owui_clients.extend(owui_by_user_id.values())
handlers_by_user_id = {
    user_id: TelegramHandlers(owui, sessions)
    for user_id, owui in owui_by_user_id.items()
}

handlers = TelegramHandlerRouter(
    user_handlers=handlers_by_user_id,
    sessions=sessions,
    default_handler=default_handlers,
)


# ---------------------------------------------------------------------------
# Lifecycle hooks & entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is required")
    if not OPENWEBUI_API_KEY:
        raise SystemExit("OPENWEBUI_API_KEY is required")

    log.info("Starting Telegram → Open WebUI bridge (v2.0.0)")
    log.info("Open WebUI:    %s", OPENWEBUI_BASE_URL)
    log.info("Sessions file: %s", SESSIONS_FILE)
    user_ids = sorted(ALLOWED_USER_IDS) or "(all — set ALLOWED_USER_IDS!)"
    log.info("Allowed users: %s", user_ids)

    start_http_server(sessions)

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("newchat", handlers.cmd_newchat))
    app.add_handler(CommandHandler("id", handlers.cmd_id))
    app.add_handler(CommandHandler("model", handlers.cmd_model))
    app.add_handler(CommandHandler("system", handlers.cmd_system))
    app.add_handler(CommandHandler("language", handlers.cmd_language))
    app.add_handler(CallbackQueryHandler(handlers.on_callback_query, pattern=r"^menu:"))
    app.add_handler(MessageHandler(filters.PHOTO, handlers.on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_message))

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


async def close_owui_clients() -> None:
    await asyncio.gather(*(client.aclose() for client in owui_clients))


if __name__ == "__main__":
    try:
        main()
    finally:
        asyncio.run(close_owui_clients())
