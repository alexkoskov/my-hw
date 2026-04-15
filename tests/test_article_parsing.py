#!/usr/bin/env python3
"""
Unit tests for article parsing (fetch_article).
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news_bot import fetch_article


class TestFetchArticle(unittest.TestCase):
    """Test fetch_article function."""

    @patch('news_bot.requests.get')
    def test_successful_fetch(self, mock_get):
        """fetch_article returns title, text, images when HTML is valid."""
        # Mock HTML content
        html_content = """
        <html>
            <body>
                <h1>Test Article Title</h1>
                <article>
                    <p>First paragraph.</p>
                    <p>Second paragraph.</p>
                    <img src="http://example.com/image1.jpg">
                    <img src="http://example.com/image2.jpg">
                </article>
            </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html_content
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')

        self.assertIsNotNone(result)
        self.assertEqual(result['title'], 'Test Article Title')
        self.assertIn('First paragraph.', result['text'])
        self.assertIn('Second paragraph.', result['text'])
        self.assertEqual(len(result['images']), 2)
        self.assertEqual(result['images'][0], 'http://example.com/image1.jpg')
        self.assertEqual(result['images'][1], 'http://example.com/image2.jpg')
        mock_get.assert_called_once_with('http://example.com/article', timeout=10)

    @patch('news_bot.requests.get')
    def test_no_h1_fallback(self, mock_get):
        """fetch_article returns empty title if no h1 tag."""
        html_content = """
        <html>
            <body>
                <article>
                    <p>Content.</p>
                </article>
            </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html_content
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')
        self.assertEqual(result['title'], '')

    @patch('news_bot.requests.get')
    def test_no_article_tag_fallback_to_div(self, mock_get):
        """fetch_article tries to find div with class article-content if article not found."""
        html_content = """
        <html>
            <body>
                <h1>Title</h1>
                <div class="article-content">
                    <p>Content inside div.</p>
                </div>
            </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html_content
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')
        self.assertIn('Content inside div.', result['text'])

    @patch('news_bot.requests.get')
    def test_no_article_body_found(self, mock_get):
        """fetch_article returns empty text if no article body found."""
        html_content = """
        <html>
            <body>
                <h1>Title</h1>
            </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html_content
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')
        self.assertEqual(result['text'], '')

    @patch('news_bot.requests.get')
    def test_images_filtered_only_http(self, mock_get):
        """Only images with http/https src are collected."""
        html_content = """
        <html>
            <body>
                <h1>Title</h1>
                <article>
                    <img src="http://example.com/valid.jpg">
                    <img src="https://example.com/valid2.jpg">
                    <img src="/relative/path.jpg">
                    <img src="data:image/png;base64,...">
                </article>
            </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html_content
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')
        self.assertEqual(len(result['images']), 2)
        self.assertIn('http://example.com/valid.jpg', result['images'])
        self.assertIn('https://example.com/valid2.jpg', result['images'])

    @patch('news_bot.requests.get')
    def test_images_limited_to_three(self, mock_get):
        """Only up to three images are kept."""
        html_content = """
        <html>
            <body>
                <h1>Title</h1>
                <article>
                    <img src="http://example.com/1.jpg">
                    <img src="http://example.com/2.jpg">
                    <img src="http://example.com/3.jpg">
                    <img src="http://example.com/4.jpg">
                </article>
            </body>
        </html>
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = html_content
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')
        self.assertEqual(len(result['images']), 3)
        self.assertEqual(result['images'][0], 'http://example.com/1.jpg')
        self.assertEqual(result['images'][2], 'http://example.com/3.jpg')

    @patch('news_bot.requests.get')
    def test_http_error_returns_none(self, mock_get):
        """fetch_article returns None on HTTP error."""
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception('HTTP Error')
        mock_get.return_value = mock_response

        result = fetch_article('http://example.com/article')
        self.assertIsNone(result)

    @patch('news_bot.requests.get')
    def test_general_exception_returns_none(self, mock_get):
        """fetch_article returns None on any exception."""
        mock_get.side_effect = Exception('Network error')
        result = fetch_article('http://example.com/article')
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()