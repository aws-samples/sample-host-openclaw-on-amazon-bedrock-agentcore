"""E2E access to saved V2 routes; never delete or reconstruct SESSION records."""

from importlib import import_module
import json
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from .config import E2EConfig


def _routing():
    # Import the production validator lazily; collection does not load AWS state.
    return import_module("lambda.shared.python.runtime_routing")


def _get_table(cfg: E2EConfig):
    return boto3.resource("dynamodb", region_name=cfg.region).Table(cfg.identity_table)


def get_user_id(cfg: E2EConfig) -> Optional[str]:
    """Read the canonical owner; lookup errors must not imply a new user."""
    item = _get_table(cfg).get_item(
        Key={"PK": f"CHANNEL#telegram:{cfg.telegram_user_id}", "SK": "PROFILE"},
        ConsistentRead=True,
    ).get("Item")
    return item["userId"] if item else None


def get_session_route(cfg: E2EConfig, user_id: Optional[str] = None):
    """Resolve the saved active V2 route without manifest or default-ARN fallback."""
    user_id = user_id or get_user_id(cfg)
    if not user_id:
        raise RuntimeError("No registered E2E user; register a dedicated new test identity first")
    table = _get_table(cfg)
    route = _routing().resolve_route(table, user_id)
    profile = table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": "PROFILE"}, ConsistentRead=True,
    ).get("Item", {})
    if (profile.get("runtimeEnrollment") != "V2"
            or profile.get("enrollmentActorId") != route.actor_id):
        raise RuntimeError("Canonical V2 binding ownership requires reconciliation")
    return route


def get_session_id(cfg: E2EConfig, user_id: str) -> str:
    return get_session_route(cfg, user_id).session_id


def _stop_agentcore_session(cfg: E2EConfig) -> bool:
    """Stop only the saved test session, retaining its route and identity.

    This is not a cold-start reset, migration, or proof of a complete checkpoint.
    Coordinate with other callers before using it.
    """
    route = get_session_route(cfg)
    client = boto3.client("bedrock-agentcore", region_name=cfg.region)
    try:
        client.stop_runtime_session(**route.invocation())
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return False
        raise


def _invoke(cfg: E2EConfig, route, payload: dict) -> dict:
    client = boto3.client(
        "bedrock-agentcore", region_name=cfg.region,
        config=Config(read_timeout=600, retries={"total_max_attempts": 1}),
    )
    response = client.invoke_agent_runtime(
        **route.invocation(), payload=json.dumps(payload).encode(),
        contentType="application/json", accept="application/json",
    )
    body = response["response"]
    try:
        result = json.loads(body.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("AgentCore Runtime response must be an object")
        return result
    finally:
        body.close()


def _payload(route, action: str) -> dict:
    return {
        "action": action, "userId": route.user_id, "actorId": route.actor_id,
        "sessionId": route.session_id, **route.payload_context(),
        # Direct diagnostics must not deliver a channel message.
        "channel": "e2e", "channelActorId": None, "channelTarget": None,
    }


def get_agent_status(cfg: E2EConfig) -> dict:
    """Read status through the saved route with immutable binding metadata."""
    route = get_session_route(cfg)
    outer = _invoke(cfg, route, _payload(route, "status"))
    inner = outer.get("response", {})
    status = json.loads(inner) if isinstance(inner, str) else inner
    if not isinstance(status, dict) or status.get("runtimePlatform") != "V2":
        raise RuntimeError("Expected V2 status")
    binding = status.get("lifecycle", {}).get("binding")
    if status.get("lifecycle", {}).get("state") == "USER_READY" and not isinstance(binding, dict):
        raise RuntimeError("Ready status lacks immutable binding evidence")
    if binding is not None:
        expected = {
            "userId": route.user_id, "actorId": route.actor_id,
            "runtimeSessionId": route.session_id, "routingGeneration": route.generation,
        }
        if any(binding.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Status binding differs from the saved V2 route")
    return status


def get_chat_completion(cfg: E2EConfig, message: str) -> dict:
    """Run one billed diagnostic chat with shared admission and no automatic retry.

    The Router log preview truncates execution metadata; read the complete body.
    Uncertain outcomes retain admission for reconciliation.
    """
    route = get_session_route(cfg)
    routing = _routing()
    with routing.admit_route(_get_table(cfg), route, route.user_id, route.actor_id) as admission:
        try:
            result = _invoke(cfg, route, {**_payload(route, "chat"), "message": message})
            if routing.is_unknown_outcome(result):
                admission.retain()
            return result
        except Exception:
            admission.retain()
            raise
