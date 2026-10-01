# Reachout API (this repository on its own). Chromium + its system libraries come with the Playwright image,
# so WhatsApp can be switched on (WHATSAPP_ENABLED=1); with it off, the browser is simply never started.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app/backend
COPY requirements.txt requirements.lock.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c requirements.lock.txt
COPY . .

ENV DATA_DIR=/data \
    OPEN_BROWSER=0 \
    WA_HEADLESS=1 \
    CHROMIUM_NO_SANDBOX=1 \
    APP_ENV=production \
    COOKIE_SECURE=1 \
    PORT=8000

# Run as the image's unprivileged user, not root.
RUN mkdir -p /data && chown -R pwuser:pwuser /data /app
USER pwuser
EXPOSE 8000

# One worker process: send jobs, WhatsApp browsers and per-account locks live in this process's memory.
# Do NOT raise --workers: locks and background jobs would run twice. Scale threads instead.
CMD gunicorn app:app --workers 1 --threads 16 --timeout 120 --bind 0.0.0.0:${PORT}
