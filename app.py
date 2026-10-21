#!/usr/bin/env python3
"""OpenClaw on AgentCore Runtime — CDK Application entry point.

Architecture: Per-user AgentCore Runtime sessions with webhook-based
channel ingestion via Router Lambda. No keepalive needed — sessions
idle-terminate naturally.

Hybrid deployment model:
  Phase 1 (CDK): VPC, Security, AgentCore-base (Role/SG/S3), Observability
  Phase 2 (SDK controller): AgentCore Runtime, Endpoint, ECR, Docker build
  Phase 3 (CDK): Router, Cron, TokenMonitoring and AgentCore Runtime Invoke IAM
"""

import os

import aws_cdk as cdk
import cdk_nag

from stacks import DeploymentNamer
from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.router_stack import RouterStack
from stacks.guardrails_stack import GuardrailsStack
from stacks.gateway_stack import GatewayStack
from stacks.cron_stack import CronStack
from stacks.observability_stack import ObservabilityStack
from stacks.token_monitoring_stack import TokenMonitoringStack
from stacks.runtime_config import load_runtime_manifest, runtime_target_from_manifest

app = cdk.App()
# Optional environment suffix (environment_suffix context / OPENCLAW_ENV_SUFFIX).
# Empty by default, which leaves every stack and resource name unchanged.
namer = DeploymentNamer.from_scope(app)

env = cdk.Environment(
    account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
)

# --- Foundation ---
vpc_stack = VpcStack(app, namer.stack("OpenClawVpc"), env=env)

security_stack = SecurityStack(app, namer.stack("OpenClawSecurity"), env=env)

# --- Guardrails (Bedrock content filtering — opt-in via enable_guardrails) ---
guardrails_stack = GuardrailsStack(
    app,
    namer.stack("OpenClawGuardrails"),
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- AgentCore Gateway MCP tools (prototype, opt-in via enable_gateway) ---
# Only instantiated when the flag is on so that, with the default (false),
# the synthesized templates of every existing stack are unchanged.
gateway_stack = None
if app.node.try_get_context("enable_gateway") is True:
    gateway_stack = GatewayStack(
        app,
        namer.stack("OpenClawGateway"),
        cognito_issuer_url=security_stack.cognito_issuer_url,
        cognito_client_id=security_stack.user_pool_client_id,
        cmk_arn=security_stack.cmk.key_arn,
        env=env,
    )

# --- AgentCore base resources (Role, SG, S3) ---
# AgentCore Runtime and its endpoints are owned by the SDK controller.
agentcore_stack = AgentCoreStack(
    app,
    namer.stack("OpenClawAgentCore"),
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    cognito_issuer_url=security_stack.cognito_issuer_url,
    cognito_client_id=security_stack.user_pool_client_id,
    cognito_user_pool_id=security_stack.user_pool_id,
    cognito_password_secret_name=security_stack.cognito_password_secret.secret_name,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    # Guardrail wiring (dropped by a rebase in 923189c / #30, restored in #100).
    # GuardrailsStack exports None when enable_guardrails=false, so the
    # AgentCoreStack `if guardrail_id:` branch (bedrock:ApplyGuardrail) is
    # skipped cleanly in that case.
    guardrail_id=guardrails_stack.guardrail_id or "",
    guardrail_version=guardrails_stack.guardrail_version or "",
    env=env,
)

runtime_manifest = load_runtime_manifest(
    app.node.try_get_context("runtime_manifest"), env.region, env.account,
)
runtime_target = runtime_target_from_manifest(runtime_manifest)
runtime_arn = runtime_manifest["agentRuntimeArn"] if runtime_manifest else ""
runtime_endpoint_name = runtime_manifest["endpointName"] if runtime_manifest else ""

WHATSAPP_SECRET_NAME = namer.name("openclaw/channels/whatsapp")
DISCORD_SECRET_NAME = namer.name("openclaw/channels/discord")
LINE_SECRET_NAME = namer.name("openclaw/channels/line")

# --- Router (Lambda + API Gateway HTTP API for Telegram/Slack webhooks) ---
router_stack = RouterStack(
    app,
    namer.stack("OpenClawRouter"),
    runtime_arn=runtime_arn,
    runtime_endpoint_name=runtime_endpoint_name,
    runtime_target=runtime_target,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_token_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    webhook_secret_name=security_stack.webhook_secret.secret_name,
    # Fixed secret name (created by OpenClawSecurity) rather than a cross-stack
    # reference, so adding the channel needs no new export and no redeploy order.
    whatsapp_token_secret_name=WHATSAPP_SECRET_NAME,
    discord_token_secret_name=DISCORD_SECRET_NAME,
    line_token_secret_name=LINE_SECRET_NAME,
    cmk_arn=security_stack.cmk.key_arn,
    user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
    user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
    env=env,
)

# --- Cron (EventBridge Scheduler + Lambda executor) ---
# Use deterministic string ARNs for identity table to avoid cyclic dependency
# (AgentCore <- Router already exists; CronStack adds policies to AgentCore role)
_region = env.region or os.environ.get("CDK_DEFAULT_REGION", "")
_account = env.account or os.environ.get("CDK_DEFAULT_ACCOUNT", "")
_identity_table_name = namer.name("openclaw-identity")
_identity_table_arn = f"arn:aws:dynamodb:{_region}:{_account}:table/{_identity_table_name}"

cron_stack = CronStack(
    app,
    namer.stack("OpenClawCron"),
    runtime_arn=runtime_arn,
    runtime_endpoint_name=runtime_endpoint_name,
    identity_table_name=_identity_table_name,
    identity_table_arn=_identity_table_arn,
    telegram_token_secret_name=security_stack.channel_secrets["telegram"].secret_name,
    slack_token_secret_name=security_stack.channel_secrets["slack"].secret_name,
    feishu_token_secret_name=security_stack.channel_secrets["feishu"].secret_name,
    whatsapp_token_secret_name=WHATSAPP_SECRET_NAME,
    line_token_secret_name=LINE_SECRET_NAME,
    cmk_arn=security_stack.cmk.key_arn,
    agentcore_execution_role=agentcore_stack.execution_role,
    user_files_bucket_name=agentcore_stack.user_files_bucket.bucket_name,
    user_files_bucket_arn=agentcore_stack.user_files_bucket.bucket_arn,
    env=env,
)

# --- Observability (dashboards + alarms) ---
observability_stack = ObservabilityStack(
    app,
    namer.stack("OpenClawObservability"),
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)

# --- Token Monitoring ---
token_monitoring_stack = TokenMonitoringStack(
    app,
    namer.stack("OpenClawTokenMonitoring"),
    invocation_log_group=observability_stack.invocation_log_group,
    alarm_topic=observability_stack.alarm_topic,
    cmk_arn=security_stack.cmk.key_arn,
    env=env,
)
# Its scheduled-task alarms filter /openclaw/lambda/cron, which OpenClawCron creates.
token_monitoring_stack.add_dependency(cron_stack)

# --- cdk-nag security checks ---
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

app.synth()
