"""
Tests for _is_currently_authorized: a linked secondary channel is authorized
only via the owning user's primary (runtime) actor read from the
agent-unwritable ACTOR#<actor>/OWNER record, never from the agent-writable
USER#<id>/PROFILE.enrollmentActorId.

All IDs below are synthetic placeholders.
"""

import os
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


class FakeClientError(Exception):
    pass


class FakeTable:
    """DynamoDB table fake keyed by (PK, SK)."""

    def __init__(self, items):
        self.items = dict(items)

    def get_item(self, Key, ConsistentRead=False):  # noqa: N803 (AWS kwarg name)
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}


def allow(channel_key):
    return ((f"ALLOW#{channel_key}", "ALLOW"), {"PK": f"ALLOW#{channel_key}", "SK": "ALLOW"})


def owner(actor, user_id, runtime_actor):
    return ((f"ACTOR#{actor}", "OWNER"), {
        "PK": f"ACTOR#{actor}", "SK": "OWNER", "userId": user_id, "actorId": actor,
        "platformVersion": "V2", "ownerGeneration": 1, "runtimeActorId": runtime_actor,
    })


def profile(user_id, **attrs):
    item = {"PK": f"USER#{user_id}", "SK": "PROFILE"}
    item.update(attrs)
    return ((f"USER#{user_id}", "PROFILE"), item)


class TestIsCurrentlyAuthorized(unittest.TestCase):
    def setUp(self):
        self._patches = [
            patch.object(index, "REGISTRATION_OPEN", False),
            patch.object(index, "ClientError", FakeClientError),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def run_with(self, items, channel, channel_user_id, user_id):
        with patch.object(index, "identity_table", FakeTable(dict(items))):
            return index._is_currently_authorized(channel, channel_user_id, user_id)

    def test_own_allow_record_authorizes(self):
        self.assertTrue(self.run_with([allow("telegram:u1")], "telegram", "u1", "user_u1"))

    def test_legitimate_linked_channel_stays_authorized(self):
        # discord:u1 is a linked secondary of user_u1 whose primary telegram:u1
        # is still allowlisted; discord:u1 has no ALLOW# of its own.
        items = [owner("discord:u1", "user_u1", "telegram:u1"), allow("telegram:u1")]
        self.assertTrue(self.run_with(items, "discord", "u1", "user_u1"))

    def test_tampered_enrollment_actor_does_not_keep_revoked_linked_channel_authorized(self):
        # user_att's own actors are both revoked (no ALLOW#). The agent wrote
        # its own PROFILE.enrollmentActorId to point at telegram:victim, which
        # IS allowlisted. The owner record (agent-unwritable) still names the
        # attacker's own primary telegram:att, so authorization must be False.
        items = [
            owner("discord:att", "user_att", "telegram:att"),
            profile("user_att", enrollmentActorId="telegram:victim", runtimeEnrollment="V2"),
            allow("telegram:victim"),
        ]
        self.assertFalse(self.run_with(items, "discord", "att", "user_att"))

    def test_missing_owner_record_fails_closed(self):
        self.assertFalse(self.run_with([], "discord", "x", "user_x"))

    def test_owner_owned_by_a_different_user_is_rejected(self):
        # An owner record naming a different user must not authorize this user,
        # even if that other user's primary is allowlisted.
        items = [owner("discord:z", "user_other", "telegram:other"), allow("telegram:other")]
        self.assertFalse(self.run_with(items, "discord", "z", "user_z"))

    def test_registration_open_authorizes_everything(self):
        with patch.object(index, "REGISTRATION_OPEN", True):
            self.assertTrue(self.run_with([], "discord", "anyone", "user_any"))


if __name__ == "__main__":
    unittest.main()
