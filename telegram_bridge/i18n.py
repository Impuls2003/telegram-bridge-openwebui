"""Small translation catalog for messages written by the bridge."""

from __future__ import annotations

from typing import Any


DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = {"en", "ru"}
LANGUAGE_NAMES = {"en": "English", "ru": "Русский"}

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "start.help": (
            "👋 Connected to your Open WebUI.\n\n"
            "• Send any message to start or continue a chat.\n"
            "• Reply to one of my messages to continue that chat.\n"
            "• Send a photo with a caption to attach it to the chat.\n"
            "• `/newchat` — start a fresh chat.\n"
            "• `/id` — show a link to the current chat.\n"
            "• `/model [name|default]` — show or switch your model.\n"
            "• `/system [show|clear|<prompt>]` — manage your system prompt.\n"
            "• `/language [en|ru|default]` — show or change the bot language."
        ),
        "chat.new_next": "🆕 The next message will start a fresh chat.",
        "chat.none": "No active chat yet — send a message to start one.",
        "chat.current": "Current chat: {url}",
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
        "system.reset": "✅ System prompt reset to the configured default.",
        "system.set": "✅ System prompt set. It will be applied to your next message:\n\n{prompt}",
        "language.current": "Current language: {language}. Use `/language en` or `/language ru` to change it.",
        "language.changed": "✅ Bot language changed to English.",
        "language.reset": "✅ Bot language now follows your Telegram language (English if unsupported).",
        "language.invalid": "❌ Unsupported language. Choose `en` or `ru`, or use `default`.",
        "stream.empty": "⚠️ Open WebUI returned no readable response text.",
        "message.failed": "❌ Failed to process message. Please try again later.",
        "photo.failed": "❌ Failed to process photo. Please try again later.",
        "photo.default_caption": "What's in this image?",
        "outbound.backlink": "🔗 Open conversation in Open WebUI →: {url}",
    },
    "ru": {
        "start.help": (
            "👋 Подключено к Open WebUI.\n\n"
            "• Отправьте сообщение, чтобы начать или продолжить чат.\n"
            "• Ответьте на моё сообщение, чтобы продолжить тот чат.\n"
            "• Отправьте фото с подписью, чтобы прикрепить его к чату.\n"
            "• `/newchat` — начать новый чат.\n"
            "• `/id` — получить ссылку на текущий чат.\n"
            "• `/model [имя|default]` — посмотреть или сменить модель.\n"
            "• `/system [show|clear|<промпт>]` — настроить системный промпт.\n"
            "• `/language [en|ru|default]` — посмотреть или сменить язык бота."
        ),
        "chat.new_next": "🆕 Следующее сообщение начнёт новый чат.",
        "chat.none": "Активного чата пока нет — отправьте сообщение, чтобы начать.",
        "chat.current": "Текущий чат: {url}",
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
        "system.reset": "✅ Системный промпт сброшен к значению по умолчанию.",
        "system.set": "✅ Системный промпт задан. Он будет применён к следующему сообщению:\n\n{prompt}",
        "language.current": "Текущий язык: {language}. Для смены используйте `/language en` или `/language ru`.",
        "language.changed": "✅ Язык бота изменён на русский.",
        "language.reset": "✅ Язык бота теперь определяется настройкой Telegram (если язык не поддерживается — английский).",
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
