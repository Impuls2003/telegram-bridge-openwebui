"""Environment-based configuration for the Telegram bridge."""

from __future__ import annotations

import logging
import os


TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OPENWEBUI_BASE_URL: str = os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:3000").rstrip("/")
OPENWEBUI_API_KEY: str = os.environ["OPENWEBUI_API_KEY"]
DEFAULT_MODEL: str = os.environ.get("DEFAULT_MODEL", "")
DEFAULT_SYSTEM_PROMPT: str = os.environ.get("DEFAULT_SYSTEM_PROMPT", "")
ALLOWED_USER_IDS: set[int] = {
    int(user_id)
    for user_id in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if user_id.strip()
}
ADMINISTRATOR_ID: int | None = (
    int(value) if (value := os.environ.get("ADMINISTRATOR_ID", "").strip()) else None
)
CONNECTED_TOOLS: list[str] = list(
    dict.fromkeys(
        tool_id.strip()
        for tool_id in os.environ.get("CONNECTED_TOOLS", "").split(",")
        if tool_id.strip()
    )
)
LINK_TO_CHAT_IN_EVERY_MESSAGE: bool = (
    os.environ.get("LINK_TO_CHAT_IN_EVERY_MESSAGE", "true").strip().lower()
    not in {"false", "0", "no", "off"}
)
REQUEST_TIMEOUT: float = float(os.environ.get("REQUEST_TIMEOUT", "120"))
SESSIONS_FILE: str = os.environ.get("SESSIONS_FILE", "/app/data/sessions.json")
WEBHOOK_URL: str = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", "8089"))
WEBHOOK_LISTEN: str = os.environ.get("WEBHOOK_LISTEN", "0.0.0.0")
HEALTH_PORT: int = int(os.environ.get("HEALTH_PORT", "8088"))
OUTBOUND_PORT: int = int(os.environ.get("OUTBOUND_PORT", "8089"))
OUTBOUND_LISTEN: str = os.environ.get("OUTBOUND_LISTEN", "127.0.0.1")
BRIDGE_OUTBOUND_TOKEN: str = os.environ.get("BRIDGE_OUTBOUND_TOKEN", "")
STREAM_THROTTLE: float = float(os.environ.get("STREAM_THROTTLE_SECONDS", "1.0"))
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# How long after the bridge sends a message on a user's behalf we keep treating
# that chat as "current" (so a plain non-reply message continues it).
# 24h by default. Use /newchat to reset manually.
LAST_OUTBOUND_TTL_SECONDS: int = int(
    os.environ.get("LAST_OUTBOUND_TTL_SECONDS", str(24 * 3600))
)

log = logging.getLogger("owui-telegram")
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
