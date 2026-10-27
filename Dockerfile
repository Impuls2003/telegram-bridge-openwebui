# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the bot code
COPY telegram_bot.py ./
COPY telegram_bridge/ ./telegram_bridge/

# Pre-create the data dir the persistent sessions file lives in,
# and hand ownership to the unprivileged 'bot' user.
RUN mkdir -p /app/data \
    && useradd --create-home --shell /bin/false --uid 1000 bot \
    && chown -R bot:bot /app
USER bot

ENTRYPOINT ["python", "-u", "telegram_bot.py"]
