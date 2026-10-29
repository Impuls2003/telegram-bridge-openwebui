"""Async client for Open WebUI's chat and admin APIs."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Optional

import httpx

log = logging.getLogger("owui-telegram")


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


class OpenWebUI:
    """Async client for Open WebUI's chat & admin APIs."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        request_timeout: float,
        connected_tools: list[str],
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.request_timeout = request_timeout
        self.connected_tools = connected_tools
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=self.request_timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
        if self.connected_tools:
            payload["tool_ids"] = self.connected_tools

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
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.request_timeout,
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
