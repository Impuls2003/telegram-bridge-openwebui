"""HTTP health and outbound endpoints for the Telegram bridge."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import requests

from telegram_bridge.config import (
    ALLOWED_USER_IDS,
    BRIDGE_OUTBOUND_TOKEN,
    HEALTH_PORT,
    OPENWEBUI_BASE_URL,
    OUTBOUND_LISTEN,
    OUTBOUND_PORT,
    REQUEST_TIMEOUT,
    TELEGRAM_BOT_TOKEN,
    log,
)
from telegram_bridge.i18n import normalize_language, translate
from telegram_bridge.sessions import SessionStore


# ---------------------------------------------------------------------------
# HTTP health + outbound server
# ---------------------------------------------------------------------------
class _BaseHandler(BaseHTTPRequestHandler):
    """Shared handler for both the /health and /api/outbound endpoints."""

    sessions: SessionStore

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

        if include_backlink:
            language = self.sessions.get(telegram_chat_id, "settings.language")
            if not isinstance(language, str) or language not in {"en", "ru"}:
                language = self.sessions.get(
                    telegram_chat_id, "settings.telegram_language", "en"
                )
            parts.append(
                "\n" + translate(
                    normalize_language(language),
                    "outbound.backlink",
                    url=f"{openwebui_url}/c/{chat_id}",
                )
            )
        text = "\n".join(parts).strip()

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
        self.sessions.set(
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


def start_http_server(sessions: SessionStore) -> None:
    """
    Start two HTTP listeners:
    - /health on HEALTH_PORT (default 8088) bound to all interfaces, so Docker healthchecks work.
    - /api/outbound on OUTBOUND_PORT (default 8089) bound to OUTBOUND_LISTEN (default 127.0.0.1)
      so the OWUI tool inside the same host can reach it but the internet can't.
    """
    _BaseHandler.sessions = sessions

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
