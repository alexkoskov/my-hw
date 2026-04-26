# Session Handoff — 2026-04-26 (15:56 МСК / 12:56 UTC)

> Read this file FIRST when continuing in a new chat session. Then read auto-memory at `/home/vscode/.claude/projects/-workspaces-debian-2/memory/MEMORY.md`.

## TL;DR

Two consecutive features shipped end-to-end through Молянов pipeline today on branch `dev`:

1. **`mattel-parser-rewrite`** ✅ DONE, archived to `work/completed/`. Full pipeline + smoke against test channel passed yesterday/today. Mattel parser now reads RSC flight payload (Next.js App Router) instead of dead `__NEXT_DATA__`.
2. **`llm-transcreation-and-distributed-publishing`** 📋 user-spec approved + tech-spec drafted with R1+R2+R3 validation rounds. **Awaiting operator approve of tech-spec before `/decompose-tech-spec`**.

Plus several supporting commits (load_dotenv cold-start, boilerplate filter, throttle, auto-marker, `#news` hashtag, GitHub Actions CI deploy workflow, hw_review test fix).

**Branch state:** `dev` is **~10+ commits ahead** of where it was yesterday. Total `dev` ahead of `main`: 110+ commits (manual-review-workflow + mattel-parser-rewrite + today's commits). Nothing deployed yet.

## Where to resume

**Option A (most likely):** continue tech-spec approval flow.

Spec is at: [work/llm-transcreation-and-distributed-publishing/tech-spec.md](llm-transcreation-and-distributed-publishing/tech-spec.md)

Last validator state (commit `a764180`):
- Skeptic R1: approved (1 major mirage closed, 2 minor)
- Completeness R2: PASS (1 residual major closed in same commit, 6 minor accepted)
- Security R3: **approved** (0 critical/major; 2 prompt-injection minor accepted as residual in Risks)
- Test R2: passed (3 minor)
- Template R2: approved (0 critical/major; 2 minor)

When operator says **"approve"**:
1. Set `status: draft` → `status: approved` in tech-spec frontmatter.
2. Commit: `chore(techspec): approve tech-spec for llm-transcreation-and-distributed-publishing`
3. Suggest next: `/decompose-tech-spec llm-transcreation-and-distributed-publishing`.

**Option B:** operator chose to act on operator-side TODO instead — see "Open operator-side actions" below.

## Today's commits (most recent first)

```
a764180 chore(techspec): document residual prompt-injection risk
9baa63c chore(techspec): validation round 2 — security M5 + pin sync + R5
40a09a8 chore(techspec): validation round 1 — fix critical wave conflict
7388946 draft(techspec): create tech-spec for llm-transcreation-and-distributed-publishing
70d85d6 chore(userspec): approve user-spec for llm-transcreation-and-distributed-publishing
9f09210 chore(userspec): validation round 2 — fix max→min typo
8ce797b chore(userspec): validation round 1 — size L, strip tech-leakage
398d24e draft(userspec): create user-spec for llm-transcreation-and-distributed-publishing
1154c22 feat(teaser): append #news tag to all channel posts
b62a05e docs: document boilerplate_filter in patterns.md
896d35a feat: filter UI boilerplate from article paragraphs before translation
84c9df4 refactor(fallback): move auto-marker from teaser to Telegraph body
2c3a403 docs: document FALLBACK_THROTTLE_SECONDS + auto-marker in PK
cc4cc8c feat(fallback): throttle + auto-marker
c9af35e fix(tests): mock send_admin_notification in TestLoadFeeds
74c96df fix(news_bot): load .env on import for cold-start
22953a5 ci: add GitHub Actions deploy workflow + document setup
5d539f6 docs: remove hw_review test-fix from Planned
47e3ffb fix(hw_review): pin failed_at in list-footer format test
e4c8c2d docs: update project knowledge after mattel-parser-rewrite
af4a6bd chore: feature mattel-parser-rewrite approved by operator
... (mattel-parser-rewrite waves 1/2/3, audits, QA, approval)
8b42b67 feat(mattel): rewrite parser to RSC flight payload (task 01)
```

## Live behavior demonstrations done today (in test channel)

- 4 auto-fallback Gemini-translated posts (overflow path) at 12:38, 12:39, 12:04, plus throttle demo at 09:38/09:39 UTC.
- 1 manual-review Claude-translated post at 12:56 МСК (F1 article, [Telegraph URL](https://telegra.ph/Hot-Wheels-Premium-F1-pervye-bolidy-na-polkah--spustya-20-let-ozhidaniya-04-26)).
- 1 throttle demo with 75-second gap (60s throttle override + 15s publish time).
- All confirmed Mattel parser works on live data (`Mattel news: 0 Hot Wheels entries found`, no parsing errors).

## Open operator-side actions (in priority order)

### 1. CI deploy setup (~5 min, blocks deploy of EVERYTHING above)

- `ssh-keygen -t ed25519 -f ~/.ssh/hwbot_deploy -C "github-actions-hwbot"`
- Add `~/.ssh/hwbot_deploy.pub` to `authorized_keys` on VPS.
- GitHub repo Settings → Secrets and variables → Actions → 4 secrets:
  - `SSH_HOST`
  - `SSH_USER`
  - `DEPLOY_PATH`
  - `SSH_PRIVATE_KEY` (full PEM, including BEGIN/END)
- `git checkout main && git merge dev && git push origin main` → CI runs pytest → deploy.yml triggers automatically.

### 2. Approve tech-spec for llm-transcreation feature

Read [work/llm-transcreation-and-distributed-publishing/tech-spec.md](llm-transcreation-and-distributed-publishing/tech-spec.md) and decide:
- Approve as-is → `/decompose-tech-spec` → 13 tasks across 8 waves + 6 audit/final.
- Need changes → tell new Claude what to adjust.

### 3. Anthropic API key (only after llm-transcreation feature is decomposed + executed + ready to deploy)

- console.anthropic.com → API Keys → Create Key (e.g. "hwbot-prod").
- Top up $5 (~3-6 months at expected volume).
- Add to GitHub Secrets: `ANTHROPIC_API_KEY` (and optionally `ANTHROPIC_MODEL`, default `claude-haiku-4-5`).
- Add `TZ=Europe/Moscow` to GitHub Secrets too.

## Pending state in repo

- `news.db` has accumulated test channel state from today's runs (28+ published_articles, ~7 pending, 0 failed). Local DB only — server has its own.
- No active scheduled crons (the one-shot at 09:03 UTC fired and auto-deleted).
- No background processes.

## Quick reorientation commands for new session

```bash
# Where we are
git log --oneline -15
ls work/
ls work/completed/

# Current llm-transcreation feature state
ls work/llm-transcreation-and-distributed-publishing/
head -10 work/llm-transcreation-and-distributed-publishing/user-spec.md   # status: approved
head -10 work/llm-transcreation-and-distributed-publishing/tech-spec.md   # status: draft
ls work/llm-transcreation-and-distributed-publishing/logs/techspec/       # validator reports
```

## What worked, what didn't (for future sessions)

- **Worked:** Молянов pipeline (user-spec → tech-spec → tasks → execute) gave very clean rounds of validators catching real issues each iteration.
- **Worked:** code-research subagent doing deep technical investigation before tech-spec drafting saved significant rework.
- **Worked:** spawning teammates for actual execution (operator can't do task-work in dispatcher role).
- **Caveat:** SDK/lib version specifics are real — `schedule==1.2.1` requires `pytz`, not `zoneinfo`. Verify with code-research before committing in tech-spec.
- **Caveat:** Telegraph article body has no length limit; old 4000-char truncation was a Telegram-era vestige.
- **Caveat:** `_TokenRedactingFilter` only covers logging path; `send_admin_notification` is a separate code path that needs its own redaction (`_redact_text` helper).
- **Hit:** rate limits on Anthropic side mid-validation — had to wait ~50 min and retry. New session may also hit if running back-to-back validators.
