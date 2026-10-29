"""Telegram command and update handlers for the bridge."""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass
from functools import wraps
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ContextTypes

from telegram_bridge.config import (
    ALLOWED_USER_IDS,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    LAST_OUTBOUND_TTL_SECONDS,
    LINK_TO_CHAT_IN_EVERY_MESSAGE,
    OPENWEBUI_BASE_URL,
    STREAM_THROTTLE,
    log,
)
from telegram_bridge.i18n import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    normalize_language,
    translate,
)
from telegram_bridge.openwebui import OpenWebUI, image_file_ref
from telegram_bridge.sessions import SessionStore
from telegramify_markdown import split_entities


@dataclass
class ResolvedModel:
    name: str
    source: str  # one of: "env", "user-settings", "first-available", "explicit", "user setting"

    def __str__(self) -> str:
        return f"{self.name} (from {self.source})"


class ModelNotAvailableError(ValueError):
    """Raised when a requested model ID is not present in Open WebUI."""


@dataclass
class PreparedMessage:
    tg_user_id: int
    owui_model: ResolvedModel
    owui_system_prompt: Optional[str]
    owui_chat_id: Optional[str]
    language: str


def authorized(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @wraps(func)
    async def wrapper(
        self: TelegramHandlers,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> Any:
        user = update.effective_user
        if ALLOWED_USER_IDS and user.id not in ALLOWED_USER_IDS:
            log.warning(
                "Rejected unauthorized user id=%s username=%s",
                user.id,
                user.username,
            )
            return None
        return await func(self, update, context)

    return wrapper


class TelegramHandlers:
    """Telegram handlers with their Open WebUI and session dependencies."""

    def __init__(self, owui: OpenWebUI, sessions: SessionStore) -> None:
        self.owui = owui
        self.sessions = sessions

    def _language(self, update: Update) -> str:
        """Get the user's selected language or detect it from Telegram."""
        user = update.effective_user
        language = self.sessions.get(user.id, "settings.language")
        if isinstance(language, str) and language in SUPPORTED_LANGUAGES:
            return language

        detected = normalize_language(user.language_code)
        if self.sessions.get(user.id, "settings.telegram_language") != detected:
            self.sessions.set(user.id, "settings.telegram_language", detected)
        return detected

    async def _resolve_model(self, specific: Optional[str] = None) -> ResolvedModel:
        """
        Decide which model to use. Priority:
        1. Explicit `specific` arg (from /model command)
        2. DEFAULT_MODEL env var
        3. User's OWUI default (ui.models[0] from /api/v1/users/user/settings)
        4. First available model from /api/models
        """
        if specific:
            models = await self.owui.list_models()
            valid_ids = {m.get("id") for m in models}
            if specific not in valid_ids:
                raise ModelNotAvailableError(specific)
            return ResolvedModel(specific, "explicit (/model command)")

        if DEFAULT_MODEL:
            return ResolvedModel(DEFAULT_MODEL, "env DEFAULT_MODEL")

        user_default = await self.owui.get_user_default_model()
        if user_default:
            return ResolvedModel(user_default, "user settings (ui.models[0])")

        models = await self.owui.list_models()
        if models:
            first_id = models[0].get("id")
            if first_id:
                return ResolvedModel(first_id, "first from /api/models")

        raise RuntimeError(
            "No model could be resolved. Set DEFAULT_MODEL in .env, or configure "
            "a default model in Open WebUI user settings, or expose at least one model."
        )


    async def _model_for_user(self, user_id: int) -> ResolvedModel:
        """Return the user's selected model, resolving and saving a default on first use."""
        model_name = self.sessions.get(user_id, "settings.model")
        if model_name:
            return ResolvedModel(str(model_name), "user setting")

        model = await self._resolve_model()
        # Resolving is async, so a /model command may have saved a choice
        # while we were waiting. Keep that newer per-user choice if so.
        model_name = self.sessions.get(user_id, "settings.model")
        if model_name:
            return ResolvedModel(str(model_name), "user setting")

        self.sessions.set(user_id, "settings.model", model.name)
        log.info("Resolved default model for user %s: %s", user_id, model)
        return ResolvedModel(model.name, "user setting")


    @authorized
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            translate(self._language(update), "start.help"),
            parse_mode=ParseMode.MARKDOWN,
        )


    @authorized
    async def cmd_newchat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        self.sessions.set(user_id, "owui.current_chat_id", None)
        self.sessions.set(user_id, "from_telegram.last_message_at", None)
        await update.message.reply_text(translate(self._language(update), "chat.new_next"))


    @authorized
    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        language = self._language(update)
        chat_id = self.sessions.get(
            update.effective_user.id, "owui.current_chat_id"
        )
        if not chat_id:
            await update.message.reply_text(translate(language, "chat.none"))
            return
        url = f"{OPENWEBUI_BASE_URL}/c/{chat_id}"
        await update.message.reply_text(
            translate(language, "chat.current", url=url), disable_web_page_preview=True
        )


    @authorized
    async def cmd_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        current_language = self._language(update)
        args = context.args or []

        if not args:
            await update.message.reply_text(
                translate(
                    current_language,
                    "language.current",
                    language=LANGUAGE_NAMES[current_language],
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        requested = args[0].strip().lower()
        if requested in {"default", "reset"}:
            self.sessions.set(user_id, "settings.language", None)
            language = self._language(update)
            await update.message.reply_text(translate(language, "language.reset"))
            return

        if requested not in SUPPORTED_LANGUAGES:
            await update.message.reply_text(
                translate(current_language, "language.invalid"),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        self.sessions.set(user_id, "settings.language", requested)
        await update.message.reply_text(translate(requested, "language.changed"))


    @authorized
    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        user_id = update.effective_user.id
        language = self._language(update)

        if not args:
            try:
                models = await self.owui.list_models()
                selected_model = await self._model_for_user(user_id)
            except Exception:
                log.exception("Could not list models")
                await update.message.reply_text(translate(language, "model.list_failed"))
                return
            model_lines = "\n".join(f"  • `{m.get('id')}`" for m in models[:25])
            source_keys = {
                "user setting": "model.source.user",
                "env DEFAULT_MODEL": "model.source.env",
                "user settings (ui.models[0])": "model.source.owui",
                "first from /api/models": "model.source.first",
                "explicit (/model command)": "model.source.explicit",
            }
            source_key = source_keys.get(
                selected_model.source, "model.source.user"
            )
            source = translate(language, source_key)
            await update.message.reply_text(
                f"{translate(language, 'model.current', model=selected_model.name)}\n"
                f"{translate(language, 'model.source', source=source)}\n\n"
                f"{translate(language, 'model.available')}\n{model_lines}\n\n"
                f"{translate(language, 'model.usage')}",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        requested = args[0]
        try:
            if requested.lower() in {"default", "reset"}:
                self.sessions.set(user_id, "settings.model", None)
                default_model = await self._model_for_user(user_id)
                await update.message.reply_text(
                    translate(language, "model.default_set", model=default_model.name),
                    parse_mode=ParseMode.MARKDOWN,
                )
                return

            new_model = await self._resolve_model(specific=requested)
            self.sessions.set(user_id, "settings.model", new_model.name)
            await update.message.reply_text(
                translate(language, "model.switched", model=new_model.name),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            if isinstance(e, ModelNotAvailableError):
                error_text = translate(language, "model.unavailable", model=requested)
            else:
                log.exception("Could not switch model")
                error_text = translate(language, "model.switch_failed")
            await update.message.reply_text(error_text)


    @authorized
    async def cmd_system(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = context.args or []
        user_id = update.effective_user.id
        language = self._language(update)

        if not args or args[0].lower() == "show":
            prompt = self.sessions.get(
                user_id,
                "settings.system_prompt",
                DEFAULT_SYSTEM_PROMPT or translate(language, "system.none"),
            )
            await update.message.reply_text(
                translate(language, "system.current", prompt=prompt)
            )
            return

        if args[0].lower() in ("clear", "reset", "none"):
            self.sessions.set(user_id, "settings.system_prompt", None)
            await update.message.reply_text(translate(language, "system.reset"))
            return

        prompt = " ".join(args)
        self.sessions.set(user_id, "settings.system_prompt", prompt)
        await update.message.reply_text(
            translate(language, "system.set", prompt=prompt)
        )


    # ---------------------------------------------------------------------------
    # Message handlers
    # ---------------------------------------------------------------------------
    def _resolve_target_chat_id(self, update: Update, user_id: int) -> Optional[str]:
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
            mapped = self.sessions.get(
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
        last_message_at = self.sessions.get(
            user_id, "from_telegram.last_message_at"
        )
        current_chat_id = self.sessions.get(user_id, "owui.current_chat_id")
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
        self,
        update: Update,
        chat_id: str,
        stream: AsyncIterator[str],
        language: str,
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
                accumulated = await self.owui.get_latest_assistant_text(chat_id)
            except Exception as e:
                log.warning("Could not retrieve saved Open WebUI response: %s", e)

        if not accumulated.strip():
            log.error("No assistant text was available for Telegram (chat %s)", chat_id)
            await placeholder.edit_text(translate(language, "stream.empty"))
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


    async def _prepare_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> PreparedMessage:
        """Resolve the settings and chat target shared by text and photo messages."""
        user = update.effective_user
        language = self._language(update)
        system_prompt = self.sessions.get(user.id, "settings.system_prompt", DEFAULT_SYSTEM_PROMPT or None)
        target_chat_id = self._resolve_target_chat_id(update, user.id)
        self.sessions.set(
            user.id, "from_telegram.last_message_at", update.message.date.timestamp()
        )

        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action=ChatAction.TYPING
        )
        model = await self._model_for_user(user.id)
        return PreparedMessage(
            tg_user_id=user.id,
            owui_model=model,
            owui_system_prompt=system_prompt,
            owui_chat_id=target_chat_id,
            language=language,
        )


    async def _complete_message(
        self,
        update: Update,
        prepared: PreparedMessage,
        content: str,
        file_id: Optional[str] = None,
        image_data_url: Optional[str] = None,
    ) -> None:
        """Create or continue the OWUI chat, request a reply, and send it to Telegram."""
        user_id = prepared.tg_user_id
        model = prepared.owui_model
        file_ids = [file_id] if file_id else None

        if prepared.owui_chat_id:
            chat_id = prepared.owui_chat_id
            self.sessions.set(user_id, "owui.current_chat_id", chat_id)
            parent_message_id = await self.owui.get_current_message_id(chat_id)
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
                "content": content,
                "models": [model.name],
                "timestamp": int(time.time()),
            }
            if file_id:
                user_message["files"] = [image_file_ref(file_id)]
        else:
            self.sessions.set(user_id, "owui.current_chat_id", None)
            chat_id, assistant_message_id, user_message = await self.owui.new_chat(
                model.name, content, file_ids=file_ids,
            )
            self.sessions.set(user_id, "owui.current_chat_id", chat_id)

        stream = await self.owui.complete(
            chat_id,
            model.name,
            content,
            system_prompt=prepared.owui_system_prompt,
            file_ids=file_ids,
            assistant_message_id=assistant_message_id,
            user_message=user_message,
            image_data_url=image_data_url,
            stream=True,
        )
        await self._stream_reply_to_telegram(update, chat_id, stream, prepared.language)


    @authorized
    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = update.message.text or ""
        if not text:
            return

        try:
            prepared = await self._prepare_message(update, context)
            await self._complete_message(update, prepared, text)
        except Exception:
            log.exception("Message handling failed")
            await update.message.reply_text(
                translate(self._language(update), "message.failed")
            )


    @authorized
    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Download the largest photo and process it through the shared message flow."""
        try:
            prepared = await self._prepare_message(update, context)
            caption = update.message.caption or translate(
                prepared.language, "photo.default_caption"
            )
            photo = update.message.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            file_bytes = bytes(await tg_file.download_as_bytearray())
            file_id = await self.owui.upload_file(file_bytes, f"tg-{photo.file_unique_id}.jpg")
            log.info("Uploaded photo, OWUI file_id=%s (%d bytes)", file_id, len(file_bytes))
            image_data_url = "data:image/jpeg;base64," + base64.b64encode(file_bytes).decode("ascii")
            await self._complete_message(
                update, prepared, caption, file_id=file_id, image_data_url=image_data_url
            )
        except Exception:
            log.exception("Photo handling failed")
            await update.message.reply_text(
                translate(self._language(update), "photo.failed"),
            )
