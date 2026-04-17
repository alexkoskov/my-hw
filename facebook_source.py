#!/usr/bin/env python3
"""
Configuration loader for Facebook Hot Wheels source.
"""

import json
import os
import logging
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