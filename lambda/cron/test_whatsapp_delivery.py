"""Tests for WhatsApp delivery in the Cron Lambda (synthetic IDs only)."""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib  # noqa: E402

index = importlib.import_module("index")

SECRET = json.dumps({
    "accessToken": "EAAtest-access-token",
    "appSecret": "test-app-secret",
    "verifyToken": "test-verify-token",
    "phoneNumberId": "100000000000001",
})


class TestCronWhatsappDelivery(unittest.TestCase):
    def setUp(self):
        index.WHATSAPP_TOKEN_SECRET_ID = "openclaw/channels/whatsapp"

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_delivers_text_to_graph_api(self, _secret):
        with patch.object(index.whatsapp.urllib_request, "urlopen") as mock_open:
            index.deliver_response("whatsapp", "15550000001", "Your brief")
        req = mock_open.call_args.args[0]
        self.assertEqual(req.full_url, "https://graph.facebook.com/v23.0/100000000000001/messages")
        body = json.loads(req.data)
        self.assertEqual((body["to"], body["text"]["body"]), ("15550000001", "Your brief"))
        self.assertEqual(req.get_header("Authorization"), "Bearer EAAtest-access-token")

    @patch.object(index, "_get_secret", return_value="random-placeholder")
    def test_unconfigured_does_not_send(self, _secret):
        with patch.object(index.whatsapp.urllib_request, "urlopen") as mock_open:
            index.deliver_response("whatsapp", "15550000001", "Your brief")
        mock_open.assert_not_called()

    @patch.object(index, "_get_secret", return_value=SECRET)
    def test_bad_recipient_not_sent(self, _secret):
        with patch.object(index.whatsapp.urllib_request, "urlopen") as mock_open:
            index.deliver_response("whatsapp", "../../evil", "x")
        mock_open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
