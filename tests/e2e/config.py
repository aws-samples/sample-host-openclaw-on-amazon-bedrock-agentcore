"""E2E config — auto-discover AWS resources from CloudFormation outputs and Secrets Manager."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


REQUIRED_REGION = "eu-west-1"


def _resolve_region() -> str:
    """Resolve only the canonical region, rejecting every explicit mismatch."""
    explicit = {
        name: os.environ.get(name)
        for name in ("CDK_DEFAULT_REGION", "AWS_REGION", "AWS_DEFAULT_REGION")
    }
    for name, region in explicit.items():
        if region and region != REQUIRED_REGION:
            raise RuntimeError(
                f"{name} must be exactly {REQUIRED_REGION}; got {region}"
            )
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    if cdk_json.exists():
        with open(cdk_json) as f:
            ctx = json.load(f).get("context", {})
            context_region = ctx.get("region")
            if context_region and context_region != REQUIRED_REGION:
                raise RuntimeError(
                    f"cdk context region must be exactly {REQUIRED_REGION}; "
                    f"got {context_region}"
                )
    return REQUIRED_REGION


@dataclass(frozen=True)
class E2EConfig:
    region: str
    api_url: str
    webhook_secret: str
    telegram_chat_id: str
    telegram_user_id: str
    workspace_session_role_arn: str
    log_group: str = "/openclaw/lambda/router"
    identity_table: str = "openclaw-identity"


def load_config() -> E2EConfig:
    """Build config from AWS resources. Raises on missing critical values."""
    region = _resolve_region()
    cf = boto3.client("cloudformation", region_name=region)
    sm = boto3.client("secretsmanager", region_name=region)

    # API URL from CloudFormation
    try:
        resp = cf.describe_stacks(StackName="OpenClawRouter")
        outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
        api_url = outputs.get("ApiUrl", "")
    except (ClientError, IndexError, KeyError) as e:
        raise RuntimeError(f"Cannot read OpenClawRouter stack outputs: {e}") from e

    if not api_url:
        raise RuntimeError("ApiUrl output not found in OpenClawRouter stack")

    try:
        resp = cf.describe_stacks(StackName="OpenClawAgentCore")
        outputs = {
            output["OutputKey"]: output["OutputValue"]
            for output in resp["Stacks"][0].get("Outputs", [])
        }
        workspace_session_role_arn = outputs.get("WorkspaceSessionRoleArn", "")
    except (ClientError, IndexError, KeyError) as e:
        raise RuntimeError(f"Cannot read OpenClawAgentCore stack outputs: {e}") from e

    if not workspace_session_role_arn:
        raise RuntimeError(
            "WorkspaceSessionRoleArn output not found in OpenClawAgentCore stack"
        )

    # Webhook secret from Secrets Manager
    try:
        resp = sm.get_secret_value(SecretId="openclaw/webhook-secret")
        webhook_secret = resp["SecretString"]
    except ClientError as e:
        raise RuntimeError(f"Cannot read webhook secret: {e}") from e

    # Telegram IDs from env vars
    chat_id = os.environ.get("E2E_TELEGRAM_CHAT_ID", "")
    user_id = os.environ.get("E2E_TELEGRAM_USER_ID", "")
    if not chat_id or not user_id:
        raise RuntimeError(
            "Set E2E_TELEGRAM_CHAT_ID and E2E_TELEGRAM_USER_ID env vars "
            "(your real Telegram IDs for webhook simulation)"
        )

    return E2EConfig(
        region=region,
        api_url=api_url.rstrip("/"),
        webhook_secret=webhook_secret,
        telegram_chat_id=chat_id,
        telegram_user_id=user_id,
        workspace_session_role_arn=workspace_session_role_arn,
    )
