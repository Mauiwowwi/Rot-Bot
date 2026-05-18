# Microsoft's official Playwright image already contains Chromium plus
# every system library it needs. This removes the single most common
# cloud deployment failure (Chromium failing to launch on a bare host).
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app

# Install Python deps. requirements.txt is copied first so Docker can
# cache this layer and not reinstall on every code change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY rotation_bot.py .

# The bot reads the token from this env var. Set the real value in
# Railway's Variables tab, NOT here.
ENV TELEGRAM_BOT_TOKEN=""

# On Railway, scoresandodds blocks plain requests, so skip straight to
# the headless browser instead of wasting time on a doomed attempt.
ENV SKIP_REQUESTS="1"

# Polling bot: no web server, no port to expose. Just run it.
CMD ["python", "rotation_bot.py"]
