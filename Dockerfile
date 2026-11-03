# Hot Wheels news bot — container image (2026-07-03, for the VPN-egress host).
# Runs news_bot.py; the daily in-process schedule + restart policy replace the
# host systemd unit. news.db lives on a mounted /data volume (DB_FILE=/data/news.db).
FROM python:3.13-slim

# tzdata → correct log timestamps for TZ=Europe/Moscow; ca-certificates → TLS;
# iproute2 sets the startup route, setpriv drops the worker's privileges,
# and curl is used by the heartbeat watchdog.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates iproute2 util-linux curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV HOME=/home/bot PYTHONDONTWRITEBYTECODE=1 TZ=Europe/Moscow
RUN install -d -o 10001 -g 10001 -m 0700 /home/bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app and prompts/; .dockerignore excludes agent configuration,
# documentation, operator-only tools, tests, databases, and local credentials.
COPY . .
# Build contexts can contain private directories. The worker may read code and
# prompts, but must not own or modify them; writable state belongs on /data.
RUN chmod -R a+rX /app \
    && chmod 0555 /app/scripts/container_entrypoint.sh /app/watchdog.sh

ENTRYPOINT ["/app/scripts/container_entrypoint.sh"]
CMD ["python3", "news_bot.py"]
