"""Tests proving the inherited direct cron path is fail-closed."""

import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch


MODULE_PATH = Path(__file__).with_name("index.py")


def _load_cron(name="cron_index"):
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDirectCronDisabled(unittest.TestCase):
    def test_handler_rejects_before_identity_runtime_or_delivery_calls(self):
        cron = _load_cron()
        event = {
            "userId": "attacker-payload-user",
            "actorId": "telegram:attacker",
            "channel": "telegram",
            "channelTarget": "123",
            "message": "do something",
            "scheduleId": "legacy",
        }

        result = cron.handler(event, None)

        self.assertEqual(result["statusCode"], 410)
        self.assertEqual(result["code"], "DIRECT_CRON_DISABLED")

    def test_module_has_no_aws_clients_for_disabled_path(self):
        cron = _load_cron("cron_no_clients")
        self.assertFalse(hasattr(cron, "identity_table"))
        self.assertFalse(hasattr(cron, "agentcore_client"))
        self.assertFalse(hasattr(cron, "secrets_client"))

    def test_wrong_region_fails_before_module_initialization(self):
        with (
            patch.dict(
                os.environ,
                {"AWS_REGION": "ap-southeast-2", "AWS_DEFAULT_REGION": "ap-southeast-2"},
                clear=False,
            ),
            self.assertRaisesRegex(RuntimeError, "eu-west-1"),
        ):
            _load_cron("cron_wrong_region")


if __name__ == "__main__":
    unittest.main()
