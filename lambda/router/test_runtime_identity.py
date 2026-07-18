"""Runtime identity boundary tests for the webhook router."""

import importlib
import hashlib
import json
import os
import runpy
import sys
import unittest
from unittest.mock import MagicMock, patch


os.environ.setdefault(
    "AGENTCORE_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/test",
)
os.environ.setdefault("AGENTCORE_QUALIFIER", "release_" + "a" * 40)
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-test")

sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", MagicMock())
sys.modules.setdefault("botocore.exceptions", MagicMock())

index = importlib.import_module("index")


class TestCanonicalRuntimeIdentity(unittest.TestCase):
    def test_registration_allowlist_is_read_strongly_before_any_creation(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", False),
        ):
            self.assertFalse(index.is_user_allowed("telegram", "42"))

        table.get_item.assert_called_once_with(
            Key={"PK": "ALLOW#telegram:42", "SK": "ALLOW"},
            ConsistentRead=True,
        )
        table.put_item.assert_not_called()

    def test_new_identity_registration_is_one_fenced_transaction(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", False),
        ):
            user_id, is_new = index.resolve_user("telegram", "42", "Ada")

        self.assertTrue(is_new)
        self.assertTrue(user_id.startswith("user_"))
        table.put_item.assert_not_called()
        transaction = table.meta.client.transact_write_items.call_args.kwargs[
            "TransactItems"
        ]
        self.assertEqual(len(transaction), 6)
        self.assertEqual(
            transaction[0],
            {
                "ConditionCheck": {
                    "TableName": "openclaw-identity",
                    "Key": {
                        "PK": {"S": "ALLOW#telegram:42"},
                        "SK": {"S": "ALLOW"},
                    },
                    "ConditionExpression": "attribute_exists(PK) AND attribute_exists(SK)",
                }
            },
        )
        tombstone_digest = hashlib.sha256(b"telegram:42").hexdigest()
        self.assertEqual(
            transaction[1],
            {
                "ConditionCheck": {
                    "TableName": "openclaw-identity",
                    "Key": {
                        "PK": {"S": f"CHANNEL_TOMBSTONE#{tombstone_digest}"},
                        "SK": {"S": "TOMBSTONE"},
                    },
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
        )
        user_id = "user_" + hashlib.sha256(b"telegram:42").hexdigest()[:16]
        self.assertEqual(
            transaction[2]["ConditionCheck"]["Key"],
            {
                "PK": {
                    "S": "USER_TOMBSTONE#"
                    + hashlib.sha256(user_id.encode()).hexdigest()
                },
                "SK": {"S": "TOMBSTONE"},
            },
        )
        records = [entry["Put"]["Item"] for entry in transaction[3:]]
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["userId"]["S"] == user_id for record in records))
        self.assertNotIn("telegram:42", json.dumps(transaction[1]))

    def test_open_registration_still_checks_permanent_channel_tombstone(self):
        table = MagicMock()
        table.get_item.return_value = {}
        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", True),
        ):
            user_id, is_new = index.resolve_user("telegram", "42", "Ada")

        self.assertTrue(is_new)
        self.assertTrue(user_id.startswith("user_"))
        transaction = table.meta.client.transact_write_items.call_args.kwargs[
            "TransactItems"
        ]
        self.assertEqual(len(transaction), 5)
        self.assertEqual(set(transaction[0]), {"ConditionCheck"})
        self.assertIn("CHANNEL_TOMBSTONE#", json.dumps(transaction[0]))
        self.assertIn("USER_TOMBSTONE#", json.dumps(transaction[1]))
        self.assertNotIn("ALLOW#", json.dumps(transaction))

    def test_failed_registration_transaction_returns_only_strongly_read_mapping(self):
        table = MagicMock()
        table.get_item.side_effect = [
            {},
            {
                "Item": {
                    "PK": "CHANNEL#telegram:42",
                    "SK": "PROFILE",
                    "userId": "user_winning_writer",
                    "channel": "telegram",
                    "channelUserId": "42",
                }
            },
            {},
        ]
        table.meta.client.transact_write_items.side_effect = RuntimeError("race")

        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", False),
        ):
            self.assertEqual(
                index.resolve_user("telegram", "42", "Ada"),
                ("user_winning_writer", False),
            )

        self.assertEqual(table.get_item.call_count, 3)
        for call in table.get_item.call_args_list:
            self.assertTrue(call.kwargs["ConsistentRead"])

    def test_existing_channel_mapping_to_deleted_user_is_never_returned(self):
        mapping = {
            "Item": {
                "PK": "CHANNEL#telegram:42",
                "SK": "PROFILE",
                "userId": "user_deleted_42",
                "channel": "telegram",
                "channelUserId": "42",
            }
        }
        user_tombstone = {
            "Item": {
                "PK": "USER_TOMBSTONE#"
                + hashlib.sha256(b"user_deleted_42").hexdigest(),
                "SK": "TOMBSTONE",
                "markerVersion": "1",
            }
        }
        table = MagicMock()
        table.get_item.side_effect = [mapping, user_tombstone, mapping, user_tombstone]
        table.meta.client.transact_write_items.side_effect = RuntimeError(
            "user deletion fence"
        )

        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", False),
        ):
            self.assertEqual(index.resolve_user("telegram", "42", "Ada"), (None, False))

    def test_failed_registration_transaction_without_exact_mapping_denies(self):
        table = MagicMock()
        table.get_item.side_effect = [
            {},
            {
                "Item": {
                    "PK": "CHANNEL#telegram:42",
                    "SK": "PROFILE",
                    "userId": "user_other",
                    "channel": "telegram",
                    "channelUserId": "99",
                }
            },
        ]
        table.meta.client.transact_write_items.side_effect = RuntimeError("denied")

        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", False),
        ):
            self.assertEqual(index.resolve_user("telegram", "42", "Ada"), (None, False))

    def test_registration_ordered_after_channel_deletion_cannot_recreate_account(self):
        table = MagicMock()
        table.get_item.return_value = {}
        expected_tombstone = (
            "CHANNEL_TOMBSTONE#" + hashlib.sha256(b"telegram:42").hexdigest()
        )

        def deletion_fence_wins(**kwargs):
            checks = [
                operation["ConditionCheck"]
                for operation in kwargs["TransactItems"]
                if "ConditionCheck" in operation
            ]
            self.assertTrue(
                any(
                    check["Key"]["PK"] == {"S": expected_tombstone}
                    for check in checks
                )
            )
            raise RuntimeError("deletion tombstone already exists")

        table.meta.client.transact_write_items.side_effect = deletion_fence_wins
        with (
            patch.object(index, "identity_table", table),
            patch.object(index, "REGISTRATION_OPEN", False),
        ):
            self.assertEqual(index.resolve_user("telegram", "42", "Ada"), (None, False))

        table.put_item.assert_not_called()

    def test_redeemed_identity_backref_is_user_indexed_for_exact_deletion(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {
                "PK": "BIND#ABC12345",
                "SK": "BIND",
                "userId": "user_linked_42",
                "ttl": 4_000_000_000,
            }
        }
        with patch.object(index, "identity_table", table):
            user_id, success = index.redeem_bind_code(
                "ABC12345", "telegram", "77", "Grace"
            )

        self.assertTrue(success)
        self.assertEqual(user_id, "user_linked_42")
        table.get_item.assert_called_once_with(
            Key={"PK": "BIND#ABC12345", "SK": "BIND"},
            ConsistentRead=True,
        )
        transaction = table.meta.client.transact_write_items.call_args.kwargs[
            "TransactItems"
        ]
        self.assertEqual(len(transaction), 5)
        records = [entry["Put"]["Item"] for entry in transaction if "Put" in entry]
        self.assertEqual(len(records), 2)
        self.assertTrue(all(record["userId"]["S"] == user_id for record in records))
        self.assertIn("USER_TOMBSTONE#", json.dumps(transaction))
        self.assertIn("CHANNEL_TOMBSTONE#", json.dumps(transaction))
        self.assertNotIn("telegram:77", json.dumps(transaction[1:3]))

    def test_bind_redemption_cannot_write_after_user_deletion_fence_wins(self):
        table = MagicMock()
        table.get_item.return_value = {
            "Item": {
                "PK": "BIND#ABC12345",
                "SK": "BIND",
                "userId": "user_linked_42",
                "ttl": 4_000_000_000,
            }
        }
        table.meta.client.transact_write_items.side_effect = RuntimeError(
            "user tombstone condition"
        )

        with patch.object(index, "identity_table", table):
            self.assertEqual(
                index.redeem_bind_code("ABC12345", "telegram", "77", "Grace"),
                (None, False),
            )

        table.put_item.assert_not_called()
        table.delete_item.assert_not_called()

    def test_bind_code_creation_is_fenced_by_target_user_tombstone(self):
        table = MagicMock()
        with patch.object(index, "identity_table", table):
            code = index.create_bind_code("user_linked_42")

        self.assertRegex(code, r"^[A-F0-9]{8}$")
        transaction = table.meta.client.transact_write_items.call_args.kwargs[
            "TransactItems"
        ]
        self.assertEqual(len(transaction), 2)
        self.assertIn("USER_TOMBSTONE#", json.dumps(transaction[0]))
        self.assertNotIn("user_linked_42", json.dumps(transaction[0]))
        self.assertEqual(
            transaction[1]["Put"]["Item"]["userId"],
            {"S": "user_linked_42"},
        )

    def test_invoke_uses_internal_user_for_namespace_and_runtime_user(self):
        driver = MagicMock()
        driver.ensure.return_value.session_id = "ses_123456789012345678901234567890"
        driver.invoke.return_value = {"response": "ok"}
        with patch.object(index, "_get_runtime_driver", return_value=driver):
            result = index.invoke_agent_runtime(
                "ses_123456789012345678901234567890",
                "user_internal_1",
                "telegram:attacker-controlled-actor",
                "telegram",
                "hello",
                f"po1_{'a' * 64}",
            )

        self.assertEqual(result, {"response": "ok"})
        driver.invoke.assert_called_once_with(
            "user_internal_1",
            {
                "actorId": "telegram:attacker-controlled-actor",
                "channel": "telegram",
                "message": "hello",
            },
            f"po1_{'a' * 64}",
        )

    def test_legacy_router_wrapper_cannot_select_another_session(self):
        driver = MagicMock()
        driver.ensure.return_value.session_id = "ses_123456789012345678901234567890"
        with (
            patch.object(index, "_get_runtime_driver", return_value=driver),
            self.assertRaisesRegex(RuntimeError, "session mapping"),
        ):
            index.invoke_agent_runtime(
                "ses_attacker_controlled_12345678901234567890",
                "user_internal_1",
                "telegram:1",
                "telegram",
                "hello",
                f"po1_{'f' * 64}",
            )
        driver.invoke.assert_not_called()

    def test_two_linked_channel_actors_share_runtime_and_upload_namespace(self):
        linked_user = "user_linked_42"
        actors = ["telegram:101", "slack:U202", "feishu:ou_303"]
        for actor in actors:
            with self.subTest(actor=actor):
                driver = MagicMock()
                driver.ensure.return_value.session_id = "ses_123456789012345678901234567890"
                driver.invoke.return_value = {"response": "ok"}
                with patch.object(index, "_get_runtime_driver", return_value=driver):
                    index.invoke_agent_runtime(
                        "ses_123456789012345678901234567890",
                        linked_user,
                        actor,
                        actor.split(":", 1)[0],
                        "hello",
                        f"po1_{'b' * 64}",
                    )
                self.assertEqual(
                    driver.invoke.call_args.args[0],
                    linked_user,
                )

        with patch.object(index, "s3_client") as s3:
            first = index._upload_image_to_s3(
                b"image", linked_user, "image/png", f"po1_{'c' * 64}"
            )
            second = index._upload_image_to_s3(
                b"image", linked_user, "image/png", f"po1_{'c' * 64}"
            )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(f"{linked_user}/_uploads/"))
        self.assertEqual(s3.put_object.call_count, 2)

    def test_second_user_has_disjoint_runtime_and_upload_prefix(self):
        with patch.object(index, "s3_client"):
            first = index._upload_image_to_s3(
                b"same", "user_one", "image/jpeg", f"po1_{'d' * 64}"
            )
            second = index._upload_image_to_s3(
                b"same", "user_two", "image/jpeg", f"po1_{'d' * 64}"
            )

        self.assertTrue(first.startswith("user_one/_uploads/"))
        self.assertTrue(second.startswith("user_two/_uploads/"))
        self.assertNotEqual(first, second)

    def test_handler_passes_resolved_user_as_image_namespace(self):
        body = {
            "update_id": 77,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "first_name": "Linked"},
                "photo": [{"file_id": "photo"}],
            },
        }
        with (
            patch.object(index, "resolve_user", return_value=("user_resolved", False)),
            patch.object(index, "_get_telegram_token", return_value="token"),
            patch.object(index, "_download_telegram_image", return_value=(b"img", "image/jpeg", "x")),
            patch.object(index, "_upload_image_to_s3", return_value="user_resolved/_uploads/x") as upload,
            patch.object(index, "get_or_create_session", return_value="ses_123456789012345678901234567890"),
            patch.object(index, "invoke_agent_runtime", return_value={"response": "ok"}),
            patch.object(index, "send_telegram_typing"),
            patch.object(index, "send_telegram_message"),
        ):
            index.handle_telegram(body)

        self.assertEqual(upload.call_args.args[1], "user_resolved")

    def test_corrupt_resolved_identity_fails_before_session_s3_or_runtime(self):
        body = {
            "update_id": 78,
            "message": {
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "first_name": "Linked"},
                "photo": [{"file_id": "photo"}],
            },
        }
        with (
            patch.object(index, "resolve_user", return_value=("../actor-derived", False)),
            patch.object(index, "_get_telegram_token", return_value="token"),
            patch.object(index, "_download_telegram_image") as download,
            patch.object(index, "_upload_image_to_s3") as upload,
            patch.object(index, "get_or_create_session") as session,
            patch.object(index, "invoke_agent_runtime") as invoke,
            patch.object(index, "send_telegram_message"),
        ):
            index.handle_telegram(body)

        download.assert_not_called()
        upload.assert_not_called()
        session.assert_not_called()
        invoke.assert_not_called()

    def test_legacy_async_telegram_path_rejects_non_private_or_cross_actor_chat(self):
        invalid_messages = [
            {
                "chat": {"id": 42, "type": "group"},
                "from": {"id": 42},
                "text": "hello",
            },
            {
                "chat": {"id": 42, "type": "supergroup"},
                "from": {"id": 42},
                "text": "hello",
            },
            {
                "chat": {"id": 42, "type": "channel"},
                "from": {"id": 42},
                "text": "hello",
            },
            {
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 43},
                "text": "hello",
            },
            {
                "chat": {"id": 0, "type": "private"},
                "from": {"id": 0},
                "text": "hello",
            },
            {
                "chat": {"id": -42, "type": "private"},
                "from": {"id": -42},
                "text": "hello",
            },
            {
                "chat": {"id": 10**20, "type": "private"},
                "from": {"id": 10**20},
                "text": "hello",
            },
        ]
        for offset, message in enumerate(invalid_messages):
            with self.subTest(message=message):
                with (
                    patch.object(index, "resolve_user") as resolve,
                    patch.object(index, "_get_telegram_token") as token,
                    patch.object(index, "send_telegram_message") as send,
                ):
                    index.handle_telegram(
                        {"update_id": 900 + offset, "message": message}
                    )
                resolve.assert_not_called()
                token.assert_not_called()
                send.assert_not_called()


class TestRegionBoundary(unittest.TestCase):
    def test_wrong_region_fails_before_any_boto_client_or_resource_call(self):
        fake_boto = MagicMock()
        fake_boto.client.side_effect = AssertionError("AWS client must not be created")
        fake_boto.resource.side_effect = AssertionError("AWS resource must not be created")
        module_path = os.path.join(os.path.dirname(__file__), "index.py")

        with (
            patch.dict(
                os.environ,
                {
                    "AWS_REGION": "us-west-2",
                    "AWS_DEFAULT_REGION": "us-west-2",
                    "AGENTCORE_RUNTIME_ARN": "arn:test",
                    "AGENTCORE_QUALIFIER": "test",
                    "IDENTITY_TABLE_NAME": "test",
                },
                clear=False,
            ),
            patch.dict(sys.modules, {"boto3": fake_boto}),
            self.assertRaisesRegex(RuntimeError, "eu-west-1"),
        ):
            runpy.run_path(module_path, run_name="router_wrong_region_probe")

        fake_boto.client.assert_not_called()
        fake_boto.resource.assert_not_called()


if __name__ == "__main__":
    unittest.main()
