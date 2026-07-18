"""AgentCore Stack — IAM, S3, and Security Group for AgentCore Runtime.

Creates the supporting resources that the AgentCore Runtime needs:
  - Execution Role (Bedrock/log/telemetry plus exact broker invocation)
  - Trusted Credential Broker Role (the sole workspace-role assumer)
  - Workspace Session Role (S3/KMS base authority narrowed by the broker)
  - Security Group (VPC networking for the container)
  - S3 Bucket (per-user file storage and workspace sync)

The Runtime itself (container, endpoint) is deployed separately via the
AgentCore Starter Toolkit (`agentcore deploy`), which handles ECR, Docker
build (CodeBuild), and Runtime/Endpoint lifecycle. The deploy script
passes the execution role ARN, subnet IDs, and security group ID from
this stack to the toolkit.
"""

import re

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    Stack,
    RemovalPolicy,
    Token,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
import cdk_nag
from constructs import Construct

# Regions where AgentCore Browser (CfnBrowserCustom) is confirmed available.
REQUIRED_REGION = "eu-west-1"
BROWSER_SUPPORTED_REGIONS = {REQUIRED_REGION}
BEDROCK_INFERENCE_PROFILE_ID = "eu.anthropic.claude-sonnet-4-6"
BEDROCK_FOUNDATION_MODEL_ID = "anthropic.claude-sonnet-4-6"
BEDROCK_DESTINATION_REGIONS = (
    "eu-central-1",
    "eu-north-1",
    "eu-south-1",
    "eu-south-2",
    "eu-west-1",
    "eu-west-3",
)


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        vpc: ec2.IVpc,
        private_subnet_ids: list[str],
        workspace_capability_secret_name: str,
        guardrail_id: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"AgentCoreStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        if (
            not Token.is_unresolved(workspace_capability_secret_name)
            and workspace_capability_secret_name
            != "personal-operator/workspace-capability"
        ):
            raise ValueError("workspace capability secret name is not canonical")

        # --- Security Group for AgentCore Runtime containers ------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentRuntimeSecurityGroup",
            vpc=vpc,
            description="AgentCore Runtime container security group",
            allow_all_outbound=False,
        )
        self.agent_sg.add_egress_rule(
            peer=ec2.Peer.any_ipv4(),
            connection=ec2.Port.tcp(443),
            description="HTTPS to VPC endpoints and internet (web_fetch/web_search tools)",
        )
        self.agent_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from VPC",
        )

        # --- Execution Role (what the container can do) -----------------------
        execution_role_name = f"openclaw-agentcore-execution-role-{region}"
        # Deterministic ARN lets the workspace role trust this exact role
        # without creating a CloudFormation reference cycle.
        execution_role_arn_str = f"arn:aws:iam::{account}:role/{execution_role_name}"
        self.execution_role = iam.Role(
            self,
            "OpenClawExecutionRole",
            role_name=execution_role_name,
            assumed_by=iam.CompositePrincipal(
                iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                iam.ServicePrincipal("bedrock.amazonaws.com"),
                iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            ),
        )

        # The frozen EU inference profile can route to any of these six EU
        # Regions. Grant its exact source ARN separately, then bind every
        # destination model permission to invocations through that profile.
        inference_profile_arn = (
            f"arn:aws:bedrock:{region}:{account}:inference-profile/"
            f"{BEDROCK_INFERENCE_PROFILE_ID}"
        )
        bedrock_invocation_actions = [
            "bedrock:InvokeModel",
            "bedrock:InvokeModelWithResponseStream",
        ]
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=bedrock_invocation_actions,
                resources=[inference_profile_arn],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=bedrock_invocation_actions,
                resources=[
                    f"arn:aws:bedrock:{destination_region}::foundation-model/"
                    f"{BEDROCK_FOUNDATION_MODEL_ID}"
                    for destination_region in BEDROCK_DESTINATION_REGIONS
                ],
                conditions={
                    "StringLike": {
                        "bedrock:InferenceProfileArn": inference_profile_arn,
                    }
                },
            )
        )

        # Bedrock Guardrails — ApplyGuardrail permission (only when guardrails enabled)
        if guardrail_id:
            self.execution_role.add_to_policy(
                iam.PolicyStatement(
                    actions=["bedrock:ApplyGuardrail"],
                    resources=[
                        f"arn:aws:bedrock:{region}:{account}:guardrail/*",
                    ],
                )
            )

        # The potentially compromised runtime has neither direct workspace nor
        # STS authority. It may invoke only the trusted credential broker, which
        # derives and applies the exact per-user session policy outside AgentCore.
        workspace_session_role_name = f"openclaw-workspace-session-role-{region}"
        workspace_session_role_arn_str = (
            f"arn:aws:iam::{account}:role/{workspace_session_role_name}"
        )
        workspace_broker_function_name = (
            "personal-operator-workspace-credential-broker"
        )
        workspace_broker_function_arn = (
            f"arn:aws:lambda:{region}:{account}:function:"
            f"{workspace_broker_function_name}"
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[workspace_broker_function_arn],
            )
        )

        workspace_broker_role_name = (
            f"personal-operator-workspace-credential-broker-{region}"
        )
        workspace_broker_role_arn_str = (
            f"arn:aws:iam::{account}:role/{workspace_broker_role_name}"
        )
        self.workspace_broker_function_name = workspace_broker_function_name
        self.workspace_broker_role_arn = workspace_broker_role_arn_str
        self.workspace_session_role_arn = workspace_session_role_arn_str
        self.workspace_broker_role = iam.Role(
            self,
            "WorkspaceCredentialBrokerRole",
            role_name=workspace_broker_role_name,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Trusted boundary that validates user/session capabilities before "
                "assuming the workspace base role"
            ),
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[workspace_session_role_arn_str],
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[
                    f"arn:aws:dynamodb:{region}:{account}:"
                    "table/personal-operator-runtime-state"
                ],
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:"
                    f"{workspace_capability_secret_name}-??????"
                ],
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": (
                            f"secretsmanager.{region}.amazonaws.com"
                        ),
                    }
                },
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt", "kms:DescribeKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )
        self.workspace_broker_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    "/personal-operator/lambda/workspace-credential-broker:*"
                ],
            )
        )

        # CloudWatch Logs — scoped to /openclaw/ log group prefix
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/openclaw/*",
                    f"arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*",
                ],
            )
        )

        # CloudWatch Metrics — namespace condition prevents alarm falsification
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["cloudwatch:PutMetricData"],
                resources=["*"],
                conditions={
                    "StringEquals": {
                        "cloudwatch:namespace": "OpenClaw/AgentCore"
                    }
                },
            )
        )

        # X-Ray tracing
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )

        # ECR pull (toolkit creates the repo, but the execution role needs pull access)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:BatchCheckLayerAvailability",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/openclaw-bridge*",
                    f"arn:aws:ecr:{region}:{account}:repository/openclaw_agent*",
                    f"arn:aws:ecr:{region}:{account}:repository/bedrock-agentcore-*",
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # --- S3 Bucket for Per-User File Storage ------------------------------
        user_files_ttl_days = int(
            self.node.try_get_context("user_files_ttl_days") or "365"
        )
        user_files_cmk = kms.Key.from_key_arn(self, "UserFilesCmk", cmk_arn)
        self.user_files_bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=f"openclaw-user-files-{account}-{region}",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=user_files_cmk,
            bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="clean-noncurrent-workspace-versions",
                    noncurrent_version_expiration=Duration.days(
                        user_files_ttl_days
                    ),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
            enforce_ssl=True,
            versioned=True,
        )

        # Workspace access base role. Its trust is limited to the exact broker
        # role. AgentCore cannot assume it, even if runtime code omits a policy.
        self.workspace_session_role = iam.Role(
            self,
            "WorkspaceSessionRole",
            role_name=workspace_session_role_name,
            assumed_by=iam.AccountRootPrincipal().with_conditions(
                {
                    "ArnEquals": {"aws:PrincipalArn": workspace_broker_role_arn_str},
                    "StringLike": {"sts:RoleSessionName": "workspace-*"},
                }
            ),
            description="Base S3/KMS role narrowed by per-user STS session policy",
        )
        self.workspace_session_role.node.add_dependency(self.workspace_broker_role)
        self.workspace_session_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:ListBucket"],
                resources=[self.user_files_bucket.bucket_arn],
            )
        )
        self.workspace_session_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[self.user_files_bucket.arn_for_objects("*")],
            )
        )
        self.workspace_session_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:ViaService": "s3.eu-west-1.amazonaws.com",
                        "kms:CallerAccount": account,
                    }
                },
            )
        )

        # --- Reviewed runtime release identity (read via context) -------------
        # Runtime provisioning is deliberately outside this stack. Dependent
        # stacks are wired only after the release gate supplies one atomic,
        # commit-bound runtime version, immutable image, and dedicated endpoint.
        runtime_source_commit = str(
            self.node.try_get_context("runtime_source_commit") or ""
        )
        runtime_id = str(self.node.try_get_context("runtime_id") or "")
        runtime_endpoint_id = str(
            self.node.try_get_context("runtime_endpoint_id") or ""
        )
        runtime_endpoint_name = str(
            self.node.try_get_context("runtime_endpoint_name") or ""
        )
        runtime_version = str(self.node.try_get_context("runtime_version") or "")
        runtime_arn = str(self.node.try_get_context("runtime_arn") or "")
        runtime_image_uri = str(
            self.node.try_get_context("runtime_image_uri") or ""
        )
        runtime_values = (
            runtime_source_commit,
            runtime_id,
            runtime_endpoint_id,
            runtime_endpoint_name,
            runtime_version,
            runtime_arn,
            runtime_image_uri,
        )
        if not any(runtime_values):
            # Offline/foundation synthesis happens before Starter Toolkit has
            # created a runtime. Dependent stacks are not deployable until the
            # external release gate replaces every placeholder atomically.
            self.runtime_source_commit = "PLACEHOLDER"
            self.runtime_id = "PLACEHOLDER"
            self.runtime_endpoint_id = "PLACEHOLDER"
            self.runtime_endpoint_name = "PLACEHOLDER"
            self.runtime_version = "PLACEHOLDER"
            self.runtime_arn = "PLACEHOLDER"
            self.runtime_image_uri = "PLACEHOLDER"
            self.runtime_iam_arn = "PLACEHOLDER"
        else:
            if not all(runtime_values):
                raise ValueError(
                    "runtime_source_commit, runtime_id, runtime_endpoint_id, "
                    "runtime_endpoint_name, runtime_version, runtime_arn, and "
                    "runtime_image_uri must be set together"
                )
            source_commit_pattern = r"[0-9a-f]{40}"
            runtime_version_pattern = r"[1-9][0-9]{0,4}"
            runtime_id_pattern = r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}"
            runtime_arn_pattern = (
                rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
                rf"{re.escape(account)}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            runtime_image_uri_pattern = (
                rf"{re.escape(account)}\.dkr\.ecr\."
                rf"{re.escape(region)}\.amazonaws\.com/"
                r"[a-z0-9]+(?:[._/-][a-z0-9]+)*"
                r"@sha256:[0-9a-f]{64}"
            )
            if re.fullmatch(source_commit_pattern, runtime_source_commit) is None:
                raise ValueError("runtime_source_commit must be an exact git commit")
            if re.fullmatch(runtime_version_pattern, runtime_version) is None:
                raise ValueError("runtime_version is not canonical")
            if re.fullmatch(runtime_id_pattern, runtime_id) is None:
                raise ValueError(f"runtime_id is not canonical: {runtime_id}")
            if re.fullmatch(runtime_id_pattern, runtime_endpoint_id) is None:
                raise ValueError(
                    f"runtime_endpoint_id is not canonical: {runtime_endpoint_id}"
                )
            expected_endpoint_name = f"release_{runtime_source_commit}"
            if runtime_endpoint_name != expected_endpoint_name:
                raise ValueError(
                    "runtime_endpoint_name must be derived from the exact "
                    "runtime_source_commit"
                )
            if re.fullmatch(runtime_arn_pattern, runtime_arn) is None:
                raise ValueError(
                    "runtime_arn must be the exact AgentCore ARN returned for "
                    f"account {account} in {region}"
                )
            if runtime_arn.rsplit(":", 1)[-1] != runtime_version:
                raise ValueError(
                    "runtime_version must equal the exact runtime ARN version"
                )
            if re.fullmatch(runtime_image_uri_pattern, runtime_image_uri) is None:
                raise ValueError(
                    "runtime_image_uri must be an immutable ECR sha256 digest "
                    f"in account {account} and region {region}"
                )
            self.runtime_source_commit = runtime_source_commit
            self.runtime_id = runtime_id
            self.runtime_endpoint_id = runtime_endpoint_id
            self.runtime_endpoint_name = runtime_endpoint_name
            self.runtime_version = runtime_version
            self.runtime_arn = runtime_arn
            self.runtime_image_uri = runtime_image_uri
            # AgentCore has two distinct ARN namespaces. GetAgentRuntime's
            # agent/<uuid>:<version> ARN is the invocation identity above;
            # IAM authorization uses the documented runtime/<runtime-id>
            # resource grammar. Derive the latter only after validating the
            # canonical runtime ID, account, and region.
            self.runtime_iam_arn = (
                f"arn:aws:bedrock-agentcore:{region}:{account}:"
                f"runtime/{runtime_id}"
            )

        # --- AgentCore Browser (optional) -------------------------------------
        enable_browser = str(self.node.try_get_context("enable_browser") or "false").lower() == "true"
        self.browser = None
        if enable_browser:
            if region not in BROWSER_SUPPORTED_REGIONS:
                Annotations.of(self).add_warning(
                    f"enable_browser=true but region {region} is not in "
                    f"BROWSER_SUPPORTED_REGIONS {BROWSER_SUPPORTED_REGIONS}. "
                    f"Browser resource will NOT be deployed."
                )
            else:
                self.browser = agentcore.CfnBrowserCustom(
                    self,
                    "BrowserCustom",
                    name="openclaw_browser",
                    network_configuration=agentcore.CfnBrowserCustom.BrowserNetworkConfigurationProperty(
                        network_mode="VPC",
                        vpc_config=agentcore.CfnBrowserCustom.VpcConfigProperty(
                            subnets=private_subnet_ids,
                            security_groups=[self.agent_sg.security_group_id],
                        ),
                    ),
                    execution_role_arn=self.execution_role.role_arn,
                    recording_config=agentcore.CfnBrowserCustom.RecordingConfigProperty(
                        enabled=False,
                    ),
                    description="AgentCore Browser for OpenClaw (per-user browsing sessions)",
                )

                self.execution_role.add_to_policy(
                    iam.PolicyStatement(
                        actions=[
                            "bedrock-agentcore:StartBrowserSession",
                            "bedrock-agentcore:StopBrowserSession",
                            "bedrock-agentcore:GetBrowserSession",
                            "bedrock-agentcore:UpdateBrowserStream",
                            "bedrock-agentcore:ConnectBrowserAutomationStream",
                        ],
                        resources=[self.browser.attr_browser_arn],
                    )
                )

                # In hybrid deploy mode, Runtime is managed by Starter Toolkit.
                # Browser identifier is passed via --env BROWSER_IDENTIFIER=<id>
                # during `agentcore deploy`. Export it for the deploy script.
                self.browser_id = self.browser.attr_browser_id

        # --- Outputs ----------------------------------------------------------
        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(
            self,
            "WorkspaceCredentialBrokerRoleArn",
            value=workspace_broker_role_arn_str,
        )
        CfnOutput(
            self,
            "WorkspaceCredentialBrokerFunctionName",
            value=workspace_broker_function_name,
        )
        CfnOutput(
            self,
            "WorkspaceSessionRoleArn",
            value=workspace_session_role_arn_str,
        )
        CfnOutput(self, "SecurityGroupId", value=self.agent_sg.security_group_id)
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(private_subnet_ids),
        )
        if self.browser:
            CfnOutput(
                self,
                "BrowserIdentifier",
                value=self.browser.attr_browser_id,
                description="AgentCore Browser identifier",
            )

        # --- cdk-nag suppressions ---------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Log streams, ECR repositories, and telemetry APIs require "
                    "bounded wildcard resources or do not support resource-level "
                    "permissions.",
                    applies_to=[
                        "Resource::*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*",
                        # ECR pull (toolkit-managed repos — Starter Toolkit uses bedrock-agentcore- prefix)
                        f"Resource::arn:aws:ecr:{region}:{account}:repository/openclaw-bridge*",
                        f"Resource::arn:aws:ecr:{region}:{account}:repository/openclaw_agent*",
                        f"Resource::arn:aws:ecr:{region}:{account}:repository/bedrock-agentcore-*",
                        # Bedrock Guardrails (wildcard for guardrail version changes)
                        f"Resource::arn:aws:bedrock:{region}:{account}:guardrail/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.workspace_session_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="The workspace base role is limited to this bucket; the "
                    "required per-user STS session policy further restricts the "
                    "object wildcard to one canonical namespace.",
                    applies_to=[
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.workspace_broker_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Secrets Manager appends an unknown six-character suffix "
                        "to the otherwise exact capability-secret name; the log "
                        "stream wildcard is confined to one exact broker log group."
                    ),
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.user_files_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logging not required for user file storage — "
                    "CloudTrail S3 data events provide sufficient audit trail.",
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.agent_sg,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-EC23",
                    reason="Ingress uses VPC CIDR; not open to 0.0.0.0/0.",
                ),
                cdk_nag.NagPackSuppression(
                    id="CdkNagValidationFailure",
                    reason="Security group rule uses Fn::GetAtt for VPC CIDR which "
                    "cannot be validated at synth time.",
                ),
            ],
        )
