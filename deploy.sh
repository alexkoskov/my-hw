#!/bin/bash
# Deployment script for Hot Wheels News Bot
# Copies necessary files to the production server via SCP.

set -e

# Configuration (override with environment variables)
SSH_HOST="${SSH_HOST:-user@example.com}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/user/bot}"

# Files to deploy
FILES=(
    "news_bot.py"
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