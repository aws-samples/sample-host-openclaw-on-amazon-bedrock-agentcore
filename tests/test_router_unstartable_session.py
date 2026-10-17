"""Router chat path: a runtime session AgentCore can no longer start is replaced (same as the cron Lambda, #183).

16 Oct 2026 (Sydney): after a user's session storage started returning
EREMOTEIO, every start of that runtime session failed with AgentCore's
"Runtime initialization time exceeded". The cron Lambda now moves the user to
a new session (#183); a chat through the router on the same session still got
"Sorry, I'm having trouble right now" on every message. All IDs are placeholders.
"""

import importlib.util
import io
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "lambda" / "router" / "index.py"
for extra in (ROOT / "lambda" / "shared" / "python", ROOT / "lambda" / "router"):
    if extra.is_dir() and str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

USER = "user_abc123"
OLD = "ses_" + "s" * 40


def _load_router():
    spec = importlib.util.spec_from_file_location("router_unstartable_under_test", ROUTER)
    module = importlib.util.module_from_spec(spec)
    env = {
        "AGENTCORE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test",
        "AGENTCORE_QUALIFIER": "DEFAULT",
        "IDENTITY_TABLE_NAME": "openclaw-identity",
        "AWS_REGION": "us-west-2",
    }
    with patch.dict(os.environ, env), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)
    return module


def init_exceeded():
    return ClientError({"Error": {"Code": "RuntimeClientError", "Message":
                                  "Runtime initialization time exceeded. Please make sure that "
                                  "initialization completes in 120s."}}, "InvokeAgentRuntime")


def server_error():
    return ClientError({"Error": {"Code": "InternalServerException",
                                  "Message": "An unexpected error occurred while processing the request."}},
                       "InvokeAgentRuntime")


class SessionTable:
    def __init__(self, replaced_by=None):
        self.session = OLD
        self.replaced_by = replaced_by
        self.conditional_updates = 0

    def update_item(self, **kw):
        if kw.get("ConditionExpression"):
            self.conditional_updates += 1
            if self.replaced_by:
                self.session = self.replaced_by
            if self.session != kw["ExpressionAttributeValues"][":old"]:
                raise ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}},
                                  "UpdateItem")
            self.session = kw["ExpressionAttributeValues"][":new"]
        return {}

    def get_item(self, Key):
        return {"Item": {"sessionId": self.session}}


class Runtime:
    def __init__(self, old_error, reply="Here you go."):
        self.old_error = old_error
        self.reply = reply
        self.sessions = []

    def invoke_agent_runtime(self, **kw):
        self.sessions.append(kw["runtimeSessionId"])
        if kw["runtimeSessionId"] == OLD:
            raise self.old_error
        return {"statusCode": 200, "response": io.BytesIO(json.dumps({"response": self.reply}).encode())}


def _setup(old_error, **table_kw):
    module = _load_router()
    module.identity_table = SessionTable(**table_kw)
    module.agentcore_client = Runtime(old_error)
    return module


def test_chat_is_answered_on_a_new_session_when_the_old_one_cannot_start(caplog):
    module = _setup(init_exceeded())
    with caplog.at_level(logging.WARNING):
        result = module.invoke_agent_runtime_recovering(OLD, USER, "telegram:123", "telegram", "hi")
    assert result == {"response": "Here you go."}
    new = module.identity_table.session
    assert new != OLD and new.startswith(f"ses_{USER}_") and len(new) >= 33
    assert module.agentcore_client.sessions == [OLD, new]
    assert "could not start runtime session" in caplog.text  # feeds the unstartable-session alarm


def test_a_chat_and_a_brief_failing_together_share_one_new_session():
    other = "ses_" + "n" * 40
    module = _setup(init_exceeded(), replaced_by=other)
    result = module.invoke_agent_runtime_recovering(OLD, USER, "telegram:123", "telegram", "hi")
    assert result == {"response": "Here you go."}
    assert module.agentcore_client.sessions == [OLD, other]


def test_other_failures_keep_the_session_and_the_fixed_reply():
    module = _setup(server_error())
    result = module.invoke_agent_runtime_recovering(OLD, USER, "telegram:123", "telegram", "hi")
    assert result["response"].startswith("Sorry, I'm having trouble right now")
    assert "sessionUnstartable" not in result
    assert module.identity_table.session == OLD
    assert module.identity_table.conditional_updates == 0
    assert module.agentcore_client.sessions == [OLD]


def test_every_channel_handler_uses_the_recovering_invoke():
    src = ROUTER.read_text()
    assert src.count("invoke_agent_runtime_recovering(session_id, resolved_user_id") == 3
    assert "invoke_agent_runtime(session_id, resolved_user_id" not in src
