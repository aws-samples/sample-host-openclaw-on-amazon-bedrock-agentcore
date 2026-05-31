"""E2E config — auto-discover AWS resources from CloudFormation outputs and Secrets Manager."""

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def _resolve_region() -> str:
    """Resolve AWS region: env var -> cdk.json -> boto3 session."""
    region = os.environ.get("CDK_DEFAULT_REGION")
    if region:
        return region
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    if cdk_json.exists():
        with open(cdk_json) as f:
            ctx = json.load(f).get("context", {})
            if ctx.get("region"):
                return ctx["region"]
    return boto3.session.Session().region_name or "ap-southeast-2"


def _resolve_env_suffix() -> str:
    """Resolve env suffix from env var or cdk.json context."""
    raw = os.environ.get("OPENCLAW_ENV_SUFFIX")
    if raw is None:
        cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
        if cdk_json.exists():
            with open(cdk_json) as f:
                raw = str(json.load(f).get("context", {}).get("environment_suffix", "") or "")
    if raw is None:
        return ""
    suffix = raw.strip().lower().strip("-")
    if suffix and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", suffix):
        raise RuntimeError("environment_suffix must use lowercase letters, digits, and hyphens only")
    return suffix


def _with_suffix(base: str, suffix: str) -> str:
    return f"{base}-{suffix}" if suffix else base


@dataclass(frozen=True)
class E2EConfig:
    region: str
    api_url: str
    webhook_secret: str
    telegram_chat_id: str
    telegram_user_id: str
    router_stack_name: str
    agentcore_stack_name: str
    schedule_group: str
    cron_lambda_name: str
    log_group: str
    identity_table: str


def load_config() -> E2EConfig:
    """Build config from AWS resources. Raises on missing critical values."""
    region = _resolve_region()
    env_suffix = _resolve_env_suffix()
    router_stack_name = _with_suffix("OpenClawRouter", env_suffix)
    agentcore_stack_name = _with_suffix("OpenClawAgentCore", env_suffix)
    webhook_secret_id = _with_suffix("openclaw/webhook-secret", env_suffix)
    cf = boto3.client("cloudformation", region_name=region)
    sm = boto3.client("secretsmanager", region_name=region)

    # API URL from CloudFormation
    try:
        resp = cf.describe_stacks(StackName=router_stack_name)
        outputs = {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}
        api_url = outputs.get("ApiUrl", "")
    except (ClientError, IndexError, KeyError) as e:
        raise RuntimeError(f"Cannot read {router_stack_name} stack outputs: {e}") from e

    if not api_url:
        raise RuntimeError(f"ApiUrl output not found in {router_stack_name} stack")

    # Webhook secret from Secrets Manager
    try:
        resp = sm.get_secret_value(SecretId=webhook_secret_id)
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
        router_stack_name=router_stack_name,
        agentcore_stack_name=agentcore_stack_name,
        schedule_group=_with_suffix("openclaw-cron", env_suffix),
        cron_lambda_name=_with_suffix("openclaw-cron-executor", env_suffix),
        log_group=_with_suffix("/openclaw/lambda/router", env_suffix),
        identity_table=_with_suffix("openclaw-identity", env_suffix),
    )
