"""AgentCore Stack — IAM, S3, and Security Group for AgentCore Runtime.

Creates the supporting resources that the AgentCore Runtime needs:
  - Execution Role (Bedrock/log/telemetry plus exact workspace-role assumption)
  - Workspace Session Role (S3/KMS base authority narrowed per user by STS)
  - Security Group (VPC networking for the container)
  - S3 Bucket (per-user file storage and workspace sync)

The Runtime itself (container, endpoint) is deployed separately via the
AgentCore Starter Toolkit (`agentcore deploy`), which handles ECR, Docker
build (CodeBuild), and Runtime/Endpoint lifecycle. The deploy script
passes the execution role ARN, subnet IDs, and security group ID from
this stack to the toolkit.
"""

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    Stack,
    RemovalPolicy,
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


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        vpc: ec2.IVpc,
        private_subnet_ids: list[str],
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

        # Bedrock model invocation
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                ],
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

        # The execution role has no direct workspace access. It can only assume
        # the dedicated base role, after which the bridge supplies a narrower
        # per-user inline session policy.
        workspace_session_role_name = f"openclaw-workspace-session-role-{region}"
        workspace_session_role_arn_str = (
            f"arn:aws:iam::{account}:role/{workspace_session_role_name}"
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[workspace_session_role_arn_str],
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
                        "cloudwatch:namespace": [
                            "OpenClaw/AgentCore",
                            "OpenClaw/TokenUsage",
                        ]
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
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-user-files",
                    expiration=Duration.days(user_files_ttl_days),
                ),
            ],
            enforce_ssl=True,
            versioned=True,
        )

        # Workspace access base role. Its trust is limited to the exact runtime
        # execution role, while a per-user AssumeRole session policy provides
        # the namespace boundary.
        self.workspace_session_role = iam.Role(
            self,
            "WorkspaceSessionRole",
            role_name=workspace_session_role_name,
            assumed_by=iam.AccountRootPrincipal().with_conditions(
                {
                    "ArnEquals": {"aws:PrincipalArn": execution_role_arn_str},
                    "StringLike": {"sts:RoleSessionName": "workspace-*"},
                }
            ),
            description="Base S3/KMS role narrowed by per-user STS session policy",
        )
        self.workspace_session_role.node.add_dependency(self.execution_role)
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

        # --- Runtime info (from Starter Toolkit, read via context) ------------
        # Runtime/Endpoint/ECR managed by Starter Toolkit (`agentcore deploy`),
        # not CDK. These are populated by the deploy script after `agentcore deploy`
        # and passed to the dependent Router stack.
        runtime_id = self.node.try_get_context("runtime_id") or "PLACEHOLDER"
        runtime_endpoint_id = self.node.try_get_context("runtime_endpoint_id") or "PLACEHOLDER"
        self.runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
        self.runtime_endpoint_id = runtime_endpoint_id

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
                    reason="Bedrock model IDs, log streams, ECR repositories, and "
                    "telemetry APIs require bounded wildcard resources or do not "
                    "support resource-level permissions.",
                    applies_to=[
                        f"Resource::arn:aws:bedrock:{region}::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
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
