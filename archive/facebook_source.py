#!/usr/bin/env python3
"""
[АРХИВ — 2026-04-20] Facebook Hot Wheels source.

Фича отменена: Facebook требует авторизации для публичного контента,
RSS отключён, Graph API требует Developer App и регулярного обновления токенов.
См. archive/README.md.

Configuration loader for Facebook Hot Wheels source.
"""

import json
import os
import logging
import feedparser
import requests
import time
import calendar
from datetime import datetime, timezone
from urllib.parse import urlparse

# Default values
DEFAULT_FILTER_KEYWORDS = ["event", "announcement", "coming soon", "glow‑n‑fire"]
DEFAULT_ENABLED = True
DEFAULT_METHOD_PRIORITY = ["rss", "graph_api", "html"]
ALLOWED_METHODS = {"rss", "graph_api", "html"}
CONFIG_FILENAME = "facebook_source.json"


def load_config(config_path=None):
    """
    Load and validate Facebook source configuration from JSON file.

    Args:
        config_path (str, optional): Path to config file. If None, looks for
            CONFIG_FILENAME in the current working directory.

    Returns:
        dict: Configuration dictionary with keys:
            - page_url (str): required Facebook page URL
            - filter_keywords (list): optional, default DEFAULT_FILTER_KEYWORDS
            - enabled (bool): optional, default DEFAULT_ENABLED
            - method_priority (list): optional, default DEFAULT_METHOD_PRIORITY
            - access_token (str or None): optional, from config or env var

    Raises:
        FileNotFoundError: if config file does not exist
        json.JSONDecodeError: if config file contains invalid JSON
        ValueError: if required field missing or validation fails
    """
    if config_path is None:
        config_path = CONFIG_FILENAME

    # 1. Read file
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
    except FileNotFoundError:
        logging.error(f"Configuration file {config_path} not found.")
        raise
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON in {config_path}: {e}")
        raise

    # 2. Validate required field
    if "page_url" not in config_data:
        raise ValueError("Missing required field 'page_url' in configuration.")
    page_url = config_data["page_url"]
    if not isinstance(page_url, str):
        raise ValueError("Field 'page_url' must be a string.")
    # Basic URL validation
    parsed = urlparse(page_url)
    if not (parsed.scheme and parsed.netloc):
        raise ValueError(f"Invalid URL format for 'page_url': {page_url}")

    # 3. Apply defaults for optional fields
    filter_keywords = config_data.get("filter_keywords", DEFAULT_FILTER_KEYWORDS)
    if not isinstance(filter_keywords, list):
        raise ValueError("Field 'filter_keywords' must be a list of strings.")
    for kw in filter_keywords:
        if not isinstance(kw, str):
            raise ValueError("All items in 'filter_keywords' must be strings.")

    enabled = config_data.get("enabled", DEFAULT_ENABLED)
    if not isinstance(enabled, bool):
        raise ValueError("Field 'enabled' must be a boolean.")

    method_priority = config_data.get("method_priority", DEFAULT_METHOD_PRIORITY)
    if not isinstance(method_priority, list):
        raise ValueError("Field 'method_priority' must be a list of strings.")
    for method in method_priority:
        if method not in ALLOWED_METHODS:
            raise ValueError(f"Invalid method '{method}' in 'method_priority'. Allowed: {sorted(ALLOWED_METHODS)}")

    # 4. Access token: config -> environment variable -> None
    access_token = config_data.get("access_token")
    if access_token is None:
        access_token = os.getenv("FACEBOOK_ACCESS_TOKEN")  # returns None if not set
    if access_token is not None and not isinstance(access_token, str):
        raise ValueError("Field 'access_token' must be a string or null.")

    # 5. Return validated config
    return {
        "page_url": page_url,
        "filter_keywords": filter_keywords,
        "enabled": enabled,
        "method_priority": method_priority,
        "access_token": access_token
    }


def fetch_facebook_rss(page_url):
    """
    Fetch and parse Facebook page RSS feed.

    Args:
        page_url (str): Facebook page URL (e.g., https://www.facebook.com/hotwheels/)

    Returns:
        list[dict]: List of uniform entries with keys: link, title, published, message, images.
    """
    logger = logging.getLogger(__name__)
    rss_url = page_url.rstrip('/') + '/rss'
    try:
        feed = feedparser.parse(rss_url)
        if feed.bozo:
            logger.warning(f"RSS feed parse warning for {rss_url}: {feed.bozo_exception}")
            return []
    except Exception as e:
        logger.error(f"Failed to fetch RSS from {rss_url}: {e}")
        return []

    entries = []
    for item in feed.entries:
        # Extract images from media_content
        images = []
        for media in item.get('media_content', []):
            if media.get('type', '').startswith('image'):
                images.append(media.get('url'))
        # Fallback to enclosure
        if not images:
            for enc in item.get('enclosures', []):
                if enc.get('type', '').startswith('image'):
                    images.append(enc.get('href'))
        # Convert published_parsed to datetime
        pub_dt = None
        published_parsed = item.get('published_parsed')
        if published_parsed:
            pub_dt = datetime.fromtimestamp(calendar.timegm(published_parsed), tz=timezone.utc).replace(tzinfo=None)
        else:
            pub_dt = datetime.now(timezone.utc).replace(tzinfo=None)

        entry = {
            'link': item.get('link', ''),
            'title': item.get('title', ''),
            'published': pub_dt,
            'message': item.get('summary', ''),
            'images': images
        }
        entries.append(entry)
    return entries


def fetch_facebook_graph(page_id, access_token):
    """
    Fetch posts from Facebook Graph API.

    Args:
        page_id (str): Facebook page ID.
        access_token (str): Facebook Graph API access token.

    Returns:
        list[dict]: List of uniform entries with keys: link, title, published, message, images.
    """
    logger = logging.getLogger(__name__)
    base_url = f"https://graph.facebook.com/{page_id}/posts"
    params = {
        'access_token': access_token,
        'fields': 'id,permalink_url,message,created_time,attachments',
        'limit': 100
    }
    entries = []
    retries = 3
    for attempt in range(retries):
        try:
            response = requests.get(base_url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
            if 'error' in data:
                error_msg = data['error'].get('message', 'Unknown error')
                logger.error(f"Graph API error: {error_msg}")
                return []
            posts = data.get('data', [])
            for post in posts:
                # Extract images from attachments (up to 2)
                images = []
                attachments = post.get('attachments', {}).get('data', [])
                for att in attachments:
                    if att.get('type') == 'photo' and 'media' in att:
                        image_url = att['media'].get('image', {}).get('src')
                        if image_url:
                            images.append(image_url)
                            if len(images) >= 2:
                                break
                # Convert created_time to datetime (naive UTC)
                created_str = post.get('created_time')
                pub_dt = datetime.now(timezone.utc).replace(tzinfo=None)
                if created_str:
                    try:
                        # ISO 8601 with timezone e.g., 2026-04-01T12:00:00+0000
                        pub_dt = datetime.strptime(created_str, '%Y-%m-%dT%H:%M:%S%z')
                        # Convert to naive UTC datetime
                        if pub_dt.tzinfo is not None:
                            pub_dt = pub_dt.astimezone(timezone.utc).replace(tzinfo=None)
                    except ValueError:
                        logger.warning(f"Could not parse date: {created_str}")
                entry = {
                    'link': post.get('permalink_url', ''),
                    'title': (post.get('message', '')[:50] if post.get('message') else ''),
                    'published': pub_dt,
                    'message': post.get('message', ''),
                    'images': images
                }
                entries.append(entry)
            break  # success, exit retry loop
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else None
            if status_code == 429:  # rate limit
                logger.warning(f"Rate limited, attempt {attempt + 1}/{retries}")
                if attempt < retries - 1:
                    wait_time = 2 ** (attempt + 1)  # exponential backoff
                    time.sleep(wait_time)
                    continue
                else:
                    logger.error("Rate limit retries exhausted")
                    return []
            elif status_code == 400:  # invalid token
                logger.error(f"Invalid token: {e}")
                return []
            else:
                logger.error(f"HTTP error fetching Graph API: {e}")
                return []
        except Exception as e:
            logger.error(f"Unexpected error fetching Graph API: {e}")
            return []
    return entries


if __name__ == "__main__":
    # Quick smoke test when run directly
    try:
        config = load_config()
        print("Configuration loaded successfully:")
        for key, value in config.items():
            print(f"  {key}: {value}")
    except Exception as e:
        print(f"Error loading config: {e}")
        exit(1)