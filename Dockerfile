# One-shot image for the World Cup exact-score Telegram alerter.
# It runs the script once and exits — schedule it from the host (cron, a systemd
# timer, or a Kubernetes CronJob). See the README for the crontab line.
#
# Build:  docker build -t wc-alerts .
# Run:    docker run --rm -v "$PWD/.env:/app/.env:ro" wc-alerts --telegram --hours 2
FROM python:3.12-slim

# tzdata gives accurate Asia/Jerusalem kickoff times in the message (the script
# reads zoneinfo; without it, it falls back to a fixed +03:00).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first so the layer caches across script edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wc_exact_score.py .

# Secrets come from /app/.env (mount it read-only) or -e TELEGRAM_BOT_TOKEN /
# -e TELEGRAM_CHAT_ID. Everything after the image name is passed to the script,
# so `docker run ... wc-alerts --test-telegram` etc. work. CMD is the default job.
ENTRYPOINT ["python", "-u", "wc_exact_score.py"]
CMD ["--telegram", "--hours", "2"]
