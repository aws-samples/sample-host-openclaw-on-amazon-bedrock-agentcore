"""Unit tests for screenshot marker detection and delivery helpers.

Tests cover:
- _extract_screenshots: marker detection and text cleanup
- _fetch_s3_image: S3 image retrieval with error handling
- _send_telegram_photo: multipart photo upload to Telegram
- _send_slack_file: v2 file upload to Slack
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# Add lambda/router to path so we can import index
sys.path.insert(0, os.path.dirname(__file__))

# Stub out boto3 and botocore before importing index
_mock_boto3 = MagicMock()
_mock_botocore_config = MagicMock()
_mock_botocore_exceptions = MagicMock()
sys.modules.setdefault("boto3", _mock_boto3)
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", _mock_botocore_config)
sys.modules.setdefault("botocore.exceptions", _mock_botocore_exceptions)

# Set required env vars before importing index
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock:us-west-2:123456789012:agentcore/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-qualifier")
os.environ.setdefault("IDENTITY_TABLE_NAME", "test-identity")
os.environ.setdefault("USER_FILES_BUCKET", "test-bucket")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("S3_USER_FILES_BUCKET", "test-bucket")

from index import (
    _extract_screenshots,
    _fetch_s3_image,
    _send_telegram_photo,
    _send_slack_file,
    SCREENSHOT_MARKER_RE,
)


class TestExtractScreenshots(unittest.TestCase):
    """Tests for _extract_screenshots() marker detection."""

    def test_no_markers(self):
        text, keys = _extract_screenshots("Hello world")
        self.assertEqual(text, "Hello world")
        self.assertEqual(keys, [])

    def test_single_marker(self):
        text, keys = _extract_screenshots(
            "Here is the page [SCREENSHOT:ns/_screenshots/shot.png] done"
        )
        self.assertNotIn("[SCREENSHOT:", text)
        self.assertEqual(keys, ["ns/_screenshots/shot.png"])
        self.assertEqual(text, "Here is the page  done")

    def test_multiple_markers(self):
        text, keys = _extract_screenshots(
            "[SCREENSHOT:key1.png] middle [SCREENSHOT:key2.png]"
        )
        self.assertEqual(len(keys), 2)
        self.assertIn("key1.png", keys)
        self.assertIn("key2.png", keys)
        self.assertNotIn("[SCREENSHOT:", text)

    def test_text_empty_after_strip(self):
        text, keys = _extract_screenshots("[SCREENSHOT:key.png]")
        self.assertEqual(text, "")
        self.assertEqual(keys, ["key.png"])

    def test_marker_with_slashes_and_dots(self):
        text, keys = _extract_screenshots(
            "Check [SCREENSHOT:telegram_123/_screenshots/page_2026-03-10_abc123.png]"
        )
        self.assertEqual(keys, ["telegram_123/_screenshots/page_2026-03-10_abc123.png"])

    def test_empty_key_not_matched(self):
        """Empty brackets [SCREENSHOT:] should not match (requires 1+ chars)."""
        text, keys = _extract_screenshots("[SCREENSHOT:]")
        self.assertEqual(keys, [])
        self.assertEqual(text, "[SCREENSHOT:]")

    def test_whitespace_cleanup(self):
        """Extra whitespace left by marker removal should be cleaned."""
        text, keys = _extract_screenshots("before  [SCREENSHOT:k.png]  after")
        self.assertNotIn("[SCREENSHOT:", text)
        # At minimum, no leading/trailing whitespace
        self.assertEqual(text, text.strip())

    def test_marker_regex_pattern(self):
        """Regex requires at least one char inside brackets."""
        self.assertTrue(SCREENSHOT_MARKER_RE.search("[SCREENSHOT:a]"))
        self.assertIsNone(SCREENSHOT_MARKER_RE.search("[SCREENSHOT:]"))


class TestFetchS3Image(unittest.TestCase):
    """Tests for _fetch_s3_image() S3 retrieval with namespace validation."""

    @patch("index.s3_client")
    def test_returns_bytes_on_success(self, mock_s3):
        body_mock = MagicMock()
        body_mock.read.return_value = b"fake-png-data"
        mock_s3.get_object.return_value = {"Body": body_mock}
        with patch.dict(os.environ, {"S3_USER_FILES_BUCKET": "test-bucket"}):
            result = _fetch_s3_image("ns/_screenshots/screenshot.png", namespace="ns")
        self.assertEqual(result, b"fake-png-data")
        mock_s3.get_object.assert_called_once_with(Bucket="test-bucket", Key="ns/_screenshots/screenshot.png")

    @patch("index.s3_client")
    def test_returns_none_on_s3_error(self, mock_s3):
        mock_s3.get_object.side_effect = Exception("NoSuchKey")
        with patch.dict(os.environ, {"S3_USER_FILES_BUCKET": "test-bucket"}):
            result = _fetch_s3_image("ns/_screenshots/missing.png", namespace="ns")
        self.assertIsNone(result)

    @patch("index.s3_client")
    def test_uses_s3_user_files_bucket_env(self, mock_s3):
        body_mock = MagicMock()
        body_mock.read.return_value = b"data"
        mock_s3.get_object.return_value = {"Body": body_mock}
        with patch.dict(os.environ, {"S3_USER_FILES_BUCKET": "custom-bucket"}):
            _fetch_s3_image("ns/_screenshots/key.png", namespace="ns")
        mock_s3.get_object.assert_called_once_with(Bucket="custom-bucket", Key="ns/_screenshots/key.png")

    def test_rejects_path_traversal(self):
        result = _fetch_s3_image("../../etc/passwd", namespace="telegram_123456")
        self.assertIsNone(result)

    def test_rejects_cross_namespace(self):
        result = _fetch_s3_image("other_user/_screenshots/shot.png", namespace="telegram_123456")
        self.assertIsNone(result)

    @patch("index.s3_client")
    def test_accepts_valid_namespace_key(self, mock_s3):
        mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"data")}
        with patch.dict(os.environ, {"S3_USER_FILES_BUCKET": "bucket"}):
            result = _fetch_s3_image("telegram_123456/_screenshots/screenshot_123.png", namespace="telegram_123456")
        self.assertEqual(result, b"data")


class TestSendTelegramPhoto(unittest.TestCase):
    """Tests for _send_telegram_photo() multipart upload."""

    @patch("urllib.request.urlopen")
    def test_success_returns_true(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        result = _send_telegram_photo("12345", b"png-bytes", None, "bot-token")
        self.assertTrue(result)
        # Verify the URL contains sendPhoto
        req_obj = mock_urlopen.call_args[0][0]
        self.assertIn("sendPhoto", req_obj.full_url)
        self.assertIn("bot-token", req_obj.full_url)

    @patch("urllib.request.urlopen")
    def test_failure_returns_false(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Network error")
        result = _send_telegram_photo("12345", b"png-bytes", None, "bot-token")
        self.assertFalse(result)

    @patch("urllib.request.urlopen")
    def test_includes_chat_id_in_body(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        _send_telegram_photo("99999", b"img", None, "tok")
        req_obj = mock_urlopen.call_args[0][0]
        self.assertIn(b"99999", req_obj.data)

    @patch("urllib.request.urlopen")
    def test_includes_caption_when_provided(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        _send_telegram_photo("123", b"img", "my caption", "tok")
        req_obj = mock_urlopen.call_args[0][0]
        self.assertIn(b"my caption", req_obj.data)

    @patch("urllib.request.urlopen")
    def test_no_caption_field_when_none(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        _send_telegram_photo("123", b"img", None, "tok")
        req_obj = mock_urlopen.call_args[0][0]
        # Should not contain "caption" form field
        self.assertNotIn(b'name="caption"', req_obj.data)


class TestSendSlackFile(unittest.TestCase):
    """Tests for _send_slack_file() v2 file upload.

    Requires files:write Slack bot scope for screenshot delivery.
    """

    @patch("urllib.request.urlopen")
    def test_success_returns_true(self, mock_urlopen):
        # Mock three sequential calls: getUploadURLExternal, upload, completeUploadExternal
        mock_urlopen.side_effect = [
            MagicMock(read=lambda: json.dumps({
                "ok": True,
                "upload_url": "https://files.slack.com/upload/v1/abc",
                "file_id": "F123",
            }).encode()),
            MagicMock(),  # upload bytes
            MagicMock(read=lambda: json.dumps({"ok": True}).encode()),  # complete
        ]
        result = _send_slack_file("C12345", b"png-bytes", "xoxb-token")
        self.assertTrue(result)
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("urllib.request.urlopen")
    def test_returns_false_on_get_url_failure(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock(
            read=lambda: json.dumps({"ok": False, "error": "not_authed"}).encode()
        )
        result = _send_slack_file("C12345", b"png-bytes", "xoxb-token")
        self.assertFalse(result)

    @patch("urllib.request.urlopen")
    def test_returns_false_on_network_error(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Connection refused")
        result = _send_slack_file("C12345", b"png-bytes", "xoxb-token")
        self.assertFalse(result)

    @patch("urllib.request.urlopen")
    def test_returns_false_on_complete_failure(self, mock_urlopen):
        mock_urlopen.side_effect = [
            MagicMock(read=lambda: json.dumps({
                "ok": True,
                "upload_url": "https://files.slack.com/upload/v1/abc",
                "file_id": "F123",
            }).encode()),
            MagicMock(),  # upload bytes
            MagicMock(read=lambda: json.dumps({"ok": False, "error": "channel_not_found"}).encode()),
        ]
        result = _send_slack_file("C12345", b"png-bytes", "xoxb-token")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
