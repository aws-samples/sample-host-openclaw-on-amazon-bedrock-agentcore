"""Container-side evidence for E2E tests.

The bridge ships the container's stdout/stderr to the CloudWatch log group
``/openclaw/container`` (bridge/cloudwatch-logger.js), one stream per
container named ``<namespace>-<boot ms>``. The Gateway tool Lambdas log one
``gateway_tool_call`` JSON line per invocation (lambda/gateway_tools/lib/mcp.js).

These helpers let a test prove *how* a reply was produced, not just that a
reply arrived:

* :func:`openclaw_ready` — the real OpenClaw 2.0 gateway reached ready in this
  container (``[contract] OpenClaw is ready on port``), i.e. the reply did not
  come from the warm-up shim.
* :func:`tool_calls` — the ``tool=`` names OpenClaw's embedded agent started in
  a time window (``exec`` for the exec skills; the MCP tool name for Gateway
  tools).
* :func:`gateway_tool_calls` — the Lambda-side audit lines in a time window,
  with the verified namespace and the Cognito token type that was accepted.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import boto3

from .config import E2EConfig

CONTAINER_LOG_GROUP = "/openclaw/container"
GATEWAY_LAMBDA_LOG_GROUPS = {
    "user-files": "/openclaw/lambda/gateway-userfiles",
    "schedules": "/openclaw/lambda/gateway-schedules",
    "interceptor": "/openclaw/lambda/gateway-interceptor",
}

_TOOL_START = re.compile(r"embedded run tool start: runId=\S+ tool=(\S+)")
_TOOL_END = re.compile(r"embedded run tool end: runId=\S+ tool=(\S+)")


def namespace(cfg: E2EConfig) -> str:
    return f"telegram_{cfg.telegram_user_id}"


def _logs(cfg: E2EConfig):
    return boto3.client("logs", region_name=cfg.region)


def latest_stream(cfg: E2EConfig) -> Optional[str]:
    """Newest container log stream for the E2E user."""
    resp = _logs(cfg).describe_log_streams(
        logGroupName=CONTAINER_LOG_GROUP, orderBy="LastEventTime", descending=True, limit=10
    )
    ns = namespace(cfg)
    for s in resp.get("logStreams", []):
        if s["logStreamName"].startswith(ns):
            return s["logStreamName"]
    return None


def stream_events(cfg: E2EConfig, stream: str, start_ms: Optional[int] = None) -> List[dict]:
    client = _logs(cfg)
    events, token = [], None
    while True:
        kw = {"logGroupName": CONTAINER_LOG_GROUP, "logStreamName": stream, "startFromHead": True, "limit": 10000}
        if start_ms:
            kw["startTime"] = start_ms
        if token:
            kw["nextToken"] = token
        try:
            resp = client.get_log_events(**kw)
        except client.exceptions.ResourceNotFoundException:
            return events
        events.extend(resp["events"])
        if resp.get("nextForwardToken") == token or not resp["events"]:
            return events
        token = resp["nextForwardToken"]


def openclaw_ready(cfg: E2EConfig, stream: str) -> bool:
    """True when the OpenClaw 2.0 gateway reached ready in this container."""
    return any("OpenClaw is ready on port" in e["message"] for e in stream_events(cfg, stream))


def mcp_config_written(cfg: E2EConfig, stream: str) -> bool:
    """True when the bridge wrote mcp.servers.agentcore into openclaw.json."""
    return any("mcp.servers.agentcore enabled" in e["message"] for e in stream_events(cfg, stream))


@dataclass
class ToolWindow:
    started: List[str] = field(default_factory=list)
    ended: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    proxy_requests: int = 0

    @property
    def used_exec(self) -> bool:
        return "exec" in self.started

    def used_mcp(self, tool_suffix: str) -> bool:
        """True if some non-exec tool whose name ends with tool_suffix was started."""
        return any(t != "exec" and t.endswith(tool_suffix) for t in self.started)


def tool_calls(cfg: E2EConfig, stream: str, start_ms: int, end_ms: int) -> ToolWindow:
    """Tools started by OpenClaw's embedded agent between two timestamps."""
    win = ToolWindow()
    for e in stream_events(cfg, stream, start_ms - 1000):
        if e["timestamp"] > end_ms + 3000:
            break
        m = _TOOL_START.search(e["message"])
        if m:
            win.started.append(m.group(1))
        m = _TOOL_END.search(e["message"])
        if m:
            win.ended.append(m.group(1))
        if "Incoming request:" in e["message"]:
            win.proxy_requests += 1
        if "isError=true" in e["message"]:
            win.errors.append(e["message"][:200])
    return win


def gateway_tool_calls(cfg: E2EConfig, start_ms: int, end_ms: int, settle_s: float = 5.0) -> List[dict]:
    """``gateway_tool_call`` audit lines from the tool Lambdas in a window."""
    time.sleep(settle_s)  # Lambda logs land a few seconds after the invocation
    client = _logs(cfg)
    out: List[dict] = []
    for target, group in GATEWAY_LAMBDA_LOG_GROUPS.items():
        if target == "interceptor":
            continue
        kw = {
            "logGroupName": group,
            "startTime": start_ms - 2000,
            "endTime": end_ms + 15000,
            "filterPattern": '"gateway_tool_call"',
        }
        while True:
            try:
                resp = client.filter_log_events(**kw)
            except client.exceptions.ResourceNotFoundException:
                break
            for e in resp.get("events", []):
                try:
                    rec = json.loads(e["message"][e["message"].index("{"):])
                except (ValueError, json.JSONDecodeError):
                    continue
                rec["_target"] = target
                rec["_ts"] = e["timestamp"]
                out.append(rec)
            if "nextToken" in resp:
                kw["nextToken"] = resp["nextToken"]
            else:
                break
    return sorted(out, key=lambda r: r["_ts"])


def gateway_stack_outputs(cfg: E2EConfig) -> Dict[str, str]:
    """Outputs of OpenClawGateway, or {} when the stack is not deployed."""
    cf = boto3.client("cloudformation", region_name=cfg.region)
    try:
        resp = cf.describe_stacks(StackName="OpenClawGateway")
    except cf.exceptions.ClientError:
        return {}
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
