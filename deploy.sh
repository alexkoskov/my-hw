#!/bin/bash
# Deployment script for Hot Wheels News Bot
# Copies necessary files to the production server via SCP.

set -e

# Configuration (override with environment variables)
SSH_HOST="${SSH_HOST:-user@example.com}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/user/bot}"

# Files to deploy
# What lives where:
#   - Server (this list): everything the daily 12:00 МСК cron path in
#     news_bot.job() needs. That's news_bot.py + its first-party imports
#     (sources, Telegraph publisher, pending-articles repo) + the three
#     llm-transcreation runtime modules (claude_transcreation.py,
#     compute_publish_slots.py, outage_state.py — all imported by news_bot.py
#     at startup; without any of them news_bot crashes with ImportError on the
#     first cron tick) + feeds.json (still loaded by load_feed_urls() at top
#     of job()) + requirements.txt + .env.example + ux-guidelines.md (Claude
#     API system prompt, read by claude_transcreation._load_prompt).
#   - INVARIANT: any new first-party import added to news_bot.py MUST be
#     mirrored into FILES here AND in .github/workflows/deploy.yml. Otherwise
#     the server will hit ImportError on the next cron tick with no CI signal.
#   - Operator's Claude Code session only (NOT deployed): hw_review.py and
#     preview_renderer.py — the manual-review-workflow CLI tools the operator
#     runs locally to approve/publish queued articles. They don't run on the
#     server.
#   - Special case — ux-guidelines.md: lives at
#     .claude/skills/project-knowledge/references/ux-guidelines.md in the repo
#     but `scp` (called below WITHOUT -r) flattens subdirs, so on the server
#     the file lands at $DEPLOY_PATH/ux-guidelines.md (flat). Decision 8 of
#     the llm-transcreation tech-spec covers this: claude_transcreation
#     ._load_prompt tries the subdir path first and falls back to the flat
#     filename — both layouts work, no tar wrapper needed.
#   - Not deployed, ever: news.db (user data, must never be overwritten).
FILES=(
    "news_bot.py"
    "autoevolution_source.py"
    "mattel_news_source.py"
    "lamley_source.py"
    "orangetrack_source.py"
    "t_hunted_source.py"
    "telegraph_publisher.py"
    "pending_articles_repo.py"
    "claude_transcreation.py"
    "gemini_transcreation.py"
    "openai_transcreation.py"
    "openrouter_transcreation.py"
    "llm_transcreation.py"
    "_llm_common.py"
    "compute_publish_slots.py"
    "outage_state.py"
    "admin_alerts.py"
    "boilerplate_filter.py"
    "feeds.json"
    "requirements.txt"
    ".env.example"
    ".claude/skills/project-knowledge/references/ux-guidelines.md"
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
