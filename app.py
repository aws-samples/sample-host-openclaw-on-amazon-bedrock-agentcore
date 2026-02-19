#!/usr/bin/env python3
"""OpenClaw on AgentCore — CDK Application entry point."""

import aws_cdk as cdk
import cdk_nag
import boto3

from stacks import cross_region_model_id
from stacks.vpc_stack import VpcStack
from stacks.security_stack import SecurityStack
from stacks.agentcore_stack import AgentCoreStack
from stacks.fargate_stack import FargateStack
from stacks.edge_stack import EdgeStack
from stacks.observability_stack import ObservabilityStack
from stacks.token_monitoring_stack import TokenMonitoringStack

app = cdk.App()

_account = app.node.try_get_context("account")
_region = app.node.try_get_context("region")
_base_model_id = app.node.try_get_context("default_model_id") or "anthropic.claude-sonnet-4-6"

# Validate that placeholders have been replaced
if not _account or _account == "YOUR_AWS_ACCOUNT_ID":
    raise SystemExit("ERROR: Set 'account' in cdk.json to your AWS account ID")
if not _region or _region == "YOUR_REGION":
    raise SystemExit("ERROR: Set 'region' in cdk.json to your target AWS region")

# Resolve cross-region model ID (e.g. "anthropic.claude-sonnet-4-6" -> "ap.anthropic.claude-sonnet-4-6")
_default_model_id = cross_region_model_id(_region, _base_model_id)

env = cdk.Environment(
    account=_account,
    region=_region,
)

# --- Look up CloudFront origin-facing managed prefix list for the target region ---
_cf_prefix_list_id = ""
try:
    _ec2 = boto3.client("ec2", region_name=_region)
    _resp = _ec2.describe_managed_prefix_lists(
        Filters=[{"Name": "prefix-list-name", "Values": ["com.amazonaws.global.cloudfront.origin-facing"]}]
    )
    _cf_prefix_list_id = _resp["PrefixLists"][0]["PrefixListId"]
except Exception:
    pass  # Lookup failed (no creds or region) — will cause synth error if empty

# --- Foundation ---
vpc_stack = VpcStack(app, "OpenClawVpc", env=env)

security_stack = SecurityStack(app, "OpenClawSecurity", env=env)

# --- AgentCore ---
agentcore_stack = AgentCoreStack(
    app,
    "OpenClawAgentCore",
    cmk_arn=security_stack.cmk.key_arn,
    vpc=vpc_stack.vpc,
    private_subnet_ids=[s.subnet_id for s in vpc_stack.vpc.private_subnets],
    cognito_issuer_url=security_stack.cognito_issuer_url,
    cognito_client_id=security_stack.user_pool_client_id,
    default_model_id=_default_model_id,
    env=env,
)


# --- Fargate ---
fargate_stack = FargateStack(
    app,
    "OpenClawFargate",
    vpc=vpc_stack.vpc,
    gateway_token_secret_name=security_stack.gateway_token_secret.secret_name,
    cmk_arn=security_stack.cmk.key_arn,
    runtime_id=agentcore_stack.runtime_id,
    runtime_endpoint_id=agentcore_stack.runtime_endpoint_id,
    memory_id=agentcore_stack.memory_id,
    cognito_user_pool_id=security_stack.user_pool_id,
    cognito_client_id=security_stack.user_pool_client_id,
    cognito_password_secret_name=security_stack.cognito_password_secret.secret_name,
    cloudfront_prefix_list_id=_cf_prefix_list_id,
    default_model_id=_default_model_id,
    env=env,
)
# Dependencies are inferred via cross-stack references (vpc, fargate_sg, secrets, cmk)

# --- Read gateway token for CloudFront Function validation ---
_gateway_token = ""
try:
    _sm = boto3.client(
        "secretsmanager",
        region_name=_region,
    )
    _gateway_token = _sm.get_secret_value(SecretId="openclaw/gateway-token")[
        "SecretString"
    ]
except Exception:
    pass  # Token unavailable (first deploy or no creds) — falls back to presence-only check

# --- Edge (CloudFront + WAF) ---
edge_stack = EdgeStack(
    app,
    "OpenClawEdge",
    alb=fargate_stack.public_alb,
    gateway_token=_gateway_token,
    env=env,
)


# --- Observability ---
observability_stack = ObservabilityStack(
    app,
    "OpenClawObservability",
    fargate_service=fargate_stack.service,
    cluster_name=fargate_stack.cluster.cluster_name,
    service_name=fargate_stack.service.service_name,
    env=env,
)


# --- Token Monitoring ---
token_monitoring_stack = TokenMonitoringStack(
    app,
    "OpenClawTokenMonitoring",
    invocation_log_group=observability_stack.invocation_log_group,
    alarm_topic=observability_stack.alarm_topic,
    env=env,
)


# --- cdk-nag security checks ---
cdk.Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(verbose=True))

app.synth()
