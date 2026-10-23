"""Tests for Feishu encrypted-event decryption in the Router Lambda.

All keys and payloads below are synthetic placeholders.
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-123456789012-us-west-2")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

import importlib
index = importlib.import_module("index")

ENCRYPT_KEY = "test-encrypt-key"
IV = bytes(range(16))


def _encrypt(plaintext):
    key = hashlib.sha256(ENCRYPT_KEY.encode()).hexdigest()
    out = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-K", key, "-iv", IV.hex()],
                         input=plaintext, capture_output=True, check=True).stdout
    return {"encrypt": base64.b64encode(IV + out).decode()}


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI needed to build test ciphertext")
@patch.object(index, "_get_feishu_credentials", return_value=("app", "secret", "vtoken", ENCRYPT_KEY))
class TestDecryptFeishuEvent(unittest.TestCase):
    def test_round_trip(self, _creds):
        body = json.dumps({"header": {"event_type": "im.message.receive_v1"}})
        self.assertEqual(index._decrypt_feishu_event(_encrypt(body.encode())), body)

    def test_short_or_misaligned_ciphertext_is_rejected_before_libcrypto(self, _creds):
        with patch.object(index, "_aes_cbc_decrypt") as aes:
            for size in (0, 1, 15, 16, 17, 31, 33, 47):
                with self.subTest(size=size):
                    blob = {"encrypt": base64.b64encode(b"\x01" * size).decode()}
                    self.assertIsNone(index._decrypt_feishu_event(blob))
            aes.assert_not_called()

    def test_invalid_base64_returns_none(self, _creds):
        self.assertIsNone(index._decrypt_feishu_event({"encrypt": "not base64!"}))
        self.assertIsNone(index._decrypt_feishu_event({"encrypt": "QQ="}))

    def test_non_utf8_plaintext_returns_none(self, _creds):
        self.assertIsNone(index._decrypt_feishu_event(_encrypt(b"\xff\xfe\xfd")))

    def test_handler_ignores_decrypted_non_json(self, _creds):
        self.assertEqual(index.handle_feishu(json.dumps(_encrypt(b"not json"))),
                         {"statusCode": 200, "body": "ok"})


class TestAesKeyAndIvSizes(unittest.TestCase):
    def test_wrong_sizes_raise_before_any_native_call(self):
        for key, iv in ((b"k" * 31, IV), (b"k" * 32, b"i" * 15), (b"k" * 32, b"i" * 17)):
            with self.subTest(key=len(key), iv=len(iv)):
                with self.assertRaises(RuntimeError):
                    index._aes_cbc_decrypt(key, iv, b"\x00" * 16)


if __name__ == "__main__":
    unittest.main()
