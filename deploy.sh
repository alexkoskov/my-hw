#!/bin/bash
# Deployment script for Hot Wheels News Bot
# Copies necessary files to the production server via SCP.

set -e

# Configuration (override with environment variables)
SSH_HOST="${SSH_HOST:-user@example.com}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/user/bot}"

# Files to deploy
# What lives where:
#   - Server (this list): everything the hourly cron path in news_bot.job()
#     needs. That's news_bot.py + its first-party imports (sources, Telegraph
#     publisher, pending-articles repo) + feeds.json (still loaded by
#     load_feed_urls() at top of job()) + requirements.txt + .env.example.
#   - Operator's Claude Code session only (NOT deployed): hw_review.py and
#     preview_renderer.py — the manual-review-workflow CLI tools the operator
#     runs locally to approve/publish queued articles. They don't run on the
#     server.
#   - Not deployed, ever: news.db (user data, must never be overwritten).
FILES=(
    "news_bot.py"
    "autoevolution_source.py"
    "mattel_news_source.py"
    "lamley_source.py"
    "telegraph_publisher.py"
    "pending_articles_repo.py"
    "feeds.json"
    "requirements.txt"
    ".env.example"
)

echo "Deploying to $SSH_HOST:$DEPLOY_PATH"

# Check that all source files exist
for file in "${FILES[@]}"; do
    if [[ ! -f "$file" ]]; then
        echo "Error: $file not found in current directory"
        exit 1
    fi
done

# Copy files
echo "Copying files..."
scp "${FILES[@]}" "$SSH_HOST:$DEPLOY_PATH/"

# Optionally install dependencies on the server
echo "Installing Python dependencies on server..."
ssh "$SSH_HOST" "cd $DEPLOY_PATH && pip install --user -r requirements.txt"

echo "Deployment complete. Ensure environment variables are set on the server."
echo "If using cron, restart is not needed; script will be executed on next schedule."