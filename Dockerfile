# Hot Wheels news bot — container image (2026-07-03, for the VPN-egress host).
# Runs news_bot.py; the daily in-process schedule + restart policy replace the
# host systemd unit. news.db lives on a mounted /data volume (DB_FILE=/data/news.db).
FROM python:3.13-slim

# tzdata → correct log timestamps for TZ=Europe/Moscow; ca-certificates → TLS;
# iproute2 → set the container's VPN default route before Python starts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app (see .dockerignore — keeps .claude/ for the ux-guidelines.md
# system prompt; excludes .git, tests, *.db, .env, etc.).
COPY . .

CMD ["python3", "news_bot.py"]
