#!/bin/bash
# Deployment script for Hot Wheels News Bot
# Copies necessary files to the production server via SCP.

set -e

# Configuration (override with environment variables)
SSH_HOST="${SSH_HOST:-user@example.com}"
DEPLOY_PATH="${DEPLOY_PATH:-/home/user/bot}"

# Files to deploy
# What lives where:
#   - Server (this list): everything news_bot.job() needs. There is no cron:
#     news_bot runs as a long-lived process and schedules itself in-process
#     (news_bot.py:4641 — one daily tick at 10:00 МСК, which then computes the
#     fixed publish slots 10:00/15:00/19:30). Corrected 2026-08-03; the old
#     "daily 12:00 МСК cron" wording predates the Docker migration.
#     The list is news_bot.py + its first-party imports (sources, Telegraph
#     publisher, pending-articles repo) + the LLM stack (llm_transcreation.py
#     dispatcher, _llm_common.py, and the four engine modules) +
#     compute_publish_slots.py + outage_state.py — all imported by news_bot.py
#     at startup; without any of them news_bot crashes with ImportError on the
#     first tick — + feeds.json (still loaded by load_feed_urls() at top of
#     job()) + requirements.txt + .env.example + ux-guidelines.md (the LLM
#     system prompt; each engine loads it via its own _load_prompt, which uses
#     the shared default path in _llm_common.py:64-70).
#   - INVARIANT: any new first-party import added to news_bot.py MUST be
#     mirrored into FILES here AND in .github/workflows/deploy.yml. Otherwise
#     the server will hit ImportError on the next tick with no CI signal.
#   - dom_blocks.py: the shared inline-markup walker. Imported by the source
#     parsers (orangetrack today, t-hunted next), so it rides in via their
#     import closure — the invariant test derives that automatically.
#   - Ahead of its importer: feature_flags.py is listed although nothing in
#     the runtime closure imports it YET — t_hunted_source picks it up in
#     Task 7 of source-formatting-parity. Shipped with the module rather than
#     "later" because this exact class of omission once cost 38 consecutive
#     ModuleNotFoundError crashes (work/completed/t-hunted-pt-source).
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
    "feature_flags.py"
    "dom_blocks.py"
    "model_extractor.py"
    "backfill_fingerprints.py"
    "watchdog.sh"
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
