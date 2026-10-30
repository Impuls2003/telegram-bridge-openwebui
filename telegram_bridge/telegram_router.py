"""Authorize Telegram updates and route them to per-user handler instances."""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from telegram_bridge.config import (
    ADMINISTRATOR_ID,
    ALLOWED_USER_IDS,
    OPENWEBUI_API_KEYS,
    log,
)
from telegram_bridge.i18n import normalize_language, translate
from telegram_bridge.sessions import SessionStore
from telegram_bridge.telegram_handlers import TelegramHandlers


class TelegramHandlerRouter:
    """Authorize each sender and delegate updates to that user's handler."""

    def __init__(
        self,
        user_handlers: dict[int, TelegramHandlers],
        sessions: SessionStore,
        default_handler: TelegramHandlers | None = None,
    ) -> None:
        self.user_handlers = user_handlers
        self.sessions = sessions
        self.default_handler = default_handler
        self._access_requests: set[int] = set()

    @staticmethod
    def _is_authorized(user_id: int) -> bool:
        return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS

    def _language(self, update: Update) -> str:
        user = update.effective_user
        if user is None:
            return "en"
        language = self.sessions.get(user.id, "settings.language")
        if isinstance(language, str) and language in {"en", "ru"}:
            return language
        return normalize_language(user.language_code)

    async def _deny_start(self, update: Update) -> None:
        user = update.effective_user
        if user is None or update.message is None:
            return
        language = self._language(update)
        log.warning(
            "Rejected unauthorized /start user id=%s username=%s",
            user.id,
            user.username,
        )
        await update.message.reply_text(
            translate(language, "access.denied_start", user_id=user.id),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(
                    translate(language, "access.request_button"),
                    callback_data="menu:request_access",
                )]]
            ),
        )

    async def _handle_unauthorized_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        user = update.effective_user
        if query is None:
            return
        if user is None:
            await query.answer()
            return

        language = self._language(update)
        action = query.data or ""
        log.warning(
            "Rejected unauthorized callback user id=%s username=%s",
            user.id,
            user.username,
        )
        if action != "menu:request_access":
            await query.answer(translate(language, "access.callback_denied"), show_alert=True)
            return
        if user.id in self._access_requests:
            await query.answer(translate(language, "access.request_already_sent"), show_alert=True)
            return
        if ADMINISTRATOR_ID is None:
            log.error("Cannot send access request: ADMINISTRATOR_ID is not configured")
            await query.answer(translate(language, "access.admin_unconfigured"), show_alert=True)
            return

        username = f"@{user.username}" if user.username else translate(language, "access.no_username")
        self._access_requests.add(user.id)
        try:
            await context.bot.send_message(
                chat_id=ADMINISTRATOR_ID,
                text=translate(
                    language,
                    "access.request_message",
                    name=user.full_name,
                    username=username,
                    user_id=user.id,
                ),
            )
        except Exception:
            self._access_requests.discard(user.id)
            log.exception("Could not send access request to administrator %s", ADMINISTRATOR_ID)
            await query.answer(translate(language, "access.request_failed"), show_alert=True)
            return

        await query.answer()
        if query.message is not None:
            await query.edit_message_text(translate(language, "access.request_sent"))

    def _handler_for(self, user_id: int) -> TelegramHandlers | None:
        return self.user_handlers.get(user_id, self.default_handler)

    async def _dispatch(
        self,
        method_name: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> Any:
        user = update.effective_user
        if user is None or not self._is_authorized(user.id):
            if method_name == "cmd_start":
                await self._deny_start(update)
            elif method_name == "on_callback_query":
                await self._handle_unauthorized_callback(update, context)
            elif user is not None:
                log.warning(
                    "Rejected unauthorized update user id=%s username=%s",
                    user.id,
                    user.username,
                )
            return None

        handler = self._handler_for(user.id)
        if handler is None:
            log.error("No Telegram handler configured for authorized user %s", user.id)
            return None
        method: Callable[..., Coroutine[Any, Any, Any]] = getattr(handler, method_name)
        return await method(update, context)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("cmd_start", update, context)

    async def cmd_newchat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("cmd_newchat", update, context)

    async def cmd_id(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("cmd_id", update, context)

    async def cmd_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("cmd_model", update, context)

    async def cmd_system(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("cmd_system", update, context)

    async def cmd_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("cmd_language", update, context)

    async def on_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Any:
        return await self._dispatch("on_callback_query", update, context)

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("on_photo", update, context)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        return await self._dispatch("on_message", update, context)
