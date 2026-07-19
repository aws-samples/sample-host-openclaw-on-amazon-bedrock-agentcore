#!/usr/bin/env python3
"""OpenClaw on AgentCore Runtime — CDK Application entry point.

Architecture: Per-user AgentCore Runtime sessions with webhook-based
channel ingestion via Router Lambda. No keepalive needed — sessions
idle-terminate naturally.

Deployment model:
  Foundation (CDK): VPC, security, retained image boundary, observability
  Release (CDK): exact digest-bound Runtime and commit-specific Endpoint
  Consumers (CDK): Router, web control surface, and Cron tombstone
"""

import os
from pathlib import Path
import re
import sys

import aws_cdk as cdk
import cdk_nag

from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.capability_stack import CapabilityStack, STATE_TABLE_NAME
from stacks.router_stack import RouterStack
from stacks.web_stack import WebStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.cron_stack import CronStack
from stacks.scheduler_stack import SchedulerStack
from stacks.observability_stack import ObservabilityStack
from stacks.trusted_lambda_asset import resolve_trusted_lambda_asset_metadata

REQUIRED_REGION = "eu-west-1"
RELEASE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")

repository_root = Path(__file__).resolve().parent
sys.path.insert(0, str(repository_root / "lambda"))
from capabilities.catalog import compile_catalog  # noqa: E402

app = cdk.App()

context_region = app.node.try_get_context("region")
for source, configured_region in (
    ("cdk context region", context_region),
    ("CDK_DEFAULT_REGION", os.environ.get("CDK_DEFAULT_REGION")),
    ("AWS_REGION", os.environ.get("AWS_REGION")),
    ("AWS_DEFAULT_REGION", os.environ.get("AWS_DEFAULT_REGION")),
):
    if configured_region and configured_region != REQUIRED_REGION:
        raise RuntimeError(
            f"{source} must be exactly {REQUIRED_REGION}; got {configured_region}"
        )

env = cdk.Environment(
    account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=REQUIRED_REGION,
)

configured_account = app.node.try_get_context("account") or os.environ.get(
    "CDK_DEFAULT_ACCOUNT"
)
capability_state_table_arn = (
    f"arn:aws:dynamodb:{REQUIRED_REGION}:{configured_account}:table/"
    f"{STATE_TABLE_NAME}"
)
trusted_lambda_asset = resolve_trusted_lambda_asset_metadata(
    repository_root,
    account=configured_account,
    allow_synthetic_source=(
        os.environ.get("PERSONAL_OPERATOR_SYNTH_SOURCE_ASSET") == "1"
    ),
)
capability_release_commit = app.node.try_get_context("capability_release_commit") or ""
if not capability_release_commit and configured_account == "000000000000":
    capability_release_commit = "0" * 40
if RELEASE_COMMIT_PATTERN.fullmatch(capability_release_commit) is None:
    raise RuntimeError(
        "capability_release_commit must bind the exact reviewed Git commit"
    )
_, capability_catalog = compile_catalog(
    capability_release_commit,
    repository_root / "specs" / "capabilities" / "schemas",
)

# --- Foundation ---
vpc_stack = VpcStack(app, "OpenClawVpc", env=env)

security_stack = SecurityStack(app, "OpenClawSecurity", env=env)

# --- Guardrails (Bedrock content filtering — opt-in via enable_guardrails) ---
guardrails_stack = GuardrailsStack(
    app,
    "OpenClawGuardrails",
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- Capability relay and admission gateway (Task 2) ---
# Runtime/endpoint provisioning is deliberately external. Phase 3 accepts only
# one atomic commit-bound runtime context supplied as explicit CDK arguments.
capability_stack = CapabilityStack(
    app,
    "PersonalOperatorCapabilities",
    trusted_code_asset_root=trusted_lambda_asset.path,
    cmk_arn=security_stack.cmk.key_arn,
    release_commit=capability_release_commit,
    catalog_digest=capability_catalog.catalog_digest,
    env=env,
)

# --- AgentCore foundation and optional immutable release resources -----------
# Empty runtime context produces a foundation-only template. Supplying one
# exact commit and image digest adds direct CloudFormation Runtime/Endpoint L1s;
# a complete verified context then binds consumer stacks to that exact version.
agentcore_stack = AgentCoreStack(
    app,
    "OpenClawAgentCore",
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    trusted_endpoint_security_group=vpc_stack.vpce_sg,
    s3_prefix_list_id=vpc_stack.s3_prefix_list_id,
    workspace_capability_secret_name=(
        security_stack.workspace_capability_secret.secret_name
    ),
    capability_gateway_function_arn=capability_stack.gateway_function_arn,
    guardrail_id=guardrails_stack.guardrail_id,
    guardrail_version=guardrails_stack.guardrail_version,
    guardrail_arn=guardrails_stack.guardrail_arn,
    env=env,
)
agentcore_stack.add_dependency(capability_stack)

# --- Router (Lambda + API Gateway HTTP API for Telegram/Slack webhooks) ---
router_stack = RouterStack(
    app,
    "OpenClawRouter",
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_iam_arn=agentcore_stack.runtime_iam_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    runtime_endpoint_name=agentcore_stack.runtime_endpoint_name,
    runtime_binding=agentcore_stack.runtime_binding,
    capability_state_table_name=STATE_TABLE_NAME,
    capability_state_table_arn=capability_state_table_arn,
    capability_release_commit=capability_release_commit,
    capability_catalog_digest=capability_catalog.catalog_digest,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_token_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    webhook_secret_name=security_stack.webhook_secret.secret_name,
    workspace_capability_secret_name=(
        security_stack.workspace_capability_secret.secret_name
    ),
    workspace_broker_role_arn=agentcore_stack.workspace_broker_role_arn,
    workspace_broker_function_name=(
        agentcore_stack.workspace_broker_function_name
    ),
    workspace_session_role_arn=agentcore_stack.workspace_session_role_arn,
    cmk_arn=security_stack.cmk.key_arn,
    user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
    user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
    trusted_code_asset_root=trusted_lambda_asset.path,
    trusted_code_asset_hash=trusted_lambda_asset.asset_hash,
    env=env,
)

# --- Trusted read-only scheduler (Task 7) ---
# A governed control table + ingress Lambda whose EventBridge Scheduler role
# can only invoke the ingress function and whose ingress role can only
# strong-read control state and enqueue a read-only occurrence into the
# existing per-user FIFO. No connector/browser/provider authority exists on
# either role.
scheduler_stack = SchedulerStack(
    app,
    "PersonalOperatorScheduler",
    trusted_code_asset_root=trusted_lambda_asset.path,
    cmk_arn=security_stack.cmk.key_arn,
    update_queue_arn=router_stack.update_queue.queue_arn,
    update_queue_url=router_stack.update_queue.queue_url,
    catalog_digest=capability_catalog.catalog_digest,
    capability_state_table_name=STATE_TABLE_NAME,
    capability_state_table_arn=capability_state_table_arn,
    env=env,
)
scheduler_stack.add_dependency(router_stack)

# --- Trusted consumer web control surface ---
# The stack creates its composite control table and consumes domain-separated
# secrets owned by SecurityStack. Provider credentials remain invalid,
# fail-closed placeholders until an explicit staging preflight replaces them. A
# CloudFront-scope WAF ACL, when configured, must
# already exist in us-east-1; this eu-west-1 application only attaches its ARN.
web_stack = WebStack(
    app,
    "PersonalOperatorWeb",
    cmk_arn=security_stack.cmk.key_arn,
    runtime_state_table=router_stack.runtime_state_table,
    capability_state_table=capability_stack.state_table,
    identity_table=router_stack.identity_table,
    message_ledger_table=router_stack.message_ledger_table,
    user_files_bucket=agentcore_stack.user_files_bucket,
    runtime_arn=agentcore_stack.runtime_arn,
    runtime_iam_arn=agentcore_stack.runtime_iam_arn,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    runtime_endpoint_name=agentcore_stack.runtime_endpoint_name,
    runtime_binding=agentcore_stack.runtime_binding,
    scheduler_control_function_arn=(
        scheduler_stack.control_function.function_arn
    ),
    scheduler_control_table_arn=(
        scheduler_stack.control_table.table_arn
    ),
    trusted_code_asset_root=trusted_lambda_asset.path,
    trusted_code_asset_hash=trusted_lambda_asset.asset_hash,
    web_asset_root=str(repository_root / "web" / "dist"),
    auth_secret=security_stack.web_auth_secret,
    approval_secret=security_stack.approval_signing_secret,
    origin_verification_secret=security_stack.origin_verification_secret,
    google_readonly_oauth_secret=security_stack.google_readonly_oauth_secret,
    google_send_oauth_secret=security_stack.google_send_oauth_secret,
    openai_api_key_secret=security_stack.openai_api_key_secret,
    founder_user_ids=app.node.try_get_context("founder_user_ids") or "",
    gmail_send_connection_id=(
        app.node.try_get_context("gmail_send_connection_id") or ""
    ),
    gmail_send_account_email=(
        app.node.try_get_context("gmail_send_account_email") or ""
    ),
    web_acl_id=app.node.try_get_context("cloudfront_web_acl_arn") or None,
    env=env,
)
web_stack.add_dependency(scheduler_stack)

# Same-ID tombstone deployment removes any legacy Scheduler/Lambda/IAM
# resources from an existing OpenClawCron CloudFormation stack.
cron_stack = CronStack(app, "OpenClawCron", env=env)

# --- Observability (metadata-only dashboards + alarms; no model payloads) ---
observability_stack = ObservabilityStack(
    app,
    "OpenClawObservability",
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- cdk-nag security checks ---
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

app.synth()
