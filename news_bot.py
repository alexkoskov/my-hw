#!/usr/bin/env python3
"""
Automated news collector and Telegram poster.
Fetches RSS feed, parses articles, translates, summarizes, and posts to Telegram.
"""

import sqlite3
import logging
import feedparser
import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
import schedule
import time
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
import os
import json
from urllib.parse import urlparse

# Configuration - set via environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TRANSLATOR_SERVICE = 'google'  # or 'libre'
RSS_URL = "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"
DB_FILE = "news.db"
LOG_LEVEL = logging.INFO


def load_feeds():
    """Load RSS feed URLs from feeds.json, fall back to hardcoded RSS_URL if missing or invalid."""
    try:
        with open('feeds.json', 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return [RSS_URL]

    if not isinstance(data, list):
        return [RSS_URL]

    valid_urls = []
    for item in data[:5]:  # limit to first 5
        if not isinstance(item, str):
            return [RSS_URL]
        parsed = urlparse(item)
        if not (parsed.scheme and parsed.netloc) or parsed.scheme not in ('http', 'https'):
            return [RSS_URL]
        valid_urls.append(item)

    if not valid_urls:
        return [RSS_URL]
    return valid_urls


# Setup logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database functions
def init_db():
    """Create SQLite table for processed news if not exists."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS processed_news
                 (link TEXT PRIMARY KEY, title TEXT, pub_date TEXT, processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()
    logger.info("Database initialized.")

def is_processed(link):
    """Check if a news item has already been processed."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT 1 FROM processed_news WHERE link = ?", (link,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_processed(link, title, pub_date):
    """Mark a news item as processed."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO processed_news (link, title, pub_date) VALUES (?, ?, ?)",
              (link, title, pub_date))
    conn.commit()
    conn.close()
    logger.debug(f"Marked as processed: {link}")

# RSS functions
def fetch_rss(url):
    """Fetch and parse RSS feed, return list of entries."""
    try:
        feed = feedparser.parse(url)
        if feed.bozo:
            logger.warning(f"RSS feed parse warning for {url}: {feed.bozo_exception}")
        return feed.entries
    except Exception as e:
        logger.error(f"Failed to fetch RSS from {url}: {e}")
        return []

def filter_new_entries(entries):
    """Filter entries that are not already processed."""
    new_entries = []
    for entry in entries:
        link = entry.get('link')
        if link and not is_processed(link):
            new_entries.append(entry)
    logger.info(f"Found {len(new_entries)} new entries.")
    return new_entries

# Article parsing
def fetch_article(url):
    """Fetch article HTML and parse title, text, images."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Extract title
        title = soup.find('h1').get_text(strip=True) if soup.find('h1') else ''
        
        # Extract article body - adjust selector based on actual site structure
        article_body = soup.find('article')
        if not article_body:
            article_body = soup.find('div', class_='article-content')
        text = article_body.get_text(strip=True) if article_body else ''
        
        # Extract images (main article images)
        images = []
        img_tags = article_body.find_all('img') if article_body else []
        for img in img_tags:
            src = img.get('src')
            if src and src.startswith('http'):
                images.append(src)
        
        return {
            'title': title,
            'text': text,
            'images': images[:3]  # keep up to 3
        }
    except Exception as e:
        logger.error(f"Failed to fetch article {url}: {e}")
        return None

# Translation
def translate_text(text, source='auto', target='ru'):
    """Translate text using Google Translate."""
    try:
        translator = GoogleTranslator(source=source, target=target)
        translated = translator.translate(text)
        return translated
    except Exception as e:
        logger.error(f"Translation failed: {e}")
        return text  # fallback to original

# Summarization (simple extractive)
def summarize_text(text, sentences=5):
    """Extract first N sentences as summary."""
    import re
    # naive sentence split
    sentences_list = re.split(r'(?<=[.!?])\s+', text)
    summary = ' '.join(sentences_list[:sentences])
    return summary

# Telegram posting
def send_to_telegram(title, summary, images, original_link):
    """Send formatted post to Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.error("Telegram credentials not set.")
        return False
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    message = f"*{title}*\n\n{summary}\n\nИсточник: [читать оригинал]({original_link})"
    
    try:
        if images:
            # Send photo with caption
            bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=images[0], caption=message, parse_mode='Markdown')
            # If more images, send as media group (optional)
            # For simplicity, we only send first image
        else:
            bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=message, parse_mode='Markdown')
        logger.info(f"Posted to Telegram: {title}")
        return True
    except TelegramError as e:
        logger.error(f"Telegram error: {e}")
        return False

# Processing pipeline
def process_new_articles(entries, limit=3):
    """Process up to limit new articles."""
    count = 0
    for entry in entries[:limit]:
        link = entry.get('link')
        title = entry.get('title')
        pub_date = entry.get('published', '')
        
        feed_url = entry.get('feed_url', 'unknown')
        logger.info(f"Processing: {title} (from {feed_url})")
        article = fetch_article(link)
        if not article:
            continue
        
        # Translate title and text
        translated_title = translate_text(article['title'])
        translated_text = translate_text(article['text'])
        
        # Summarize
        summary = summarize_text(translated_text, sentences=5)
        
        # Post to Telegram
        success = send_to_telegram(translated_title, summary, article['images'], link)
        if success:
            mark_processed(link, title, pub_date)
            count += 1
        else:
            logger.warning(f"Failed to post {link}, skipping.")
    return count

# Scheduler
def job():
    """Main job to run daily."""
    logger.info("Starting daily news collection...")
    feed_urls = load_feeds()
    if not feed_urls:
        feed_urls = [RSS_URL]
    logger.info(f"Processing {len(feed_urls)} feeds...")
    all_entries = []
    for i, url in enumerate(feed_urls, 1):
        logger.info(f"Fetching feed {i}/{len(feed_urls)}: {url}")
        entries = fetch_rss(url)
        for entry in entries:
            entry['feed_url'] = url
        all_entries.extend(entries)
    new_entries = filter_new_entries(all_entries)
    processed = process_new_articles(new_entries, limit=3)
    logger.info(f"Job finished. Processed {processed} new articles.")

def main():
    """Entry point."""
    init_db()
    logger.info("News bot started.")
    
    # Schedule daily at 12:00 local time (adjust as needed)
    schedule.every().day.at("12:00").do(job)
    
    # Run immediately for testing (comment out in production)
    job()
    
    # Keep the script alive
    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()