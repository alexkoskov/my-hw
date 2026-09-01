# Project: Hot Wheels News Bot

> A Python bot that collects Hot Wheels news from four sources, transcreates each article into Russian with an LLM (not a summary — full text, style-pinned to `ux-guidelines.md`), publishes the body to Telegra.ph and posts a hashtag card with an Instant View preview to a Telegram channel.

---

## How This Project Works

**Context:** All project knowledge is in `.claude/skills/project-knowledge/` skill with guides for architecture, patterns, and deployment (+ optional UX guidelines and domain-specific files).

**Default branch:** `dev`

**Execution scope:** The HARD SCOPE RULE in [AGENTS.md](AGENTS.md) is mandatory: install the cabinet, do not renovate the building. Use the minimum safe implementation and verification path; do not add process work without explicit approval.

**Library Documentation:** Always use context7 when you need code generation, setup or configuration steps, or library/API documentation. This means you should automatically use the Context7 MCP tools to resolve library id and get library docs without user having to explicitly ask.
