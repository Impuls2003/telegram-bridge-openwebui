"""Small translation catalog for messages written by the bridge."""

from __future__ import annotations

from typing import Any


DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = {"en", "ru"}
LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "start.help": "👋 Connected to your Open WebUI. Send a message or choose an action:",
        "access.denied_start": "Access is not enabled for your account.\nYour Telegram ID: `{user_id}`\nAsk the bot administrator to add this ID to `ALLOWED_USER_IDS`.",
        "button.newchat": "🆕 New chat",
        "button.telegram_id": "🪪 My Telegram ID",
        "button.chat": "🔗 Current chat",
        "button.model": "🤖 Model",
        "button.system": "📝 System prompt",
        "button.system_set": "✏️ Change prompt",
        "button.system_reset": "↩️ Reset prompt",
        "button.language": "🌐 Language",
        "button.default_model": "Use default model",
        "button.back": "⬅️ Back",
        "model.menu_title": "Choose a model (first 25):",
        "model.menu_empty": "No models are available.",
        "model.menu_expired": "This model menu expired. Send /start to open a new one.",
        "chat.new_next": "🆕 The next message will start a fresh chat.",
        "chat.none": "No active chat yet — send a message to start one.",
        "chat.current": "Current chat: {url}",
        "telegram.id": "Your Telegram ID: `{user_id}`",
        "model.list_failed": "❌ Could not list models. Please try again later.",
        "model.current": "Current model: `{model}`",
        "model.source": "Source: {source}",
        "model.available": "Available models (first 25):",
        "model.usage": "Use `/model <name>` to switch or `/model default` to reset.",
        "model.source.user": "user setting",
        "model.source.env": "environment default",
        "model.source.owui": "Open WebUI user settings",
        "model.source.first": "first available model",
        "model.source.explicit": "explicit selection",
        "model.default_set": "✅ Using the default model: `{model}`",
        "model.switched": "✅ Switched to: `{model}`",
        "model.switch_failed": "❌ Could not switch model. Please try again later.",
        "model.unavailable": "❌ Model `{model}` is not available in Open WebUI.",
        "system.current": "Current system prompt:\n\n{prompt}",
        "system.none": "(none — using Open WebUI default behavior)",
        "system.enter_prompt": "Send the system prompt as your next text message.",
        "system.reset": "✅ System prompt reset to the configured default.",
        "system.set": "✅ System prompt set. It will be applied to your next message:\n\n{prompt}",
        "language.current": "Current language: {language}. Use `/language en` or `/language ru` to change it.",
        "language.changed": "✅ Bot language changed to English.",
        "language.reset": "✅ Bot language now follows your Telegram language (English if unsupported).",
        "language.menu_title": "Choose the bot language:",
        "language.follow_telegram": "Follow Telegram language",
        "language.invalid": "❌ Unsupported language. Choose `en` or `ru`, or use `default`.",
        "stream.empty": "⚠️ Open WebUI returned no readable response text.",
        "message.failed": "❌ Failed to process message. Please try again later.",
        "photo.failed": "❌ Failed to process photo. Please try again later.",
        "photo.default_caption": "What's in this image?",
        "outbound.backlink": "🔗 Open conversation in Open WebUI →: {url}",
    },
    "ru": {
        "start.help": "👋 Подключено к Open WebUI. Отправьте сообщение или выберите действие:",
        "access.denied_start": "Для вашего аккаунта доступ не включён.\nВаш Telegram ID: `{user_id}`\nПопросите администратора бота добавить этот ID в `ALLOWED_USER_IDS`.",
        "button.newchat": "🆕 Новый чат",
        "button.telegram_id": "🪪 Мой Telegram ID",
        "button.chat": "🔗 Текущий чат",
        "button.model": "🤖 Модель",
        "button.system": "📝 Системный промпт",
        "button.system_set": "✏️ Изменить промпт",
        "button.system_reset": "↩️ Сбросить промпт",
        "button.language": "🌐 Язык",
        "button.default_model": "Модель по умолчанию",
        "button.back": "⬅️ Назад",
        "model.menu_title": "Выберите модель (первые 25):",
        "model.menu_empty": "Доступных моделей нет.",
        "model.menu_expired": "Это меню моделей устарело. Отправьте /start, чтобы открыть новое.",
        "chat.new_next": "🆕 Следующее сообщение начнёт новый чат.",
        "chat.none": "Активного чата пока нет — отправьте сообщение, чтобы начать.",
        "chat.current": "Текущий чат: {url}",
        "telegram.id": "Ваш Telegram ID: `{user_id}`",
        "model.list_failed": "❌ Не удалось получить список моделей. Попробуйте позже.",
        "model.current": "Текущая модель: `{model}`",
        "model.source": "Источник: {source}",
        "model.available": "Доступные модели (первые 25):",
        "model.usage": "Используйте `/model <имя>` для смены или `/model default` для сброса.",
        "model.source.user": "настройка пользователя",
        "model.source.env": "модель по умолчанию из окружения",
        "model.source.owui": "настройки пользователя Open WebUI",
        "model.source.first": "первая доступная модель",
        "model.source.explicit": "выбрана явно",
        "model.default_set": "✅ Используется модель по умолчанию: `{model}`",
        "model.switched": "✅ Выбрана модель: `{model}`",
        "model.switch_failed": "❌ Не удалось сменить модель. Попробуйте позже.",
        "model.unavailable": "❌ Модель `{model}` недоступна в Open WebUI.",
        "system.current": "Текущий системный промпт:\n\n{prompt}",
        "system.none": "(не задан — используется поведение Open WebUI по умолчанию)",
        "system.enter_prompt": "Отправьте системный промпт следующим текстовым сообщением.",
        "system.reset": "✅ Системный промпт сброшен к значению по умолчанию.",
        "system.set": "✅ Системный промпт задан. Он будет применён к следующему сообщению:\n\n{prompt}",
        "language.current": "Текущий язык: {language}. Для смены используйте `/language en` или `/language ru`.",
        "language.changed": "✅ Язык бота изменён на русский.",
        "language.reset": "✅ Язык бота теперь определяется настройкой Telegram (если язык не поддерживается — английский).",
        "language.menu_title": "Выберите язык бота:",
        "language.follow_telegram": "Использовать язык Telegram",
        "language.invalid": "❌ Язык не поддерживается. Выберите `en` или `ru` либо укажите `default`.",
        "stream.empty": "⚠️ Open WebUI не вернул текстовый ответ.",
        "message.failed": "❌ Не удалось обработать сообщение. Попробуйте позже.",
        "photo.failed": "❌ Не удалось обработать фото. Попробуйте позже.",
        "photo.default_caption": "Что изображено на фото?",
        "outbound.backlink": "🔗 Открыть диалог в Open WebUI →: {url}",
    },
}


def normalize_language(language: str | None) -> str:
    """Return a supported language, or English when the language is unknown."""
    if not isinstance(language, str):
        return DEFAULT_LANGUAGE
    code = (language or "").strip().lower().replace("_", "-").split("-", 1)[0]
    return code if code in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def translate(language: str, key: str, **values: Any) -> str:
    """Translate a message, falling back to English for unknown languages or keys."""
    template = MESSAGES.get(language, MESSAGES[DEFAULT_LANGUAGE]).get(
        key, MESSAGES[DEFAULT_LANGUAGE].get(key, key)
    )
    return template.format(**values)
