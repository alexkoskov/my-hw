#!/usr/bin/env python3
"""
Extract text of the latest article from RSS feeds listed in feeds.json.
Uses existing functions from news_bot.py.
"""

import sys
import logging
from datetime import datetime
import feedparser

# Import functions from news_bot.py
try:
    from news_bot import load_feeds, fetch_rss, get_article_data
except ImportError as e:
    print(f"Failed to import from news_bot.py: {e}")
    sys.exit(1)

# Setup minimal logging to avoid interference
logging.basicConfig(level=logging.WARNING)


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
    now = datetime.utcnow()
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
    # 1. Load feeds
    feed_urls = load_feeds()
    if not feed_urls:
        print("No feeds found. Using default RSS URL.")
        # fallback to the default RSS URL from news_bot.py
        from news_bot import RSS_URL
        feed_urls = [RSS_URL]

    print(f"Processing {len(feed_urls)} feed(s)...")

    # 2. Get all entries
    entries = get_all_entries(feed_urls)
    if not entries:
        print("No entries found in any feed.")
        sys.exit(0)

    print(f"Total entries found: {len(entries)}")

    # 3. Sort by date
    sorted_entries = sort_entries_by_date(entries)

    # 4. Take the latest entry
    latest = sorted_entries[0]
    title = latest.get('title', 'Untitled')
    link = latest.get('link', 'No link')
    feed = latest.get('feed_url', 'Unknown feed')
    print(f"Latest article: '{title}'")
    print(f"From feed: {feed}")
    print(f"Link: {link}")

    # 5. Extract article data
    print("Fetching article content...")
    article_data = get_article_data(latest)
    if not article_data:
        print("Failed to fetch article data.")
        sys.exit(1)

    text = article_data.get('text', '').strip()
    if not text:
        print("Article text is empty.")
        sys.exit(1)

    # 6. Truncate if too long (optional)
    if len(text) > 5000:
        text = text[:5000] + "..."

    # 7. Output the text
    print("\n--- Article text ---")
    print(text)
    print("--- End of text ---")


if __name__ == "__main__":
    main()