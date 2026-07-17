"""Runtime identity boundary tests for the webhook router."""

import importlib
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
os.environ.setdefault("AGENTCORE_QUALIFIER", "test-endpoint")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("USER_FILES_BUCKET", "openclaw-user-files-test")

sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", MagicMock())
sys.modules.setdefault("botocore.exceptions", MagicMock())

index = importlib.import_module("index")


class TestCanonicalRuntimeIdentity(unittest.TestCase):
    def test_invoke_uses_internal_user_for_namespace_and_runtime_user(self):
        response = MagicMock()
        response.read.return_value = b'{"response":"ok"}'
        with patch.object(index, "agentcore_client") as client:
            client.invoke_agent_runtime.return_value = {
                "statusCode": 200,
                "response": response,
            }

            result = index.invoke_agent_runtime(
                "ses_123456789012345678901234567890",
                "user_internal_1",
                "telegram:attacker-controlled-actor",
                "telegram",
                "hello",
                f"po1_{'a' * 64}",
            )

        self.assertEqual(result, {"response": "ok"})
        call = client.invoke_agent_runtime.call_args.kwargs
        self.assertEqual(call["runtimeUserId"], "user_internal_1")
        payload = json.loads(call["payload"])
        self.assertEqual(payload["internalUserId"], "user_internal_1")
        self.assertEqual(payload["namespace"], "user_internal_1")
        self.assertEqual(payload["actorId"], "telegram:attacker-controlled-actor")
        self.assertNotIn("userId", payload)

    def test_two_linked_channel_actors_share_runtime_and_upload_namespace(self):
        linked_user = "user_linked_42"
        actors = ["telegram:101", "slack:U202", "feishu:ou_303"]
        for actor in actors:
            with self.subTest(actor=actor):
                response = MagicMock()
                response.read.return_value = b'{"response":"ok"}'
                with patch.object(index, "agentcore_client") as client:
                    client.invoke_agent_runtime.return_value = {
                        "statusCode": 200,
                        "response": response,
                    }
                    index.invoke_agent_runtime(
                        "ses_123456789012345678901234567890",
                        linked_user,
                        actor,
                        actor.split(":", 1)[0],
                        "hello",
                        f"po1_{'b' * 64}",
                    )
                self.assertEqual(
                    client.invoke_agent_runtime.call_args.kwargs["runtimeUserId"],
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
                "chat": {"id": 9},
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
                "chat": {"id": 9},
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
