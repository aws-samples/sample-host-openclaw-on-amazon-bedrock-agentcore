"""Regression: the Cron Lambda must not log the actor or the delivery target
(a phone number for a WhatsApp user). Covers verify_cron_event for a WhatsApp
actor and a Telegram actor.

All IDs below are synthetic placeholders.
"""

import importlib
import logging
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")

sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.config"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
index = importlib.import_module("index")

WA_PHONE = "15551234567"
WA_ACTOR = f"whatsapp:{WA_PHONE}"
TG_ID = "778899001"
TG_ACTOR = f"telegram:{TG_ID}"


class FakeClientError(Exception):
    pass


class FakeTable:
    def __init__(self, items):
        self.items = dict(items)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}


class CaptureLogs:
    def __enter__(self):
        self.messages = []
        self.handler = logging.Handler()
        self.handler.emit = lambda record: self.messages.append(record.getMessage())
        index.logger.addHandler(self.handler)
        self._level = index.logger.level
        index.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        index.logger.removeHandler(self.handler)
        index.logger.setLevel(self._level)

    @property
    def text(self):
        return "\n".join(self.messages)


def _table(actor, channel, target, user="user_x"):
    return FakeTable({
        (f"CHANNEL#{actor}", "PROFILE"): {"userId": user},
        (f"ACTOR#{actor}", "OWNER"): {"userId": user, "actorId": actor, "platformVersion": "V2",
                                      "ownerGeneration": 1, "runtimeActorId": actor},
        (f"USER#{user}", "PROFILE"): {},
        (f"USER#{user}", "CRON#s1"): {"channelActorId": actor, "actorId": actor, "channel": channel,
                                      "channelTarget": target, "message": "hi", "scheduleName": "S"},
    })


def _event(actor, channel, target, user="user_x", **over):
    event = {"userId": user, "channelActorId": actor, "actorId": actor, "channel": channel,
             "channelTarget": target, "message": "hi", "scheduleId": "s1", "scheduleName": "S"}
    event.update(over)
    return event


class TestCronLogPrivacy(unittest.TestCase):
    def setUp(self):
        for target, attr in (("authorize_channel_actor", MagicMock()),
                             ("ClientError", FakeClientError)):
            p = patch.object(index, target, attr)
            p.start()
            self.addCleanup(p.stop)

    def _verify(self, actor, channel, target, event=None):
        with patch.object(index, "identity_table", _table(actor, channel, target)), \
                patch.object(index, "get_or_create_session", lambda uid: SimpleNamespace(actor_id=actor)), \
                CaptureLogs() as cap:
            job, error = index.verify_cron_event(event or _event(actor, channel, target))
        return job, error, cap.text

    def test_verify_logs_channel_and_user_not_actor_or_target(self):
        for actor, channel, target in [(WA_ACTOR, "whatsapp", WA_PHONE), (TG_ACTOR, "telegram", TG_ID)]:
            job, error, text = self._verify(actor, channel, target)
            self.assertIsNone(error)
            self.assertIsNotNone(job)
            self.assertNotIn(actor, text)
            self.assertNotIn(target, text)
            self.assertIn("user=user_x", text)
            self.assertIn(f"channel={channel}", text)

    def test_missing_fields_log_has_no_actor_or_target(self):
        # A WhatsApp event missing its message still must not log the phone.
        event = _event(WA_ACTOR, "whatsapp", WA_PHONE, message=None)
        with patch.object(index, "identity_table", _table(WA_ACTOR, "whatsapp", WA_PHONE)), \
                CaptureLogs() as cap:
            job, error = index.verify_cron_event(event)
        self.assertIsNotNone(error)
        self.assertNotIn(WA_ACTOR, cap.text)
        self.assertNotIn(WA_PHONE, cap.text)


if __name__ == "__main__":
    unittest.main()
