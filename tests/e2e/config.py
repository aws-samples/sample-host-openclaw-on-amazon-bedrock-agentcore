"""AWS config auto-discovery for E2E tests.

Resolves all configuration from AWS without hardcoded values:
- API URL: CloudFormation stack OpenClawRouter output ApiUrl
- Webhook secret: Secrets Manager openclaw/webhook-secret
- Region: CDK_DEFAULT_REGION env -> cdk.json context -> boto3 session
- Log group: /openclaw/lambda/router
- Identity table: openclaw-identity
- Telegram IDs: E2E_TELEGRAM_CHAT_ID / E2E_TELEGRAM_USER_ID env vars
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


LOG_GROUP_NAME = "/openclaw/lambda/router"
IDENTITY_TABLE_NAME = "openclaw-identity"
ROUTER_STACK_NAME = "OpenClawRouter"


def _resolve_region(override: str | None = None) -> str:
    """Resolve AWS region: explicit override -> env var -> cdk.json -> boto3 session."""
    if override:
        return override

    env_region = os.environ.get("CDK_DEFAULT_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if env_region:
        return env_region

    cdk_json_path = Path(__file__).resolve().parents[2] / "cdk.json"
    if cdk_json_path.exists():
        try:
            with open(cdk_json_path) as f:
                ctx = json.load(f).get("context", {})
            region = ctx.get("region", "")
            if region:
                return region
        except (json.JSONDecodeError, OSError):
            pass

    session = boto3.session.Session()
    return session.region_name or "us-west-2"


def _get_stack_output(cf_client, stack_name: str, output_key: str) -> str:
    """Read a single CloudFormation stack output value."""
    resp = cf_client.describe_stacks(StackName=stack_name)
    for stack in resp.get("Stacks", []):
        for output in stack.get("Outputs", []):
            if output["OutputKey"] == output_key:
                return output["OutputValue"]
    raise ValueError(f"Output {output_key!r} not found in stack {stack_name!r}")


def _get_secret(sm_client, secret_id: str) -> str:
    """Read a Secrets Manager secret value."""
    resp = sm_client.get_secret_value(SecretId=secret_id)
    return resp["SecretString"]


@dataclass(frozen=True)
class E2EConfig:
    """Frozen configuration for E2E tests."""

    region: str
    api_url: str
    webhook_secret: str
    log_group: str
    identity_table: str
    chat_id: str
    user_id: str

    @property
    def webhook_url(self) -> str:
        """Full Telegram webhook URL."""
        base = self.api_url.rstrip("/")
        return f"{base}/webhook/telegram"

    @property
    def health_url(self) -> str:
        """Health check endpoint URL."""
        base = self.api_url.rstrip("/")
        return f"{base}/health"

    @property
    def actor_id(self) -> str:
        """Telegram actor ID for DynamoDB lookups."""
        return f"telegram:{self.user_id}"


def load_config(
    *,
    region: str | None = None,
    chat_id: str | None = None,
    user_id: str | None = None,
) -> E2EConfig:
    """Load E2E config from AWS, env vars, and cdk.json.

    Args:
        region: AWS region override.
        chat_id: Telegram chat ID override (falls back to E2E_TELEGRAM_CHAT_ID env).
        user_id: Telegram user ID override (falls back to E2E_TELEGRAM_USER_ID env).

    Raises:
        ValueError: If required Telegram IDs are not provided.
        ClientError: If AWS API calls fail.
    """
    resolved_region = _resolve_region(region)

    chat_id = chat_id or os.environ.get("E2E_TELEGRAM_CHAT_ID", "")
    user_id = user_id or os.environ.get("E2E_TELEGRAM_USER_ID", "")

    if not chat_id or not user_id:
        raise ValueError(
            "Telegram IDs required. Set E2E_TELEGRAM_CHAT_ID and "
            "E2E_TELEGRAM_USER_ID environment variables, or pass --chat-id / --user-id."
        )

    cf_client = boto3.client("cloudformation", region_name=resolved_region)
    sm_client = boto3.client("secretsmanager", region_name=resolved_region)

    api_url = _get_stack_output(cf_client, ROUTER_STACK_NAME, "ApiUrl")
    webhook_secret = _get_secret(sm_client, "openclaw/webhook-secret")

    return E2EConfig(
        region=resolved_region,
        api_url=api_url,
        webhook_secret=webhook_secret,
        log_group=LOG_GROUP_NAME,
        identity_table=IDENTITY_TABLE_NAME,
        chat_id=str(chat_id),
        user_id=str(user_id),
    )


def load_health_config(*, region: str | None = None) -> E2EConfig:
    """Load minimal config for health-check-only operations.

    Only resolves the API URL — does not require Telegram IDs or webhook secret.
    """
    resolved_region = _resolve_region(region)
    cf_client = boto3.client("cloudformation", region_name=resolved_region)
    api_url = _get_stack_output(cf_client, ROUTER_STACK_NAME, "ApiUrl")

    return E2EConfig(
        region=resolved_region,
        api_url=api_url,
        webhook_secret="",
        log_group=LOG_GROUP_NAME,
        identity_table=IDENTITY_TABLE_NAME,
        chat_id="",
        user_id="",
    )
