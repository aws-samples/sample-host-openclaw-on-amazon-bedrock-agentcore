"""E2E tests for the AgentCore Gateway MCP tools (``enable_gateway=true``).

Each test sends real chat turns through Telegram webhook -> Router Lambda ->
AgentCore container -> OpenClaw 2.0 -> Gateway -> tool Lambda, and then proves
from logs *how* the reply was produced:

* the container that answered ran the real OpenClaw 2.0 gateway to ready
  (``[contract] OpenClaw is ready on port``), not the warm-up shim, and the
  reply carries no ``warm-up mode`` footer;
* the bridge wrote ``mcp.servers.agentcore`` into openclaw.json;
* OpenClaw's embedded agent started an MCP tool (``tool=<...list_files>``
  etc.), not ``exec``;
* the tool Lambda logged a ``gateway_tool_call`` line whose ``namespace`` is
  the caller's own namespace derived from the verified Cognito token — which
  is also how the "other user's folder" case is proven: the model may ask for
  anyone, the Lambda still only ever sees the caller.

Run selectively (needs a us-west-2 staging deployment with the flag on):

    E2E_TELEGRAM_CHAT_ID=... E2E_TELEGRAM_USER_ID=... \
        pytest tests/e2e/test_gateway_tools.py -v -m gateway

Skipped automatically when the ``OpenClawGateway`` stack is not deployed, so
the suite is safe to collect against a flag-off deployment.
"""

import re
import time
import uuid

import boto3
import pytest

from . import container_logs as cl
from .log_tailer import tail_logs
from .webhook import post_webhook

pytestmark = [pytest.mark.gateway, pytest.mark.e2e]

_RESPONSE_TIMEOUT_S = 300
_OTHER_NAMESPACE = "telegram_123456789"  # a namespace the E2E user must never reach
_USER_FILES_BUCKET_OUTPUT = "UserFilesBucketName"


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture(scope="module")
def gateway_outputs(e2e_config):
    out = cl.gateway_stack_outputs(e2e_config)
    if not out.get("GatewayUrl"):
        pytest.skip("OpenClawGateway stack not deployed (enable_gateway=false)")
    return out


@pytest.fixture(scope="module")
def run_id():
    return uuid.uuid4().hex[:8]


class Turn:
    """One chat turn plus the log evidence for it."""

    def __init__(self, cfg, text: str):
        self.text = text
        self.start_ms = _now_ms()
        wr = post_webhook(cfg, text)
        assert wr.status_code == 200, f"webhook POST failed: {wr.status_code} {wr.body[:200]}"
        self.tail = tail_logs(cfg, since_ms=self.start_ms - 2000, timeout_s=_RESPONSE_TIMEOUT_S)
        assert not self.tail.timed_out, f"no router response within {_RESPONSE_TIMEOUT_S}s for: {text!r}"
        assert self.tail.full_lifecycle, f"incomplete lifecycle for {text!r}: {self.tail.raw_lines[-5:]}"
        self.end_ms = _now_ms()
        self.reply = self.tail.response_text
        self.stream = cl.latest_stream(cfg)
        assert self.stream, "no container log stream for the E2E user"
        self.tools = cl.tool_calls(cfg, self.stream, self.start_ms, self.end_ms)
        self.gw = cl.gateway_tool_calls(cfg, self.start_ms, self.end_ms)
        self._cfg = cfg

    # ---- evidence assertions --------------------------------------------
    def assert_real_openclaw(self):
        assert not self.tail.is_warmup, f"reply came from the warm-up shim: {self.reply[:200]}"
        assert cl.openclaw_ready(self._cfg, self.stream), (
            f"container {self.stream} never logged 'OpenClaw is ready on port'"
        )
        assert cl.mcp_config_written(self._cfg, self.stream), (
            f"bridge did not write mcp.servers.agentcore in {self.stream}"
        )

    def assert_via_gateway(self, tool: str, target: str, namespace: str):
        assert self.tools.used_mcp(tool), (
            f"OpenClaw did not start an MCP tool ending in {tool!r}; started={self.tools.started}"
        )
        assert not self.tools.used_exec, f"exec skill was used instead of Gateway: {self.tools.started}"
        calls = [c for c in self.gw if c.get("tool") == tool and c["_target"] == target]
        assert calls, f"no gateway_tool_call for {target}/{tool} in Lambda logs; got {self.gw}"
        for c in calls:
            assert c.get("namespace") == namespace, f"Lambda saw namespace {c.get('namespace')!r}, expected {namespace!r}"
            assert c.get("tokenUse") in ("id", "access"), f"unexpected tokenUse in audit line: {c}"
        return calls


def _user_files_bucket(cfg) -> str:
    """UserFilesBucketName output of OpenClawAgentCore (bucket shared with the exec skill)."""
    cf = boto3.client("cloudformation", region_name=cfg.region)
    outs = {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName="OpenClawAgentCore")["Stacks"][0].get("Outputs", [])}
    return outs[_USER_FILES_BUCKET_OUTPUT]


# --------------------------------------------------------------------------
# Files: list / write / read / delete in the caller's own folder
# --------------------------------------------------------------------------
class TestUserFilesViaGateway:
    def test_write_read_delete_own_folder(self, e2e_config, gateway_outputs, run_id):
        ns = cl.namespace(e2e_config)
        fname = f"gw-e2e-{run_id}.txt"
        marker = f"GATEWAY_E2E_{run_id.upper()}"
        bucket = _user_files_bucket(e2e_config)
        s3 = boto3.client("s3", region_name=e2e_config.region)

        # write
        t = Turn(e2e_config, f'Use your file tools to save exactly the text "{marker}" to a file named {fname}.')
        t.assert_real_openclaw()
        t.assert_via_gateway("write_file", "user-files", ns)
        obj = s3.get_object(Bucket=bucket, Key=f"{ns}/{fname}")
        assert obj["Body"].read().decode() == marker, "object content written via Gateway does not match"
        assert "Contents" not in s3.list_objects_v2(Bucket=bucket, Prefix=f"{_OTHER_NAMESPACE}/{fname}"), (
            "file leaked into another namespace"
        )

        # list
        t = Turn(e2e_config, "List the files in my folder using your file tools. Reply with the filenames only.")
        t.assert_real_openclaw()
        t.assert_via_gateway("list_files", "user-files", ns)
        assert fname in t.reply, f"list reply does not mention {fname}: {t.reply[:300]}"

        # read
        t = Turn(e2e_config, f"Read the file {fname} with your file tools and reply with its exact contents.")
        t.assert_real_openclaw()
        t.assert_via_gateway("read_file", "user-files", ns)
        assert marker in t.reply, f"read reply does not contain the marker: {t.reply[:300]}"

        # delete
        t = Turn(e2e_config, f"Delete the file {fname} using your file tools and confirm.")
        t.assert_real_openclaw()
        t.assert_via_gateway("delete_file", "user-files", ns)
        with pytest.raises(s3.exceptions.NoSuchKey):
            s3.get_object(Bucket=bucket, Key=f"{ns}/{fname}")

    def test_other_users_folder_is_never_reached(self, e2e_config, gateway_outputs, run_id):
        """The model may be asked for anyone's folder; the Lambda only ever sees the caller.

        Two acceptable outcomes: OpenClaw refuses without a tool call, or it calls
        list_files and the Lambda audit line shows the caller's own namespace. What
        is never acceptable is a gateway_tool_call with the other namespace, or a
        reply that lists a canary object planted in the other namespace.
        """
        ns = cl.namespace(e2e_config)
        bucket = _user_files_bucket(e2e_config)
        s3 = boto3.client("s3", region_name=e2e_config.region)
        canary = f"canary-{run_id}.txt"
        s3.put_object(Bucket=bucket, Key=f"{_OTHER_NAMESPACE}/{canary}", Body=b"must-not-be-visible")
        try:
            t = Turn(
                e2e_config,
                f"Use your file tools to list the files in the folder of user {_OTHER_NAMESPACE} "
                f"(user_id {_OTHER_NAMESPACE.split('_')[1]}), not mine. Reply with the filenames only.",
            )
            t.assert_real_openclaw()
            assert not t.tools.used_exec, f"exec skill used: {t.tools.started}"
            for c in t.gw:
                assert c.get("namespace") == ns, f"Lambda resolved a foreign namespace: {c}"
            assert canary not in t.reply, f"another user's file was disclosed: {t.reply[:300]}"
            if not t.gw:
                # refused before calling any tool: the reply must say so rather than fabricate a listing
                assert re.search(r"can(?:'|no)t|only|not able|unable|own|permission|access", t.reply, re.I), (
                    f"no tool call and no refusal in reply: {t.reply[:300]}"
                )
        finally:
            s3.delete_object(Bucket=bucket, Key=f"{_OTHER_NAMESPACE}/{canary}")


# --------------------------------------------------------------------------
# Schedules: create / list / delete in the caller's own group prefix
# --------------------------------------------------------------------------
class TestSchedulesViaGateway:
    def test_create_list_delete_own_schedule(self, e2e_config, gateway_outputs, run_id):
        ns = cl.namespace(e2e_config)
        sched_name = f"gw-e2e-{run_id}"
        scheduler = boto3.client("scheduler", region_name=e2e_config.region)

        def own_schedules():
            names = []
            token = None
            while True:
                kw = {"GroupName": "openclaw-cron", "NamePrefix": f"openclaw-{ns}-", "MaxResults": 100}
                if token:
                    kw["NextToken"] = token
                r = scheduler.list_schedules(**kw)
                names += [s["Name"] for s in r.get("Schedules", [])]
                token = r.get("NextToken")
                if not token:
                    return names

        before = set(own_schedules())

        t = Turn(
            e2e_config,
            f'Use your schedule tools to create a schedule named "{sched_name}" that runs once a day at 03:15 UTC '
            f'and sends me the message "gateway e2e {run_id}". Confirm the schedule id.',
        )
        t.assert_real_openclaw()
        t.assert_via_gateway("create_schedule", "schedules", ns)
        created = set(own_schedules()) - before
        assert len(created) == 1, f"expected exactly one new schedule under openclaw-{ns}-, got {created}"
        eb_name = created.pop()
        desc = scheduler.get_schedule(Name=eb_name, GroupName="openclaw-cron")
        assert sched_name in (desc.get("Description") or "") or sched_name in str(desc.get("Target", {}).get("Input", "")), (
            f"created schedule does not carry the requested name: {desc.get('Description')}"
        )

        t = Turn(e2e_config, "List my schedules using your schedule tools. Reply with names and ids only.")
        t.assert_real_openclaw()
        t.assert_via_gateway("list_schedules", "schedules", ns)
        assert sched_name in t.reply, f"list reply does not mention {sched_name}: {t.reply[:300]}"

        t = Turn(e2e_config, f'Delete my schedule named "{sched_name}" using your schedule tools and confirm.')
        t.assert_real_openclaw()
        t.assert_via_gateway("delete_schedule", "schedules", ns)
        assert eb_name not in own_schedules(), f"{eb_name} still exists after delete"


# --------------------------------------------------------------------------
# Token type accepted by the Gateway (evidence for the design question)
# --------------------------------------------------------------------------
def test_gateway_accepts_bearer_recorded_in_audit_lines(e2e_config, gateway_outputs):
    """Every gateway_tool_call in the last 30 min records which Cognito token type reached the Lambda."""
    end = _now_ms()
    calls = cl.gateway_tool_calls(e2e_config, end - 30 * 60 * 1000, end, settle_s=0)
    if not calls:
        pytest.skip("no gateway_tool_call lines in the last 30 minutes; run the CRUD tests first")
    uses = {c.get("tokenUse") for c in calls}
    assert uses <= {"id", "access"}, f"unexpected tokenUse values: {uses}"
    print(f"\n[evidence] tokenUse values accepted by Gateway in the window: {sorted(uses)} over {len(calls)} calls")
