"""Tests for image upload support in the Router Lambda."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Set required env vars before importing the module
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

# Mock boto3 before importing the module
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")


class TestRuntimeInvocationIdentity(unittest.TestCase):
    def test_is_stable_for_retry_and_domain_separated(self):
        first = index._build_runtime_invocation_id("telegram", 1001, "user-a")
        retry = index._build_runtime_invocation_id("telegram", 1001, "user-a")
        different_event = index._build_runtime_invocation_id("telegram", 1002, "user-a")
        different_user = index._build_runtime_invocation_id("telegram", 1001, "user-b")
        different_channel = index._build_runtime_invocation_id("slack", 1001, "user-a")

        self.assertEqual(first, retry)
        self.assertRegex(first, r"^po1_[0-9a-f]{64}$")
        self.assertEqual(
            len({first, different_event, different_user, different_channel}), 4
        )

    def test_rejects_missing_immutable_event_identity(self):
        with self.assertRaises(ValueError):
            index._build_runtime_invocation_id("telegram", None, "user-a")


class TestUploadImageToS3(unittest.TestCase):
    """Tests for _upload_image_to_s3."""

    def setUp(self):
        self.original_bucket = index.USER_FILES_BUCKET
        index.USER_FILES_BUCKET = "test-bucket"

    def tearDown(self):
        index.USER_FILES_BUCKET = self.original_bucket

    @patch.object(index, "s3_client")
    def test_valid_jpeg_upload(self, mock_s3):
        """Valid JPEG upload returns an S3 key."""
        image_bytes = b"\xff\xd8" + b"\x00" * 100
        invocation_id = index._build_runtime_invocation_id(
            "telegram", 1001, "user-a"
        )
        result = index._upload_image_to_s3(
            image_bytes, "telegram_123", "image/jpeg", invocation_id
        )

        self.assertIsNotNone(result)
        content_hash = index.hashlib.sha256(image_bytes).hexdigest()
        self.assertEqual(
            result,
            f"telegram_123/_uploads/img_{invocation_id}_{content_hash}.jpeg",
        )
        self.assertTrue(result.endswith(".jpeg"))
        mock_s3.put_object.assert_called_once()

    @patch.object(index, "s3_client")
    def test_retry_is_same_key_and_changed_event_or_image_is_different(self, mock_s3):
        image_bytes = b"\x89PNG" + b"\x00" * 100
        invocation_id = index._build_runtime_invocation_id(
            "slack", "Ev-1", "user-a"
        )
        changed_event_id = index._build_runtime_invocation_id(
            "slack", "Ev-2", "user-a"
        )

        first = index._upload_image_to_s3(
            image_bytes, "slack_U123", "image/png", invocation_id
        )
        retry = index._upload_image_to_s3(
            image_bytes, "slack_U123", "image/png", invocation_id
        )
        changed_event = index._upload_image_to_s3(
            image_bytes, "slack_U123", "image/png", changed_event_id
        )
        changed_image = index._upload_image_to_s3(
            image_bytes + b"changed", "slack_U123", "image/png", invocation_id
        )

        self.assertEqual(first, retry)
        self.assertEqual(len({first, changed_event, changed_image}), 3)
        self.assertEqual(
            mock_s3.put_object.call_args_list[0].kwargs["Key"],
            mock_s3.put_object.call_args_list[1].kwargs["Key"],
        )

    def test_invalid_content_type(self):
        """Non-image content type is rejected."""
        result = index._upload_image_to_s3(
            b"data", "ns", "application/pdf", f"po1_{'a' * 64}"
        )
        self.assertIsNone(result)

    def test_oversized_image(self):
        """Image exceeding 3.75 MB is rejected."""
        big = b"\x00" * (3_750_001)
        result = index._upload_image_to_s3(
            big, "ns", "image/jpeg", f"po1_{'a' * 64}"
        )
        self.assertIsNone(result)

    def test_no_bucket_configured(self):
        """Returns None when USER_FILES_BUCKET is empty."""
        index.USER_FILES_BUCKET = ""
        result = index._upload_image_to_s3(
            b"data", "ns", "image/jpeg", f"po1_{'a' * 64}"
        )
        self.assertIsNone(result)

    @patch.object(index, "s3_client")
    def test_s3_error(self, mock_s3):
        """Returns None when S3 put_object fails."""
        mock_s3.put_object.side_effect = Exception("S3 error")
        result = index._upload_image_to_s3(
            b"data", "ns", "image/jpeg", f"po1_{'a' * 64}"
        )
        self.assertIsNone(result)

    @patch.object(index, "s3_client")
    def test_invalid_invocation_identity_fails_before_upload(self, mock_s3):
        result = index._upload_image_to_s3(
            b"data", "ns", "image/jpeg", "attacker-controlled"
        )
        self.assertIsNone(result)
        mock_s3.put_object.assert_not_called()


class TestDownloadTelegramImage(unittest.TestCase):
    """Tests for _download_telegram_image."""

    @patch("index.urllib_request")
    def test_photo_array_downloads_largest(self, mock_urllib):
        """Downloads the last (largest) photo in the array."""
        message = {
            "photo": [
                {"file_id": "small", "width": 90},
                {"file_id": "medium", "width": 320},
                {"file_id": "large", "width": 1280},
            ]
        }

        # Mock getFile response
        get_file_resp = MagicMock()
        get_file_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"file_path": "photos/file_1.jpg", "file_size": 50000},
        }).encode()

        # Mock file download
        download_resp = MagicMock()
        download_resp.read.return_value = b"\xff\xd8" + b"\x00" * 100

        mock_urllib.urlopen.side_effect = [get_file_resp, download_resp]

        image_bytes, content_type, filename = index._download_telegram_image(message, "test-token")

        self.assertIsNotNone(image_bytes)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(filename, "file_1.jpg")

    @patch("index.urllib_request")
    def test_document_with_image_mime(self, mock_urllib):
        """Downloads a document that has an image MIME type."""
        message = {
            "document": {
                "file_id": "doc_image",
                "mime_type": "image/png",
                "file_name": "screenshot.png",
            }
        }

        get_file_resp = MagicMock()
        get_file_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"file_path": "documents/screenshot.png", "file_size": 30000},
        }).encode()

        download_resp = MagicMock()
        download_resp.read.return_value = b"\x89PNG" + b"\x00" * 100

        mock_urllib.urlopen.side_effect = [get_file_resp, download_resp]

        image_bytes, content_type, _ = index._download_telegram_image(message, "test-token")
        self.assertIsNotNone(image_bytes)
        self.assertEqual(content_type, "image/png")

    def test_no_image_in_message(self):
        """Returns None tuple when message has no photo or image document."""
        message = {"text": "Hello"}
        result = index._download_telegram_image(message, "test-token")
        self.assertEqual(result, (None, None, None))

    def test_document_non_image_mime(self):
        """Rejects document with non-image MIME type."""
        message = {"document": {"file_id": "doc", "mime_type": "application/pdf"}}
        result = index._download_telegram_image(message, "test-token")
        self.assertEqual(result, (None, None, None))

    @patch("index.urllib_request")
    def test_oversized_file_rejected(self, mock_urllib):
        """Rejects files that exceed the size limit."""
        message = {"photo": [{"file_id": "big"}]}

        get_file_resp = MagicMock()
        get_file_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"file_path": "photos/big.jpg", "file_size": 5_000_000},
        }).encode()
        mock_urllib.urlopen.return_value = get_file_resp

        result = index._download_telegram_image(message, "test-token")
        self.assertEqual(result, (None, None, None))


class TestDownloadSlackFile(unittest.TestCase):
    """Tests for _download_slack_file."""

    @patch("index.urllib_request")
    def test_valid_image_download(self, mock_urllib):
        """Downloads a valid image file from Slack."""
        file_info = {
            "mimetype": "image/jpeg",
            "size": 50000,
            "url_private_download": "https://files.slack.com/files-pri/T0/file.jpg",
            "name": "photo.jpg",
        }

        download_resp = MagicMock()
        download_resp.read.return_value = b"\xff\xd8" + b"\x00" * 100
        mock_urllib.urlopen.return_value = download_resp
        mock_urllib.Request = MagicMock(return_value=MagicMock())

        image_bytes, content_type, filename = index._download_slack_file(file_info, "xoxb-token")

        self.assertIsNotNone(image_bytes)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(filename, "photo.jpg")

    def test_non_image_rejected(self):
        """Rejects non-image MIME types."""
        file_info = {"mimetype": "text/plain", "size": 100}
        result = index._download_slack_file(file_info, "xoxb-token")
        self.assertEqual(result, (None, None, None))

    def test_oversized_file_rejected(self):
        """Rejects files exceeding size limit."""
        file_info = {"mimetype": "image/png", "size": 5_000_000}
        result = index._download_slack_file(file_info, "xoxb-token")
        self.assertEqual(result, (None, None, None))

    def test_no_download_url(self):
        """Returns None when no download URL is available."""
        file_info = {"mimetype": "image/jpeg", "size": 100}
        result = index._download_slack_file(file_info, "xoxb-token")
        self.assertEqual(result, (None, None, None))


class TestBuildStructuredMessage(unittest.TestCase):
    """Tests for _build_structured_message."""

    def test_with_text_and_image(self):
        """Builds message with both text and image reference."""
        result = index._build_structured_message("What's this?", "ns/_uploads/img.jpeg", "image/jpeg")
        self.assertEqual(result["text"], "What's this?")
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["images"][0]["s3Key"], "ns/_uploads/img.jpeg")
        self.assertEqual(result["images"][0]["contentType"], "image/jpeg")

    def test_without_text(self):
        """Builds message with empty text when caption is missing."""
        result = index._build_structured_message("", "ns/_uploads/img.png", "image/png")
        self.assertEqual(result["text"], "")
        self.assertEqual(len(result["images"]), 1)


class TestHandleTelegramWithImages(unittest.TestCase):
    """Integration tests for handle_telegram with image support."""

    @patch.object(index, "invoke_agent_runtime", return_value={"response": "I see a cat!"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012345678")
    @patch.object(index, "resolve_user", return_value=("user_test123", False))
    @patch.object(index, "send_telegram_typing")
    @patch.object(index, "send_telegram_message")
    @patch.object(index, "_upload_image_to_s3", return_value="telegram_123/_uploads/img_test.jpeg")
    @patch.object(index, "_download_telegram_image", return_value=(b"image_data", "image/jpeg", "photo.jpg"))
    @patch.object(index, "_get_telegram_token", return_value="test-token")
    def test_photo_message(self, mock_token, mock_download, mock_upload,
                           mock_send, mock_typing, mock_resolve, mock_session, mock_invoke):
        """Photo message triggers image download, upload, and structured message."""
        body = json.dumps({
            "update_id": 1001,
            "message": {
                "chat": {"id": 123},
                "from": {"id": 456, "first_name": "Test"},
                "photo": [{"file_id": "abc", "width": 1280}],
                "caption": "What's this?",
            }
        })

        index.handle_telegram(body)

        mock_download.assert_called_once()
        mock_upload.assert_called_once()
        expected_invocation_id = index._build_runtime_invocation_id(
            "telegram", 1001, "user_test123"
        )
        self.assertEqual(mock_upload.call_args[0][3], expected_invocation_id)
        # invoke_agent_runtime should receive structured message
        call_args = mock_invoke.call_args
        msg = call_args[0][4]  # 5th positional arg is message
        self.assertIsInstance(msg, dict)
        self.assertEqual(msg["text"], "What's this?")
        self.assertEqual(len(msg["images"]), 1)
        self.assertEqual(
            call_args[0][5],
            expected_invocation_id,
        )

    @patch.object(index, "invoke_agent_runtime", return_value={"response": "Hello!"})
    @patch.object(index, "get_or_create_session", return_value="ses_test_123456789012345678")
    @patch.object(index, "resolve_user", return_value=("user_test123", False))
    @patch.object(index, "send_telegram_typing")
    @patch.object(index, "send_telegram_message")
    @patch.object(index, "_get_telegram_token", return_value="test-token")
    def test_text_only_backward_compat(self, mock_token, mock_send, mock_typing,
                                        mock_resolve, mock_session, mock_invoke):
        """Text-only messages still work as plain strings."""
        body = json.dumps({
            "update_id": 1002,
            "message": {
                "chat": {"id": 123},
                "from": {"id": 456, "first_name": "Test"},
                "text": "Hello",
            }
        })

        index.handle_telegram(body)

        call_args = mock_invoke.call_args
        msg = call_args[0][4]
        self.assertEqual(msg, "Hello")


if __name__ == "__main__":
    unittest.main()
