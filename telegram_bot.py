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
from pathlib import Path
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def chunk_message(text: str, max_len: int = 3500) -> list[str]:
    """
    Split long text at paragraph / line / word boundaries.
    Telegram's message cap is 4096 chars; we leave headroom for the backlink.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at < max_len // 2:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at < max_len // 2:
            split_at = max_len
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def image_file_ref(file_id: str) -> dict[str, str]:
    """Build an Open WebUI chat reference for an uploaded image."""
    # Some Open WebUI versions return the content URL in `id` rather than a
    # bare file id. Reduce either form to the actual id before building URLs.
    file_id = str(file_id).strip()
    if "/api/v1/files/" in file_id:
        file_id = file_id.rsplit("/api/v1/files/", 1)[1].split("/", 1)[0]
    return {
        "type": "image",
        "id": file_id,
        # Open WebUI's chat UI builds /api/v1/files/{url}/content itself.
        "url": file_id,
        "content_type": "image/jpeg",
    }


# ---------------------------------------------------------------------------
# Open WebUI client
# ---------------------------------------------------------------------------
class OpenWebUI:
    """Async client for Open WebUI's chat & admin APIs."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def healthcheck(self) -> bool:
        try:
            r = await self._client.get("/api/models", timeout=5.0)
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[dict[str, Any]]:
        """Return available models (handles both flat and wrapped response shapes)."""
        r = await self._client.get("/api/models")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data, list):
            return data
        return []

    async def get_user_default_model(self) -> Optional[str]:
        """Fetch the authenticated user's UI default model."""
        try:
            # Tight timeout so this can't block startup for two minutes when
            # OWUI is unreachable.
            r = await self._client.get(
                "/api/v1/users/user/settings",
                timeout=httpx.Timeout(5.0, connect=5.0),
            )
            if r.status_code != 200:
                log.warning(
                    "User-settings endpoint returned %s: %s",
                    r.status_code, r.text[:200],
                )
                return None
            data = r.json()
            models = data.get("ui", {}).get("models", [])
            if models and models[0]:
                return models[0]
            log.warning("User-settings endpoint returned no model in ui.models")
            return None
        except httpx.TimeoutException as e:
            log.warning("User-settings request timed out: %s", repr(e))
            return None
        except httpx.HTTPError as e:
            log.warning("User-settings HTTP error: %s: %s", type(e).__name__, e)
            return None
        except Exception as e:
            # Last-resort: log the full exception so it's never silently empty.
            log.warning(
                "User-settings fetch failed unexpectedly: %s: %s",
                type(e).__name__, e or repr(e),
            )
            return None

    async def new_chat(
        self,
        model: str,
        content: str,
        file_ids: Optional[list[str]] = None,
    ) -> tuple[str, str, dict[str, Any]]:
        """
        Create a chat with a user message and empty assistant placeholder.
        Returns (server_chat_id, assistant_message_id, user_message).
        """
        chat_id = str(uuid.uuid4())
        user_msg_id = str(uuid.uuid4())
        assistant_msg_id = str(uuid.uuid4())
        now = int(time.time())

        user_msg: dict[str, Any] = {
            "id": user_msg_id,
            "parentId": None,
            "childrenIds": [assistant_msg_id],
            "role": "user",
            "content": content,
            "models": [model] if model else [],
            "timestamp": now,
        }
        image_files = [image_file_ref(fid) for fid in (file_ids or [])]
        if image_files:
            user_msg["files"] = image_files

        history: dict[str, dict[str, Any]] = {
            user_msg_id: user_msg,
            assistant_msg_id: {
                "id": assistant_msg_id,
                "parentId": user_msg_id,
                "childrenIds": [],
                "role": "assistant",
                "content": "",
                "model": model,
                "modelName": model,
                "modelIdx": 0,
                "done": False,
                "timestamp": now,
            },
        }

        payload: dict[str, Any] = {
            "chat": {
                "id": chat_id,
                "title": content[:50] + ("…" if len(content) > 50 else ""),
                "models": [model] if model else [],
                "params": {},
                "files": image_files,
                "history": {"messages": history, "currentId": assistant_msg_id},
                "messages": [
                    {
                        "id": user_msg_id,
                        "role": "user",
                        "content": content,
                        "models": [model] if model else [],
                        "childrenIds": [assistant_msg_id],
                        **({"files": image_files} if image_files else {}),
                    },
                    {
                        "id": assistant_msg_id,
                        "role": "assistant",
                        "content": "",
                        "parentId": user_msg_id,
                        "childrenIds": [],
                        "model": model,
                        "modelName": model,
                        "modelIdx": 0,
                        "done": False,
                    },
                ],
                "tags": [],
                "timestamp": now,
            },
        }
        r = await self._client.post("/api/v1/chats/new", json=payload)
        r.raise_for_status()
        response_data = r.json()
        server_chat_id = response_data.get("id")
        if not server_chat_id:
            server_chat_id = (response_data.get("chat") or {}).get("id")
        if not server_chat_id:
            raise RuntimeError(
                "Open WebUI created a chat but returned no chat id; "
                "cannot safely start completion"
            )
        if server_chat_id != chat_id:
            log.info(
                "Open WebUI assigned chat id %s (requested %s)",
                server_chat_id, chat_id,
            )
        return str(server_chat_id), assistant_msg_id, user_msg

    async def complete(
        self,
        chat_id: str,
        model: str,
        content: str,
        system_prompt: Optional[str] = None,
        file_ids: Optional[list[str]] = None,
        assistant_message_id: Optional[str] = None,
        user_message: Optional[dict[str, Any]] = None,
        image_data_url: Optional[str] = None,
        stream: bool = False,
    ) -> Any:
        """
        Run a completion. If stream=True, returns an async iterator yielding text chunks.
        Otherwise returns the full assistant text.
        """
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        user_content: Any = content
        if image_data_url:
            user_content = [
                {"type": "text", "text": content},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
        messages.append({"role": "user", "content": user_content})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "chat_id": chat_id,
            "stream": stream,
            "metadata": {"chat_id": chat_id},
        }
        if assistant_message_id:
            payload["id"] = assistant_message_id
        if user_message:
            payload["user_message"] = user_message
        if file_ids:
            payload["files"] = [image_file_ref(fid) for fid in file_ids]
        if CONNECTED_TOOLS:
            payload["tool_ids"] = CONNECTED_TOOLS

        if stream:
            return self._stream(payload)
        r = await self._client.post("/api/chat/completions", json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        async with self._client.stream("POST", "/api/chat/completions", json=payload) as resp:
            if resp.is_error:
                # Read the body so the existing HTTP error handler can show it.
                await resp.aread()
            resp.raise_for_status()
            buffer = ""
            async for chunk in resp.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
            # A final SSE frame may not end with a newline, so it remains in
            # the buffer after aiter_text() finishes.
            line = buffer.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    try:
                        obj = json.loads(data)
                        delta = obj["choices"][0].get("delta", {}).get("content", "")
                        if isinstance(delta, str) and delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass

    async def upload_file(self, file_bytes: bytes, filename: str) -> str:
        """Upload a binary file to OWUI and return its file id."""
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {OPENWEBUI_API_KEY}"},
            timeout=REQUEST_TIMEOUT,
        ) as c:
            files = {"file": (filename, file_bytes)}
            r = await c.post("/api/v1/files/", files=files)
            r.raise_for_status()
            file_id = str(r.json()["id"])
            if "/api/v1/files/" in file_id:
                file_id = file_id.rsplit("/api/v1/files/", 1)[1].split("/", 1)[0]
            return file_id

    async def get_latest_assistant_text(self, chat_id: str) -> str:
        """Read the latest assistant text from a saved chat."""
        r = await self._client.get(f"/api/v1/chats/{chat_id}")
        r.raise_for_status()
        data = r.json()
        chat = data.get("chat", data)
        history = chat.get("history", {})
        history_messages = history.get("messages", {})

        current_id = history.get("currentId")
        if current_id and isinstance(history_messages, dict):
            current = history_messages.get(current_id, {})
            if current.get("role") == "assistant" and isinstance(current.get("content"), str):
                if current["content"].strip():
                    return current["content"]

        messages = chat.get("messages", [])
        if isinstance(messages, list):
            for message in reversed(messages):
                if message.get("role") == "assistant":
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content
        return ""

    async def get_current_message_id(self, chat_id: str) -> Optional[str]:
        """Return the active leaf message ID to use as the next turn's parent."""
        r = await self._client.get(f"/api/v1/chats/{chat_id}")
        r.raise_for_status()
        data = r.json()
        chat = data.get("chat", data)
        history = chat.get("history", {})
        current_id = history.get("currentId")
        if current_id:
            return str(current_id)

        messages = history.get("messages", {})
        if isinstance(messages, dict):
            assistant_messages = [
                message for message in messages.values()
                if isinstance(message, dict)
                and message.get("role") == "assistant"
                and message.get("id")
            ]
            if assistant_messages:
                latest = max(assistant_messages, key=lambda item: item.get("timestamp", 0))
                return str(latest["id"])

        messages = chat.get("messages", [])
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "assistant" and message.get("id"):
                    return str(message["id"])
        return None


owui = OpenWebUI(OPENWEBUI_BASE_URL, OPENWEBUI_API_KEY)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------
@dataclass
class ResolvedModel:
    name: str
    source: str  # one of: "env", "user-settings", "first-available", "explicit"

    def __str__(self) -> str:
        return f"{self.name} (from {self.source})"


_current_model: Optional[ResolvedModel] = None
_current_model_lock = threading.Lock()


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


def current_model() -> ResolvedModel:
    if _current_model is None:
        raise RuntimeError(
            "No model resolved. Set DEFAULT_MODEL in .env, configure a default "
            "in Open WebUI user settings, or ensure /api/models returns at "
            "least one entry."
        )
    return _current_model


def set_current_model(model: ResolvedModel) -> None:
    global _current_model
    with _current_model_lock:
        _current_model = model


# ---------------------------------------------------------------------------
# Persistent session store
# ---------------------------------------------------------------------------
@dataclass
class UserSession:
    chat_id: Optional[str] = None
    system_prompt: Optional[str] = None


class SessionStoreOLD:
    """
    JSON-backed per-user session storage with atomic writes.

    Stores two kinds of state:
    - Per-user session: telegram_user_id -> { chat_id, system_prompt }
    - Outbound message map: telegram_message_id -> owui_chat_id
      (used to route Telegram replies back to the originating OWUI chat)
    """

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = self._load()
        # Ensure subkeys exist
        self._data.setdefault("sessions", {})
        self._data.setdefault("outbound", {})

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"sessions": {}, "outbound": {}}
        try:
            data = json.loads(self.path.read_text())
        except Exception as e:
            log.warning("Could not load sessions from %s: %s", self.path, e)
            return {"sessions": {}, "outbound": {}}

        # One-time migration from v1.0/v1.1 format (root keys were user ids)
        # to v1.2 format (everything nested under "sessions" / "outbound").
        if not isinstance(data, dict):
            return {"sessions": {}, "outbound": {}}
        legacy = {
            k: v for k, v in data.items()
            if k not in ("sessions", "outbound")
            and isinstance(v, dict)
            and not v.get("outbound")  # avoid false positives
        }
        if legacy:
            log.info("Migrating %d legacy session entries to v1.2 layout", len(legacy))
            data.setdefault("sessions", {}).update(
                {uid: {"chat_id": e.get("chat_id"), "system_prompt": e.get("system_prompt")}
                 for uid, e in legacy.items()}
            )
            for k in legacy:
                data.pop(k, None)
        data.setdefault("sessions", {})
        data.setdefault("outbound", {})
        # Persist migration immediately so the file is clean from boot 1.
        if legacy:
            try:
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2))
                tmp.replace(self.path)
            except Exception as e:
                log.warning("Could not persist migration: %s", e)
        return data

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            tmp.replace(self.path)
        except Exception as e:
            log.warning("Could not save sessions: %s", e)

    # ---- user sessions ----------------------------------------------------
    def get(self, user_id: int) -> UserSession:
        with self._lock:
            entry = self._data["sessions"].setdefault(str(user_id), {})
            return UserSession(
                chat_id=entry.get("chat_id"),
                system_prompt=entry.get("system_prompt") or DEFAULT_SYSTEM_PROMPT or None,
            )

    def update(self, user_id: int, *, chat_id: Optional[str] = None,
               system_prompt: Optional[str] = None,
               clear_system_prompt: bool = False) -> UserSession:
        with self._lock:
            entry = self._data["sessions"].setdefault(str(user_id), {})
            if chat_id is not None:
                entry["chat_id"] = chat_id
            if clear_system_prompt:
                entry["system_prompt"] = None
            elif system_prompt is not None:
                entry["system_prompt"] = system_prompt
            self._save()
            return UserSession(
                chat_id=entry.get("chat_id"),
                system_prompt=entry.get("system_prompt"),
            )

    # ---- outbound message map --------------------------------------------
    def remember_outbound(self, telegram_message_id: int, owui_chat_id: str) -> None:
        """Record that a Telegram message id was sent on behalf of an OWUI chat."""
        with self._lock:
            self._data["outbound"][str(telegram_message_id)] = owui_chat_id
            self._save()

    def lookup_outbound(self, telegram_message_id: int) -> Optional[str]:
        """Return the OWUI chat id that produced this Telegram message, if any."""
        with self._lock:
            return self._data["outbound"].get(str(telegram_message_id))

    def remember_last_outbound_for(
        self, user_id: int, telegram_message_id: int, owui_chat_id: str
    ) -> None:
        """Stamp the user's last-outbound pointer (with current time)."""
        with self._lock:
            self._data.setdefault("last_outbound", {})
            self._data["last_outbound"][str(user_id)] = {
                "ts": time.time(),
                "telegram_message_id": telegram_message_id,
                "owui_chat_id": owui_chat_id,
            }
            self._save()

    def get_last_outbound_for(
        self, user_id: int, ttl_seconds: int
    ) -> Optional[dict]:
        """Return the user's most recent outbound message if within TTL, else None."""
        with self._lock:
            entry = self._data.get("last_outbound", {}).get(str(user_id))
            if not entry:
                return None
            age = time.time() - float(entry.get("ts", 0))
            if age > ttl_seconds:
                return None
            return entry

    def clear_last_outbound_for(self, user_id: int) -> None:
        """Used by /newchat to force the next message to start a fresh chat."""
        with self._lock:
            self._data.get("last_outbound", {}).pop(str(user_id), None)
            self._save()


sessions = SessionStoreOLD(SESSIONS_FILE)
sessions_new = SessionStore(SESSIONS_FILE + "new.json")


def system_prompt_for_user(user_id: int) -> Optional[str]:
    """Return the user's system prompt or the configured default prompt."""
    prompt = sessions_new.get(user_id, "settings.system_prompt")
    if prompt is None:
        return DEFAULT_SYSTEM_PROMPT or None
    return str(prompt)


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
            ready = _current_model is not None
            self._write_json(
                200 if ready else 503,
                {
                    "status": "ok" if ready else "starting",
                    "model": str(_current_model) if _current_model else None,
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
        # Remember the mapping so a Telegram reply routes back to the OWUI chat
        sessions.remember_outbound(telegram_message_id, chat_id)
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
        "• /newchat — start a fresh chat.\n"
        "• /id — show a link to the current chat.\n"
        "• /model [name] — show or switch the active model.\n"
        "• /system [show|clear|<prompt>] — manage your system prompt.\n"
    )


@authorized
async def cmd_newchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    sessions_new.set(user_id, "owui.current_chat_id", None)
    sessions_new.set(user_id, "from_telegram.last_message_at", None)
    await update.message.reply_text("🆕 Next message will start a fresh chat.")


@authorized
async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = sessions_new.get(
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

    if not args:
        try:
            models = await owui.list_models()
        except Exception as e:
            await update.message.reply_text(f"❌ Could not list models: {e}")
            return
        model_lines = "\n".join(f"  • `{m.get('id')}`" for m in models[:25])
        await update.message.reply_text(
            f"Current model: `{current_model().name}`\n"
            f"Source: {current_model().source}\n\n"
            f"Available models (first 25):\n{model_lines}\n\n"
            f"Use `/model <name>` to switch.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    requested = args[0]
    try:
        new_model = await resolve_model(specific=requested)
        set_current_model(new_model)
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
        prompt = system_prompt_for_user(user_id) or "(none — using OWUI default behavior)"
        await update.message.reply_text(f"Current system prompt:\n\n{prompt}")
        return

    if args[0].lower() in ("clear", "reset", "none"):
        sessions_new.set(user_id, "settings.system_prompt", None)
        await update.message.reply_text("✅ System prompt reset to the configured default.")
        return

    prompt = " ".join(args)
    sessions_new.set(user_id, "settings.system_prompt", prompt)
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
        mapped = sessions.lookup_outbound(reply_id)
        if mapped:
            log.info(
                "Routing reply via outbound map: tg_msg=%s -> owui_chat=%s",
                reply_id, mapped,
            )
            return mapped

    # Continue based on the time of the last incoming Telegram message.
    last_message_at = sessions_new.get(
        user_id, "from_telegram.last_message_at"
    )
    current_chat_id = sessions_new.get(user_id, "owui.current_chat_id")
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
        chunks = chunk_message(accumulated)
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

    system_prompt = system_prompt_for_user(user.id)
    model = current_model()
    target_chat_id = _resolve_target_chat_id(update, user.id)
    sessions_new.set(
        user.id, "from_telegram.last_message_at", update.message.date.timestamp()
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        if target_chat_id:
            chat_id = target_chat_id
            sessions_new.set(user.id, "owui.current_chat_id", chat_id)
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
            sessions_new.set(user.id, "owui.current_chat_id", None)
            chat_id, assistant_message_id, user_message = await owui.new_chat(
                model.name, text
            )
            sessions_new.set(user.id, "owui.current_chat_id", chat_id)
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
        await update.message.reply_text(
            f"❌ Open WebUI returned `{e.response.status_code}`\n"
            f"Model: `{model.name}`\n"
            f"Response: `{snippet}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    except Exception as e:
        log.exception("Unhandled error")
        await update.message.reply_text(
            f"❌ {type(e).__name__}: {e}\nModel: `{model.name}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return


@authorized
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download the largest photo, upload to OWUI, attach to a new chat."""
    user = update.effective_user
    caption = update.message.caption or "What's in this image?"
    model = current_model()
    system_prompt = system_prompt_for_user(user.id)
    target_chat_id = _resolve_target_chat_id(update, user.id)
    sessions_new.set(
        user.id, "from_telegram.last_message_at", update.message.date.timestamp()
    )

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        file_bytes = bytes(await tg_file.download_as_bytearray())
        file_id = await owui.upload_file(file_bytes, f"tg-{photo.file_unique_id}.jpg")
        log.info("Uploaded photo, OWUI file_id=%s (%d bytes)", file_id, len(file_bytes))
        image_data_url = "data:image/jpeg;base64," + base64.b64encode(file_bytes).decode("ascii")

        if target_chat_id:
            chat_id = target_chat_id
            sessions_new.set(user.id, "owui.current_chat_id", chat_id)
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
            sessions_new.set(user.id, "owui.current_chat_id", None)
            chat_id, assistant_message_id, user_message = await owui.new_chat(
                model.name, caption, file_ids=[file_id],
            )
            sessions_new.set(user.id, "owui.current_chat_id", chat_id)

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
async def post_init(app: Application) -> None:
    """
    Resolve the model before the bot starts processing messages.

    Resilient to transient failures: if user-settings lookup hangs/fails,
    the resolver falls through to first-available or env var. Only raises
    if absolutely nothing can be resolved, and even then we log + exit
    cleanly rather than crash-looping forever.
    """
    log.info("Resolving model (env → user settings → first available)…")
    try:
        resolved = await resolve_model()
        set_current_model(resolved)
        log.info("✓ Model resolved: %s", resolved)
    except Exception as e:
        log.error("Model resolution failed: %s: %s", type(e).__name__, e or repr(e))
        log.error(
            "Set DEFAULT_MODEL in .env, or configure a default model in "
            "Open WebUI user settings (Settings → Interface → Default Model)."
        )
        # Don't re-raise: that kills the container. Instead, leave the model
        # unresolved so /health reports 503 and the operator gets a clear signal.
        # Any incoming message will hit current_model() and raise a clear error
        # instead of silently using a wrong model.
        log.error("Bot will start but cannot serve messages until a model is resolvable.")


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
        .post_init(post_init)
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
