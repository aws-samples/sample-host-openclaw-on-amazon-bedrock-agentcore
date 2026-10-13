"""Verify an AgentCore Runtime V2 release with an isolated, real chat.

Uses a fresh deployment actor and session to keep the billed connection check
out of user state and channel delivery. Requires an acknowledgement plus
USER_READY and completed OpenClaw execution evidence for the requested binding
before returning warmup metadata for the deployment manifest. Callers supply
an IAM invocation client configured without retries because a failed request
may already have triggered model or tool execution.
"""

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone

from runtime_manifest import validate_v2_binding_evidence


class WarmupError(RuntimeError):
    pass


def verify_v2_evidence(body, expected):
    try:
        validate_v2_binding_evidence(body, expected)
    except ValueError as error:
        raise WarmupError(str(error)) from error


def run_warmup(client, runtime_arn, endpoint_name, *, expected_platform="V2", clock=time.monotonic,
               runtime_version=None, before_invoke=None):
    if expected_platform != "V2":
        raise WarmupError("warmup supports only V2")
    # A fresh actor keeps this billed, real chat out of user state and delivery.
    nonce = uuid.uuid4().hex
    session_id = f"deployment-warmup-{nonce}"
    payload = {
        "action": "chat",
        "userId": f"deployment-warmup-{nonce}",
        "actorId": f"deployment-warmup:{nonce}",
        "channel": "deployment-warmup",
        "sessionId": session_id,
        "runtimeSessionId": session_id,
        "routingGeneration": 0,
        "message": "Please acknowledge this connection check with OK.",
    }
    started = clock()
    attempt = {
        "mode": "chat", "status": "pending",
        "agentRuntimeArn": runtime_arn, "endpointName": endpoint_name,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        **{key: payload[key] for key in ("userId", "actorId", "sessionId", "runtimeSessionId")},
    }
    if runtime_version is not None:
        attempt["agentRuntimeVersion"] = runtime_version
    if before_invoke is not None:
        before_invoke(attempt)
    # Do not retry an invocation whose model/tool outcome may be unknown.
    result = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn, qualifier=endpoint_name,
        runtimeSessionId=session_id, contentType="application/json",
        accept="application/json", payload=json.dumps(payload).encode("utf-8"),
    )
    stream = result.get("response")
    try:
        if not stream:
            raise WarmupError("warmup response body is missing")
        raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise WarmupError("warmup response exceeds 1 MiB")
        if result.get("statusCode", 200) != 200:
            raise WarmupError("warmup returned a non-success HTTP status")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise WarmupError("warmup response is not an object")
        if body.get("error") or body.get("status") not in (None, "success", "completed", "ok"):
            raise WarmupError("warmup did not complete successfully")
        text = body.get("response")
        # AgentCore Runtime errors can use HTTP 200; require the requested acknowledgement.
        if not isinstance(text, str) or not re.match(r"(?is)^\s*(?:\*\*)?ok(?:\*\*)?(?:[.!]|\s|$)", text):
            raise WarmupError("warmup did not return the requested acknowledgement")
        if body.get("streamed"):
            raise WarmupError("warmup unexpectedly delivered a channel message")
        if body.get("userId", payload["userId"]) != payload["userId"]:
            raise WarmupError("warmup returned a different owner")
        verify_v2_evidence(body, payload)
        summary = {
            **attempt, "status": "succeeded",
            "elapsedSeconds": round(clock() - started, 3),
            "responseHash": hashlib.sha256(raw).hexdigest(),
        }
        summary.update({key: body[key] for key in (
            "runtimePlatform", "phase", "execution", "userId", "actorId", "sessionId", "runtimeSessionId",
        )})
        summary["contractVersion"] = 1
        return summary
    except (ValueError, TypeError) as error:
        raise WarmupError("warmup response is invalid JSON") from error
    finally:
        if stream is not None:
            stream.close()
