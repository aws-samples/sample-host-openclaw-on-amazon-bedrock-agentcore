"""DynamoDB session and user management for E2E tests."""

import json
from typing import Optional

import boto3
from botocore.exceptions import ClientError

from .config import E2EConfig


def _get_table(cfg: E2EConfig):
    dynamodb = boto3.resource("dynamodb", region_name=cfg.region)
    return dynamodb.Table(cfg.identity_table)


def get_user_id(cfg: E2EConfig) -> Optional[str]:
    """Look up the user ID for the E2E Telegram user."""
    table = _get_table(cfg)
    channel_key = f"telegram:{cfg.telegram_user_id}"
    try:
        resp = table.get_item(Key={"PK": f"CHANNEL#{channel_key}", "SK": "PROFILE"})
        item = resp.get("Item")
        return item["userId"] if item else None
    except ClientError:
        return None


def get_session_id(cfg: E2EConfig, user_id: str) -> Optional[str]:
    """Get the current session ID for a user."""
    table = _get_table(cfg)
    try:
        resp = table.get_item(Key={"PK": f"USER#{user_id}", "SK": "SESSION"})
        item = resp.get("Item")
        return item["sessionId"] if item else None
    except ClientError:
        return None


def reset_session(cfg: E2EConfig) -> bool:
    """Delete the session record for the E2E user, forcing a new session on next message.

    Returns True if a session was deleted, False if no session existed.
    """
    user_id = get_user_id(cfg)
    if not user_id:
        return False

    table = _get_table(cfg)
    try:
        resp = table.delete_item(
            Key={"PK": f"USER#{user_id}", "SK": "SESSION"},
            ReturnValues="ALL_OLD",
        )
        return "Attributes" in resp
    except ClientError:
        return False


def _stop_agentcore_session(cfg: E2EConfig) -> bool:
    """Stop the AgentCore runtime session for the E2E user.

    This terminates the container, ensuring the next message triggers a
    true cold start (new container pull + init).

    Returns True if session was stopped, False if already terminated.
    """
    user_id = get_user_id(cfg)
    if not user_id:
        return False

    session_id = get_session_id(cfg, user_id)
    if not session_id:
        return False

    client = boto3.client("bedrock-agentcore", region_name=cfg.region)

    # Resolve the runtime ARN from CloudFormation outputs
    cf = boto3.client("cloudformation", region_name=cfg.region)
    try:
        stacks = cf.describe_stacks(StackName="OpenClawAgentCore")
        outputs = stacks["Stacks"][0].get("Outputs", [])
        runtime_arn = next(
            (o["OutputValue"] for o in outputs if o["OutputKey"] == "RuntimeArn"),
            None,
        )
    except (ClientError, StopIteration, IndexError):
        return False

    if not runtime_arn:
        return False

    try:
        client.stop_runtime_session(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
        )
        return True
    except ClientError:
        # Session already terminated
        return False


def reset_user(cfg: E2EConfig) -> int:
    """Reset the E2E user's session without deleting their identity records.

    Only removes the SESSION record, forcing a cold start on next message.
    The CHANNEL# mapping and USER# profile are preserved so the user remains
    registered and does not hit the allowlist check.

    Returns 1 if a session record was deleted, 0 if none existed.

    .. deprecated::
        The previous behaviour (deleting all identity records) caused real
        users to lose their registration when E2E tests ran against production.
        Use reset_session() directly if you only need a session reset.
    """
    deleted = 1 if reset_session(cfg) else 0
    return deleted


def get_agent_status(cfg: E2EConfig) -> Optional[dict]:
    """Query the AgentCore contract status endpoint for a user's session.

    Invokes the contract server's ``action: status`` which returns diagnostics
    including chatRequestCount and subagentRequestCount from the proxy /health.

    Returns the parsed status dict, or None if the session doesn't exist or
    the invocation fails.
    """
    user_id = get_user_id(cfg)
    if not user_id:
        return None

    session_id = get_session_id(cfg, user_id)
    if not session_id:
        return None

    # Resolve runtime ARN and qualifier from CloudFormation
    cf = boto3.client("cloudformation", region_name=cfg.region)
    try:
        stacks = cf.describe_stacks(StackName="OpenClawAgentCore")
        outputs = stacks["Stacks"][0].get("Outputs", [])
        runtime_arn = next(
            (o["OutputValue"] for o in outputs if o["OutputKey"] == "RuntimeArn"),
            None,
        )
        qualifier = next(
            (o["OutputValue"] for o in outputs
             if o["OutputKey"] == "RuntimeEndpointArn"),
            None,
        )
    except (ClientError, StopIteration, IndexError):
        return None

    if not runtime_arn:
        return None

    client = boto3.client("bedrock-agentcore", region_name=cfg.region)
    payload = json.dumps({"action": "status"}).encode()

    try:
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier or "",
            runtimeSessionId=session_id,
            payload=payload,
            contentType="application/json",
            accept="application/json",
        )
        body = resp.get("response")
        if body:
            body_text = (body.read().decode("utf-8")
                         if hasattr(body, "read") else str(body))
            outer = json.loads(body_text)
            # The contract wraps status in {"response": JSON.stringify(diag)}
            inner = outer.get("response", "{}")
            return json.loads(inner) if isinstance(inner, str) else inner
    except Exception:
        return None

    return None
