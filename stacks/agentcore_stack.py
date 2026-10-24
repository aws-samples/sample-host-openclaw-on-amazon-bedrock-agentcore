"""AgentCore Stack — IAM, S3, and Security Group for AgentCore Runtime.

Creates the supporting resources that the AgentCore Runtime needs:
  - Execution Role (with all IAM policies for Bedrock, S3, Secrets Manager, etc.)
  - Security Group (VPC networking for the container)
  - S3 Bucket (per-user file storage and workspace sync)

AgentCore Runtime (container, immutable endpoint) is deployed separately
by the pinned V2 SDK controller. The deploy script builds the ARM64 image
or accepts a prebuilt image, then passes the execution role ARN, subnet IDs,
and security group ID from this stack to the controller.
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

from stacks import DeploymentNamer

# Regions where AgentCore Browser (CfnBrowserCustom) is confirmed available.
BROWSER_SUPPORTED_REGIONS = {"us-east-1", "us-west-2", "eu-west-1", "ap-southeast-1", "ap-southeast-2"}


class AgentCoreStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        vpc: ec2.IVpc,
        private_subnet_ids: list[str],
        cognito_issuer_url: str,
        cognito_client_id: str,
        cognito_user_pool_id: str,
        cognito_password_secret_name: str,
        gateway_token_secret_name: str,
        guardrail_id: str = "",
        guardrail_version: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account

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
        namer = DeploymentNamer.from_scope(self)
        execution_role_name = namer.name(f"openclaw-agentcore-execution-role-{region}")
        # Secret names of this environment (SecurityStack applies the same suffix).
        gateway_token_secret = namer.name("openclaw/gateway-token")
        cognito_password_secret = namer.name("openclaw/cognito-password-secret")
        telegram_channel_secret = namer.name("openclaw/channels/telegram")
        # Names created by CronStack/RouterStack; referenced in the nag suppressions.
        cron_schedule_group_name = namer.name("openclaw-cron")
        identity_table_name = namer.name("openclaw-identity")
        # Deterministic ARN avoids CDK circular dependency when the role
        # references itself in its trust policy and inline policy.
        execution_role_arn_str = f"arn:aws:iam::{account}:role/{execution_role_name}"
        # Trust policy: AgentCore Runtime (bedrock-agentcore) assumes this role.
        # Confused-deputy guards (aws:SourceAccount / aws:SourceArn) match the
        # pattern used by the Gateway role (stacks/gateway_stack.py) and the
        # AgentCore Runtime trust-policy recommendation. The previously trusted
        # ecs-tasks.amazonaws.com and bedrock.amazonaws.com principals were
        # dropped: no deployment path in this stack passes the role to those
        # services, and leaving them unconditioned trusts the services from
        # any account.
        self.execution_role = iam.Role(
            self,
            "OpenClawExecutionRole",
            role_name=execution_role_name,
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/*"
                    },
                },
            ),
        )

        # Bedrock model invocation, including Global profiles and fallback models.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                    "arn:aws:bedrock:*::inference-profile/*",
                ],
            )
        )
        # AWS Marketplace permissions — required so Bedrock can complete the
        # foundation-model access / auto-subscription check on first invocation.
        # aws-marketplace actions do not support resource-level permissions, so
        # "*" is the only valid resource (see cdk-nag suppression below).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "aws-marketplace:ViewSubscriptions",
                    "aws-marketplace:Subscribe",
                ],
                resources=["*"],
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

        # Secrets Manager — scoped to the 2 secrets the container actually needs
        # (gateway token for WebSocket auth, Cognito secret for identity derivation)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:{gateway_token_secret}-*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:{cognito_password_secret}-*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:{telegram_channel_secret}-*",
                ],
            )
        )
        # Secrets Manager — per-user API key storage (manage_secret tool).
        # Session policy further restricts to openclaw/user/{namespace}/* per user.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:CreateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:TagResource",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/user/*",
                ],
            )
        )
        # Secrets Manager — per-user Cognito passwords (bridge/cognito-token.js).
        # Kept outside openclaw/user/* so the agent's scoped session policy,
        # which only covers openclaw/user/{namespace}/*, never reaches them.
        # The pool id in the path separates deployments sharing an account.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:CreateSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/cognito-user/{cognito_user_pool_id}/*",
                ],
            )
        )
        # ListSecrets does not support resource-level restrictions (AWS API limitation).
        # Results filtered by prefix in application code (executeManageSecret).
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:ListSecrets"],
                resources=["*"],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # Cognito admin operations for auto-provisioning identities
        # Scoped to specific user pool
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{region}:{account}:userpool/{cognito_user_pool_id}",
                ],
            )
        )

        # STS self-assume for per-user scoped S3 credentials
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                resources=[execution_role_arn_str],
            )
        )
        # Trust policy: allow self-assume with scoped session name.
        # Uses AccountRootPrincipal (always exists) + ArnEquals condition to
        # avoid the chicken-and-egg problem of referencing a role that doesn't
        # exist yet during creation.
        self.execution_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                actions=["sts:AssumeRole"],
                principals=[iam.AccountRootPrincipal()],
                conditions={
                    "ArnEquals": {
                        "aws:PrincipalArn": execution_role_arn_str,
                    },
                    "StringEquals": {
                        "aws:PrincipalAccount": account,
                    },
                    "StringLike": {
                        "sts:RoleSessionName": "scoped-*"
                    },
                },
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

        # AgentCore Runtime names and IDs are assigned by the SDK controller.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:DescribeLogStreams"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:DescribeLogGroups"],
                resources=[f"arn:aws:logs:{region}:{account}:log-group:*"],
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

        # ECR pull; the deployment controller accepts these repository prefixes.
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetAuthorizationToken",
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
        # Noncurrent versions: the bridge re-saves state often, so every save
        # leaves a noncurrent version behind (prod reached ~915 GB / 7.6 M
        # versions with no expiry). Expire them after N days, but always keep
        # the newest few per key so a recent rollback / CopyObject restore of a
        # previous version (e.g. a pre-2.0 sessions.json) stays possible.
        user_files_noncurrent_days = int(
            self.node.try_get_context("user_files_noncurrent_days") or "30"
        )
        user_files_noncurrent_keep = int(
            self.node.try_get_context("user_files_noncurrent_keep") or "3"
        )
        user_files_cmk = kms.Key.from_key_arn(self, "UserFilesCmk", cmk_arn)
        self.user_files_bucket = s3.Bucket(
            self,
            "UserFilesBucket",
            bucket_name=namer.name(f"openclaw-user-files-{account}-{region}"),
            encryption=s3.BucketEncryption.KMS,
            encryption_key=user_files_cmk,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-old-user-files",
                    expiration=Duration.days(user_files_ttl_days),
                ),
                # Only touches NONCURRENT versions: current objects, delete
                # markers and versioning itself are left alone.
                s3.LifecycleRule(
                    id="expire-noncurrent-versions",
                    noncurrent_version_expiration=Duration.days(
                        user_files_noncurrent_days
                    ),
                    noncurrent_versions_to_retain=user_files_noncurrent_keep,
                ),
                # The bridge uploads large SQLite backups with multipart upload;
                # clean up parts left behind by a container killed mid-upload.
                s3.LifecycleRule(
                    id="abort-incomplete-multipart-uploads",
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                ),
            ],
            enforce_ssl=True,
            versioned=True,
        )

        # S3 per-user file storage permissions
        self.user_files_bucket.grant_read_write(self.execution_role)

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
                    name=namer.runtime_name("openclaw_browser"),
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

                # The deploy script passes this ID to AgentCore Runtime.
                self.browser_id = self.browser.attr_browser_id

        # --- Outputs ----------------------------------------------------------
        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
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
                    reason="Bedrock foundation model ARNs require wildcard for model ID. "
                    "Bedrock guardrail ARNs require wildcard for guardrail version. "
                    "aws-marketplace:ViewSubscriptions/Subscribe (Bedrock model access "
                    "checks) do not support resource-level permissions and require '*'. "
                    "Custom logs and Secrets Manager use the project prefix (openclaw/*). "
                    "AgentCore Runtime logs use the documented service prefix, with log group "
                    "discovery scoped to the current account and region. Metrics and X-Ray "
                    "do not support resource-level permissions. Cognito scoped to specific user pool.",
                    applies_to=[
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        "Resource::arn:aws:bedrock:*::inference-profile/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:{gateway_token_secret}-*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:{cognito_password_secret}-*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:{telegram_channel_secret}-*",
                        "Resource::*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:*",
                        # S3 per-user file storage bucket (grant_read_write wildcards)
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*",
                        # EventBridge cron scheduling (added by CronStack)
                        f"Resource::arn:aws:scheduler:{region}:{account}:schedule/{cron_schedule_group_name}/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                        # Per-user API key storage in Secrets Manager (manage_secret tool)
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/user/*",
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
