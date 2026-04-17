#!/usr/bin/env python3
"""
Unit tests for Facebook source configuration loader.
"""

import json
import os
import pytest
import tempfile
import shutil
from unittest.mock import patch

# The module we're testing (will be created later)
from facebook_source import load_config


class TestLoadConfig:
    """Test suite for load_config function."""

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Create a temporary directory for each test and clean up."""
        self.temp_dir = tempfile.mkdtemp()
        self.original_cwd = os.getcwd()
        os.chdir(self.temp_dir)
        yield
        os.chdir(self.original_cwd)
        shutil.rmtree(self.temp_dir)

    def write_config(self, content):
        """Helper to write facebook_source.json in current directory."""
        with open('facebook_source.json', 'w') as f:
            json.dump(content, f)

    def test_load_config_valid(self):
        """Load a valid config file with all required fields."""
        config_data = {
            "page_url": "https://www.facebook.com/hotwheels/",
            "filter_keywords": ["event", "announcement"],
            "enabled": True,
            "method_priority": ["rss", "graph_api", "html"],
            "access_token": "dummy_token"
        }
        self.write_config(config_data)

        result = load_config()
        assert result["page_url"] == "https://www.facebook.com/hotwheels/"
        assert result["filter_keywords"] == ["event", "announcement"]
        assert result["enabled"] is True
        assert result["method_priority"] == ["rss", "graph_api", "html"]
        assert result["access_token"] == "dummy_token"

    def test_load_config_missing_file(self):
        """When config file does not exist, raise FileNotFoundError."""
        # Ensure file does not exist
        assert not os.path.exists('facebook_source.json')
        with pytest.raises(FileNotFoundError):
            load_config()

    def test_load_config_invalid_json(self):
        """Invalid JSON content raises JSONDecodeError."""
        with open('facebook_source.json', 'w') as f:
            f.write('{ invalid json')
        with pytest.raises(json.JSONDecodeError):
            load_config()

    def test_load_config_missing_required_field(self):
        """Missing required field (page_url) raises ValueError."""
        config_data = {
            "filter_keywords": ["event"]
        }
        self.write_config(config_data)
        with pytest.raises(ValueError) as exc_info:
            load_config()
        assert "page_url" in str(exc_info.value)

    def test_load_config_token_from_env(self):
        """Token is read from environment variable when not in config."""
        config_data = {
            "page_url": "https://www.facebook.com/hotwheels/"
        }
        self.write_config(config_data)
        with patch.dict(os.environ, {"FACEBOOK_ACCESS_TOKEN": "env_token"}):
            result = load_config()
        assert result["access_token"] == "env_token"

    def test_load_config_token_precedence(self):
        """Config file token takes precedence over environment variable."""
        config_data = {
            "page_url": "https://www.facebook.com/hotwheels/",
            "access_token": "file_token"
        }
        self.write_config(config_data)
        with patch.dict(os.environ, {"FACEBOOK_ACCESS_TOKEN": "env_token"}):
            result = load_config()
        assert result["access_token"] == "file_token"

    def test_load_config_defaults(self):
        """Optional fields get default values when missing."""
        config_data = {
            "page_url": "https://www.facebook.com/hotwheels/"
        }
        self.write_config(config_data)
        result = load_config()
        assert result["filter_keywords"] == ["event", "announcement", "coming soon", "glow‑n‑fire"]
        assert result["enabled"] is True
        assert result["method_priority"] == ["rss", "graph_api", "html"]
        assert result["access_token"] is None

    def test_load_config_empty_keywords(self):
        """Empty filter_keywords list is allowed."""
        config_data = {
            "page_url": "https://www.facebook.com/hotwheels/",
            "filter_keywords": []
        }
        self.write_config(config_data)
        result = load_config()
        assert result["filter_keywords"] == []

    def test_load_config_invalid_url(self):
        """Invalid page_url raises ValueError."""
        config_data = {
            "page_url": "not-a-url"
        }
        self.write_config(config_data)
        with pytest.raises(ValueError) as exc_info:
            load_config()
        assert "page_url" in str(exc_info.value)

    def test_load_config_invalid_method_priority(self):
        """Invalid method priority raises ValueError."""
        config_data = {
            "page_url": "https://www.facebook.com/hotwheels/",
            "method_priority": ["unknown"]
        }
        self.write_config(config_data)
        with pytest.raises(ValueError) as exc_info:
            load_config()
        assert "method_priority" in str(exc_info.value)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])