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
import asyncio
import time
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
import os
import json
from urllib.parse import urlparse

from mattel_news_source import fetch_mattel_news

# Configuration - set via environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
TELEGRAM_ADMIN_ID = os.getenv('TELEGRAM_ADMIN_ID', '@sunny413x')
TRANSLATOR_SERVICE = 'google'  # or 'libre'
RSS_URL = "https://www.autoevolution.com/rss/tag-Hot+Wheels.xml"
DB_FILE = "news.db"
LOG_LEVEL = logging.INFO


def send_admin_notification(message):
    """Send a notification message to the admin."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_ADMIN_ID:
        logging.error("Telegram credentials or admin ID not set.")
        return False
    async def _send():
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        try:
            await bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=message, parse_mode='Markdown')
            logging.info(f"Admin notification sent: {message[:50]}...")
            return True
        except TelegramError as e:
            logging.error(f"Failed to send admin notification: {e}")
            return False
    return asyncio.run(_send())


def load_feeds():
    """Load RSS feed URLs from feeds.json. If missing or invalid, send admin notification and return empty list."""
    try:
        with open('feeds.json', 'r') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logging.warning(f"feeds.json missing or invalid: {e}. Falling back to default RSS URL.")
        try:
            send_admin_notification(f"⚠️ feeds.json missing or invalid: {e}. Bot has no RSS feed to process.")
        except Exception as notify_err:
            logging.error(f"Failed to send admin notification: {notify_err}")
        return [RSS_URL]

    if not isinstance(data, list):
        logging.warning("feeds.json does not contain a list. Falling back to default RSS URL.")
        try:
            send_admin_notification("⚠️ feeds.json does not contain a list. Bot has no RSS feed to process.")
        except Exception as notify_err:
            logging.error(f"Failed to send admin notification: {notify_err}")
        return [RSS_URL]

    valid_urls = []
    for item in data[:5]:  # limit to first 5
        if not isinstance(item, str):
            logging.warning("feeds.json contains non‑string item. Falling back to default RSS URL.")
            try:
                send_admin_notification("⚠️ feeds.json contains non‑string item. Bot has no RSS feed to process.")
            except Exception as notify_err:
                logging.error(f"Failed to send admin notification: {notify_err}")
            return [RSS_URL]
        parsed = urlparse(item)
        if not (parsed.scheme and parsed.netloc) or parsed.scheme not in ('http', 'https'):
            logging.warning(f"Invalid URL in feeds.json: {item}. Falling back to default RSS URL.")
            try:
                send_admin_notification(f"⚠️ Invalid URL in feeds.json: {item}. Bot has no RSS feed to process.")
            except Exception as notify_err:
                logging.error(f"Failed to send admin notification: {notify_err}")
            return [RSS_URL]
        valid_urls.append(item)

    if not valid_urls:
        logging.warning("feeds.json contains no valid URLs. Falling back to default RSS URL.")
        try:
            send_admin_notification("⚠️ feeds.json contains no valid URLs. Bot has no RSS feed to process.")
        except Exception as notify_err:
            logging.error(f"Failed to send admin notification: {notify_err}")
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
    seen = set()
    for entry in entries:
        link = entry.get('link')
        if link and not is_processed(link) and link not in seen:
            new_entries.append(entry)
            seen.add(link)
    logger.info(f"Found {len(new_entries)} new entries.")
    return new_entries

# Article parsing
def fetch_article(url):
    """Fetch article HTML and parse title, text, images."""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }
        response = requests.get(url, headers=headers, timeout=10)
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

def transcreate_text(text, source='auto', target='ru', is_title=False):
    """
    Translate and adapt text for a lively Russian Telegram channel.

    Google Translate + post-processing:
    - replaces bureaucratic phrasing with plain Russian
    - fixes common Hot Wheels mistranslations (brand names, jargon)
    - flips a few passive constructions to active
    - prepends a single content-aware emoji to titles (deterministic)
    """
    import re

    try:
        translated = GoogleTranslator(source=source, target=target).translate(text)
    except Exception as e:
        logger.error(f"Translation failed in transcreation: {e}")
        translated = text

    if not translated or not translated.strip():
        return text

    result = translated

    # Bureaucratic → plain Russian
    bureaucratic = {
        r'является': 'это',
        r'осуществляется': 'происходит',
        r'представляет собой': 'это',
        r'в рамках': 'в',
        r'в процессе': 'во время',
        r'в ходе': 'во время',
        r'на сегодняшний день': 'сейчас',
        r'в настоящее время': 'сейчас',
        r'на данный момент': 'сейчас',
        r'как правило': 'обычно',
        r'в связи с тем,?\s+что': 'так как',
        r'в целях': 'чтобы',
        r'с целью': 'чтобы',
        r'в случае,?\s+если': 'если',
        r'по итогам': 'после',
        r'имеет возможность': 'может',
        r'получат возможность': 'смогут',
        r'тем не менее': 'но',
        r'при этом': 'и',
    }
    for pattern, repl in bureaucratic.items():
        result = re.sub(pattern, repl, result, flags=re.IGNORECASE)

    # Passive → active
    result = re.sub(r'был выполн[еён]', 'сделали', result, flags=re.IGNORECASE)
    result = re.sub(r'был представлен', 'представили', result, flags=re.IGNORECASE)
    result = re.sub(r'было объявлено', 'объявили', result, flags=re.IGNORECASE)
    result = re.sub(r'был запущен', 'запустили', result, flags=re.IGNORECASE)

    # Hot Wheels domain glossary — fix recurring Google Translate mistakes.
    # Keeps brand names in English (fandom convention) and fixes terms Google
    # mangles ("garage build" → "гаражный проект", not "сборка гаража").
    hw_glossary = {
        r'\bХот[-\s]?[УВв]илс\b': 'Hot Wheels',
        r'\bхот[-\s]?колёс\b': 'Hot Wheels',
        r'\bсборка гаража\b': 'гаражный проект',
        r'\bсборки гаража\b': 'гаражного проекта',
        r'\bсборке гаража\b': 'гаражному проекту',
        r'\bсборкой гаража\b': 'гаражным проектом',
        r'\bлитой автомобиль\b': 'дайкаст-модель',
        r'\bлитого автомобиля\b': 'дайкаст-модели',
        r'\bлитому автомобилю\b': 'дайкаст-модели',
        r'\bлитым автомобилем\b': 'дайкаст-моделью',
        r'\b[Тт]ур легенд\b': 'Legends Tour',
        r'\bлегендарный тур\b': 'Legends Tour',
        r'\bтур\s+(Hot Wheels[™®]?\s+Legends Tour)': r'\1',
        r'\bтеперь принимает заявки\b': 'открывает приём заявок',
        r'\bтеперь принимает заявления\b': 'открывает приём заявок',
    }
    for pattern, repl in hw_glossary.items():
        result = re.sub(pattern, repl, result, flags=re.IGNORECASE)

    # Titles get a single emoji prefix chosen by content (deterministic).
    if is_title:
        t = result.lower()
        if re.search(r'легенд|legends|tour|чемпион|приз|победител', t):
            emoji = '🏆'
        elif re.search(r'гонк|скорост|race|ралли', t):
            emoji = '🏎️'
        elif re.search(r'релиз|выпуск|launch|запуск|вышел|выходит|дебют', t):
            emoji = '🚀'
        elif re.search(r'коллекц|серия|series|collection', t):
            emoji = '💎'
        elif re.search(r'сотруднич|партнёр|collab|partner', t):
            emoji = '🤝'
        elif re.search(r'анонс|объявл|представля|announce', t):
            emoji = '📢'
        elif re.search(r'машин|автомобил|модел|\bcar\b', t):
            emoji = '🚗'
        else:
            emoji = '🔥'
        return f"{emoji} {result}"

    # Body: truncate to 4000 chars on a sentence boundary.
    if len(result) > 4000:
        window = result[:4000]
        match = re.search(r'[.!?][\s\n]', window[::-1])
        if match:
            cut_pos = 4000 - match.start() - 1
            result = result[:cut_pos]
        else:
            last_space = window.rfind(' ')
            if last_space != -1:
                result = result[:last_space]
            else:
                result = result[:4000]

    return result

# Summarization (simple extractive)
def summarize_text(text, sentences=5):
    """Extract first N sentences as summary."""
    import re
    # naive sentence split
    sentences_list = re.split(r'(?<=[.!?])\s+', text)
    summary = ' '.join(sentences_list[:sentences])
    return summary

def summarize_text_with_limit(text, char_limit=4096):
    """
    Create an extractive summary of the text by selecting whole sentences
    up to the character limit, preserving author style.
    Returns the summary (may be shorter than char_limit).
    """
    import re
    if len(text) <= char_limit:
        return text
    
    # Split into sentences (naive split at punctuation followed by whitespace)
    sentences = re.split(r'(?<=[.!?])\s+', text)
    
    summary_parts = []
    current_len = 0
    
    for sent in sentences:
        sent_len = len(sent)
        # Add a space before if not the first sentence
        extra = 1 if summary_parts else 0
        if current_len + sent_len + extra <= char_limit:
            summary_parts.append(sent)
            current_len += sent_len + extra
        else:
            # Cannot add this sentence without exceeding limit
            break
    
    if summary_parts:
        summary = ' '.join(summary_parts)
        # Ensure we didn't exceed limit due to extra spaces (should not happen)
        if len(summary) > char_limit:
            # fallback to truncation at last sentence boundary
            # Find the last sentence boundary before char_limit
            window = summary[:char_limit]
            match = re.search(r'[.!?][\s\n]', window[::-1])
            if match:
                cut_pos = char_limit - match.start() - 1
                summary = summary[:cut_pos]
            else:
                last_space = window.rfind(' ')
                if last_space != -1:
                    summary = summary[:last_space]
                else:
                    summary = summary[:char_limit]
        return summary
    
    # If no sentences could be added (first sentence longer than char_limit),
    # fall back to truncation at the last space before limit.
    window = text[:char_limit]
    last_space = window.rfind(' ')
    if last_space != -1:
        return text[:last_space]
    # Otherwise hard cut
    return text[:char_limit]

# Telegram posting
def send_to_telegram(title, summary, images, original_link):
    """Send formatted post to Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        logger.error("Telegram credentials not set.")
        return False

    async def _send():
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        message = f"*{title}*\n\n{summary}\n\nИсточник: [читать оригинал]({original_link})"
        try:
            if images:
                await bot.send_photo(chat_id=TELEGRAM_CHANNEL_ID, photo=images[0], caption=message, parse_mode='Markdown')
            else:
                await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=message, parse_mode='Markdown')
            logger.info(f"Posted to Telegram: {title}")
            return True
        except TelegramError as e:
            logger.error(f"Telegram error: {e}")
            return False

    return asyncio.run(_send())

# Processing pipeline
def get_article_data(entry):
    """Extract article title, text, and images from entry, trying fetch first."""
    link = entry.get('link')
    # try fetching full article
    article = fetch_article(link)
    if article:
        return article
    # fallback to RSS summary
    import re
    import html
    summary = entry.get('summary') or entry.get('description') or ''
    # strip HTML tags
    text = re.sub(r'<[^>]+>', '', summary)
    # decode HTML entities
    text = html.unescape(text)
    return {
        'title': entry.get('title', ''),
        'text': text,
        'images': []
    }

def process_new_articles(entries, limit=3):
    """Process up to limit new articles."""
    count = 0
    for entry in entries[:limit]:
        link = entry.get('link')
        title = entry.get('title')
        pub_date = entry.get('published', '')
        
        feed_url = entry.get('feed_url', 'unknown')
        logger.info(f"Processing: {title} (from {feed_url})")
        article = get_article_data(entry)
        
        # Summarize original text first (limit 4096 chars)
        summarized_raw = summarize_text_with_limit(article['text'], char_limit=4096)
        # Translate title and summarized text with transcreation
        translated_title = transcreate_text(article['title'], is_title=True)
        translated_summary = transcreate_text(summarized_raw, is_title=False)
        
        # Use translated summary as final text
        summary = translated_summary
        
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
        logger.warning("No RSS feeds to process. Falling back to default RSS URL.")
        feed_urls = [RSS_URL]
    logger.info(f"Processing {len(feed_urls)} feeds...")
    all_entries = []
    for i, url in enumerate(feed_urls, 1):
        logger.info(f"Fetching feed {i}/{len(feed_urls)}: {url}")
        try:
            entries = fetch_rss(url)
        except Exception as e:
            logger.error(f"Failed to fetch feed {url}: {e}")
            entries = []
        for entry in entries:
            entry['feed_url'] = url
        all_entries.extend(entries)

    mattel_entries = fetch_mattel_news(notifier=send_admin_notification)
    logger.info(f"Fetched {len(mattel_entries)} entries from Mattel corporate news")
    all_entries.extend(mattel_entries)

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