# Checkpoint: News Bot Project

## Current Status
The project skeleton has been created. All core components are implemented in `news_bot.py` with placeholder implementations. The following steps have been completed:

1. ✅ Requirements analysis and architecture design.
2. ✅ Python environment setup (`requirements.txt` created).
3. ✅ SQLite database schema (`processed_news` table).
4. ✅ RSS feed parsing (`feedparser`).
5. ✅ Article scraping (`requests` + `BeautifulSoup`).
6. ✅ Translation service (`deep_translator` with Google Translate).
7. ✅ Text summarization (simple extractive).
8. ✅ Telegram posting (`python-telegram-bot`).
9. ✅ Scheduling (`schedule`).

## Files Created
- `requirements.txt` – dependencies.
- `news_bot.py` – main script.

## Next Steps Required
1. **Test RSS feed parsing** – verify that the feed URL returns expected entries.
2. **Adjust article scraping selectors** – inspect actual HTML structure of autoevolution articles and update `fetch_article()` accordingly.
3. **Set up environment variables**:
   - `TELEGRAM_BOT_TOKEN` – token from BotFather.
   - `TELEGRAM_CHANNEL_ID` – channel username or ID.
   - (Optional) Translation API key if using a paid service.
4. **Test translation** – ensure Google Translate works without blocking (may need fallback).
5. **Improve summarization** – currently extracts first 5 sentences; consider more sophisticated extraction.
6. **Test Telegram posting** – send a test post to ensure formatting and media work.
7. **Configure scheduling** – decide whether to run as a systemd service or cron job (script currently uses `schedule` library, which requires the process to stay alive).
8. **Error handling and logging** – add more robust error recovery (e.g., retries, skipping broken articles).
9. **Add configuration file** (optional) – move settings to a config file or `.env`.

## How to Resume
1. Open a new chat with the assistant and provide the context: “We are working on the Hot Wheels news bot project. Refer to the checkpoint in `my‑hw/work/checkpoint.md`.”
2. The assistant can examine the current code and continue from integration and testing.
3. Run the script in a test environment:
   ```bash
   cd my-hw
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   TELEGRAM_BOT_TOKEN=... TELEGRAM_CHANNEL_ID=... python news_bot.py
   ```
4. Monitor logs and adjust as needed.

## Notes
- The script currently processes only the **latest 3 new articles** (as requested).
- The database `news.db` will be created in the same directory.
- The scheduler runs the job daily at 12:00 local time (adjust in code).
- For production, consider containerizing with Docker and using a managed scheduler (e.g., system cron).

## Open Questions
- Does the autoevolution article page have a consistent HTML structure? Need to verify selectors.
- Is Google Translate free tier sufficient? Might need to switch to LibreTranslate or another API.
- Should images be downloaded and re‑uploaded to Telegram (to avoid hotlinking)? Currently images are linked directly.

---
*Checkpoint created on 2026‑04‑15.*