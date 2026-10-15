"""Test doubles for the V1 cron Lambda (lambda/cron/index.py).

The module is loaded fresh per test with boto3 client construction patched, so
nothing reaches AWS. Tests replace identity_table / agentcore_client / s3_client
with the fakes below. All IDs are placeholders.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "us-west-2"
USER = "user_abc123"
ACTOR = "telegram:123"
SCHEDULE_ID = "a1b2c3d4"

_ENV = {
    "AGENTCORE_RUNTIME_ARN": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/test",
    "AGENTCORE_QUALIFIER": "DEFAULT",
    "IDENTITY_TABLE_NAME": "openclaw-identity",
    "TELEGRAM_TOKEN_SECRET_ID": "openclaw/channels/telegram",
    "AWS_REGION": REGION,
    "LAMBDA_TIMEOUT_SECONDS": "900",
}


def load_cron_lambda():
    """Import a fresh copy of lambda/cron/index.py with no real AWS clients."""
    path = ROOT / "lambda" / "cron" / "index.py"
    spec = importlib.util.spec_from_file_location("cron_index_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, _ENV), \
            patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
        spec.loader.exec_module(module)
    module.identity_table = FakeTable()
    return module


class FakeTable:
    """identity table holding the user's session and the schedule's CRON# record."""

    def __init__(self, owned=True):
        self.items = {(f"USER#{USER}", "SESSION"): {"sessionId": "ses_" + "s" * 40}}
        if owned:
            self.items[(f"USER#{USER}", f"CRON#{SCHEDULE_ID}")] = {"scheduleId": SCHEDULE_ID}

    def get_item(self, Key):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": item} if item else {}

    def update_item(self, **_kwargs):
        return {}

    def put_item(self, **_kwargs):
        return {}


class FakeRuntime:
    """invoke_agent_runtime stub: each queued result is a dict body, raw bytes, or an exception."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, bytes):
            return {"response": io.BytesIO(result)}
        return {"response": io.BytesIO(json.dumps(result).encode())}

    @property
    def actions(self):
        return [json.loads(call["payload"])["action"] for call in self.calls]


READY = {"status": "ready"}


def cron_event(**overrides):
    event = {
        "userId": USER,
        "actorId": ACTOR,
        "channel": "telegram",
        "channelTarget": "123",
        "message": "Send my morning brief",
        "scheduleId": SCHEDULE_ID,
        "scheduleName": "Morning brief",
    }
    event.update(overrides)
    return event


def run_app(tmp_path):
    """Synthesize app.py offline and return {template file name: template}."""
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({
        "account": ACCOUNT, "region": REGION,
        "availability_zones": [REGION + "a", REGION + "b"],
        f"availability-zones:account={ACCOUNT}:region={REGION}": [REGION + "a", REGION + "b"],
    })
    env = {key: os.environ[key] for key in ("PATH", "HOME", "JSII_NODE", "JSII_RUNTIME_PACKAGE_CACHE_ROOT", "TMPDIR")
           if key in os.environ}
    env.update({
        "CDK_CONTEXT_JSON": json.dumps(context), "CDK_OUTDIR": str(tmp_path / "cdk.out"),
        "CDK_DEFAULT_ACCOUNT": ACCOUNT, "CDK_DEFAULT_REGION": REGION,
        "AWS_EC2_METADATA_DISABLED": "true",
    })
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assembly = json.loads((tmp_path / "cdk.out/manifest.json").read_text())
    assert not assembly.get("missing"), "Offline synth must not require context lookups"
    return {path.name: json.loads(path.read_text()) for path in (tmp_path / "cdk.out").glob("*.template.json")}
