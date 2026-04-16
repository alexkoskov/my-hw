#!/usr/bin/env python3
"""
Fetch the latest article from RSS feeds and post to Telegram.
Uses existing functions from news_bot.py (translation, summarization, posting).
"""

import sys
import logging
import os
from datetime import datetime, timezone
import random

# Load environment variables from .env file
env_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(env_path):
    print(f"Loading .env from {env_path}")
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, value = line.split('=', 1)
                os.environ[key.strip()] = value.strip()
    print("Loaded .env file manually.")
else:
    print(f"Warning: .env file not found at {env_path}")

token = os.getenv('TELEGRAM_BOT_TOKEN')
channel = os.getenv('TELEGRAM_CHANNEL_ID')
if token:
    print(f"Token present (first 5 chars): {token[:5]}...")
else:
    print("WARNING: TELEGRAM_BOT_TOKEN not set")
if channel:
    print(f"Channel ID: {channel}")
else:
    print("WARNING: TELEGRAM_CHANNEL_ID not set")

# Import functions from news_bot.py
try:
    import news_bot
    # Patch credentials with values loaded from .env
    if token:
        news_bot.TELEGRAM_BOT_TOKEN = token
    if channel:
        news_bot.TELEGRAM_CHANNEL_ID = channel
    # Import functions from the module
    from news_bot import (
        load_feeds,
        fetch_rss,
        filter_new_entries,
        get_article_data,
        transcreate_text,
        summarize_text_with_limit,
        send_to_telegram,
        mark_processed,
        init_db,
    )
except ImportError as e:
    print(f"Failed to import from news_bot.py: {e}")
    sys.exit(1)


def get_all_entries(feed_urls):
    """Fetch entries from all RSS feeds."""
    all_entries = []
    for url in feed_urls:
        entries = fetch_rss(url)
        for entry in entries:
            entry['feed_url'] = url  # tag entry with source
        all_entries.extend(entries)
    return all_entries


def sort_entries_by_date(entries):
    """Sort entries by publication date descending (latest first).
    Uses published_parsed if available, otherwise uses current time as fallback.
    """
    now = datetime.now(timezone.utc)
    now_tuple = now.timetuple()[:6]  # (year, month, day, hour, minute, second)

    def get_sort_key(entry):
        parsed = entry.get('published_parsed')
        if parsed:
            # Convert time.struct_time to tuple for comparison
            return tuple(parsed)
        # fallback: use current time (so entries without date appear at the end)
        return now_tuple

    # Sort descending (newest first)
    entries.sort(key=get_sort_key, reverse=True)
    return entries


def main():
    """Main script logic."""
    # Setup logging
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    logger.info("Initializing database...")
    init_db()

    # 1. Load feeds
    feed_urls = load_feeds()
    if not feed_urls:
        logger.error("No RSS feeds to process. Admin has been notified.")
        sys.exit(1)

    logger.info(f"Processing {len(feed_urls)} feed(s)...")

    # 2. Get all entries
    entries = get_all_entries(feed_urls)
    if not entries:
        logger.warning("No entries found in any feed.")
        sys.exit(0)

    logger.info(f"Total entries found: {len(entries)}")

    # 3. Sort by date
    sorted_entries = sort_entries_by_date(entries)

    # 4. Filter out already processed entries
    new_entries = filter_new_entries(sorted_entries)
    if not new_entries:
        logger.info("No new (unprocessed) entries found.")
        sys.exit(0)

    # 5. Take a random new entry
    random_entry = random.choice(new_entries)
    title = random_entry.get('title', 'Untitled')
    link = random_entry.get('link', 'No link')
    feed = random_entry.get('feed_url', 'Unknown feed')
    pub_date = random_entry.get('published', '')

    logger.info(f"Random new article: '{title}'")
    logger.info(f"From feed: {feed}")
    logger.info(f"Link: {link}")

    # 6. Extract article data
    logger.info("Fetching article content...")
    article_data = get_article_data(random_entry)
    if not article_data:
        logger.error("Failed to fetch article data.")
        sys.exit(1)

    original_title = article_data.get('title', title)
    text = article_data.get('text', '').strip()
    images = article_data.get('images', [])

    if not text:
        logger.warning("Article text is empty, using summary from RSS.")
        # fallback to RSS summary
        summary = latest.get('summary') or latest.get('description') or ''
        import re
        import html
        text = re.sub(r'<[^>]+>', '', summary)
        text = html.unescape(text)

    # 7. Summarize original text first (limit 4096 chars)
    logger.info("Summarizing original text...")
    summarized_raw = summarize_text_with_limit(text, char_limit=4096)
    # 8. Translate title and summarized text with transcreation
    logger.info("Translating with transcreation...")
    translated_title = transcreate_text(original_title, is_title=True)
    translated_summary = transcreate_text(summarized_raw, is_title=False)
    
    # Use translated summary as final text
    summary = translated_summary

    # 9. Post to Telegram
    logger.info("Posting to Telegram...")
    success = send_to_telegram(translated_title, summary, images, link)

    if success:
        logger.info("Successfully posted to Telegram.")
        # Mark as processed
        mark_processed(link, original_title, pub_date)
        logger.info("Article marked as processed in database.")
    else:
        logger.error("Failed to post to Telegram.")
        sys.exit(1)


if __name__ == "__main__":
    main()