#!/usr/bin/env python3
"""
Unit tests for Facebook source configuration loader.
"""

import json
import os
import pytest
import tempfile
import shutil
import requests
from datetime import datetime
from unittest.mock import patch, Mock

# The module we're testing (will be created later)
from facebook_source import load_config, fetch_facebook_rss, fetch_facebook_graph


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


class TestFetchFacebookRss:
    """Test suite for fetch_facebook_rss function."""

    @patch('facebook_source.feedparser.parse')
    def test_fetch_facebook_rss_success(self, mock_parse):
        """Mock RSS feed response, verify entry extraction."""
        from time import struct_time
        # Create a mock feed with one entry
        mock_feed = Mock()
        mock_feed.bozo = False
        mock_feed.entries = [
            {
                'link': 'https://www.facebook.com/hotwheels/posts/123',
                'title': 'New Hot Wheels Collection',
                'published': 'Mon, 01 Apr 2026 12:00:00 GMT',
                'published_parsed': struct_time((2026, 4, 1, 12, 0, 0, 0, 91, 0)),
                'summary': 'Check out our new collection!',
                'media_content': [{'url': 'https://example.com/image1.jpg', 'type': 'image/jpeg'}]
            }
        ]
        mock_parse.return_value = mock_feed

        result = fetch_facebook_rss('https://www.facebook.com/hotwheels')
        assert len(result) == 1
        entry = result[0]
        assert entry['link'] == 'https://www.facebook.com/hotwheels/posts/123'
        assert entry['title'] == 'New Hot Wheels Collection'
        # published should be a datetime object
        assert entry['published'] == datetime(2026, 4, 1, 12, 0, 0)
        assert entry['message'] == 'Check out our new collection!'
        assert entry['images'] == ['https://example.com/image1.jpg']

    @patch('facebook_source.requests.get')
    def test_fetch_facebook_rss_http_error(self, mock_get):
        """Mock 404, returns empty list, logs warning."""
        mock_response = Mock()
        mock_response.raise_for_status.side_effect = requests.HTTPError('404')
        mock_get.return_value = mock_response

        result = fetch_facebook_rss('https://www.facebook.com/hotwheels')
        assert result == []

    @patch('facebook_source.feedparser.parse')
    def test_fetch_facebook_rss_malformed(self, mock_parse):
        """Malformed RSS returns empty list."""
        mock_feed = Mock()
        mock_feed.bozo = True
        mock_feed.bozo_exception = Exception('Malformed feed')
        mock_parse.return_value = mock_feed

        result = fetch_facebook_rss('https://www.facebook.com/hotwheels')
        assert result == []


class TestFetchFacebookGraph:
    """Test suite for fetch_facebook_graph function."""

    @patch('facebook_source.requests.get')
    def test_fetch_facebook_graph_success(self, mock_get):
        """Mock Graph API response, verify mapping."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': [
                {
                    'id': '123',
                    'permalink_url': 'https://facebook.com/hotwheels/posts/123',
                    'message': 'New Hot Wheels announcement',
                    'created_time': '2026-04-01T12:00:00+0000',
                    'attachments': {
                        'data': [
                            {'type': 'photo', 'media': {'image': {'src': 'https://example.com/image1.jpg'}}}
                        ]
                    }
                }
            ]
        }
        mock_get.return_value = mock_response

        result = fetch_facebook_graph('123', 'dummy_token')
        assert len(result) == 1
        entry = result[0]
        assert entry['link'] == 'https://facebook.com/hotwheels/posts/123'
        # title should be first 50 chars of message
        assert entry['title'] == 'New Hot Wheels announcement'[:50]
        assert entry['published'] == datetime(2026, 4, 1, 12, 0, 0)
        assert entry['message'] == 'New Hot Wheels announcement'
        assert entry['images'] == ['https://example.com/image1.jpg']

    @patch('facebook_source.requests.get')
    def test_fetch_facebook_graph_invalid_token(self, mock_get):
        """Mock 400 error (invalid token), raises appropriate exception or returns empty list."""
        mock_response = Mock()
        mock_response.status_code = 400
        mock_response.raise_for_status.side_effect = requests.HTTPError('Invalid token')
        mock_get.return_value = mock_response

        result = fetch_facebook_graph('123', 'invalid_token')
        # Expect empty list or exception? According to spec, returns empty list or raises appropriate exception.
        # We'll assume returns empty list.
        assert result == []

    @patch('facebook_source.requests.get')
    def test_fetch_facebook_graph_rate_limit(self, mock_get):
        """Mock 429, verify exponential backoff and retry."""
        from unittest.mock import call
        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.raise_for_status.side_effect = requests.HTTPError('Rate limit exceeded', response=mock_response)
        mock_get.return_value = mock_response

        # We'll need to mock time.sleep to avoid actual sleep
        with patch('facebook_source.time.sleep') as mock_sleep:
            result = fetch_facebook_graph('123', 'token')
            # Expect empty list after retries
            assert result == []
            # Verify sleep called with exponential backoff
            assert mock_sleep.call_count > 0

    @patch('facebook_source.requests.get')
    def test_fetch_facebook_graph_no_images(self, mock_get):
        """Posts without attachments produce empty images list."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'data': [
                {
                    'id': '123',
                    'permalink_url': 'https://facebook.com/hotwheels/posts/123',
                    'message': 'No images',
                    'created_time': '2026-04-01T12:00:00+0000',
                    'attachments': {}
                }
            ]
        }
        mock_get.return_value = mock_response

        result = fetch_facebook_graph('123', 'token')
        entry = result[0]
        assert entry['images'] == []


if __name__ == '__main__':
    pytest.main([__file__, '-v'])