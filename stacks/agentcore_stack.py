"""AgentCore Stack — IAM, S3, Runtime, Endpoint, and optional Browser resources."""

import os

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    Stack,
    RemovalPolicy,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
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
        private_subnets: list[ec2.ISubnet],
        private_subnet_ids: list[str],
        cognito_issuer_url: str,
        cognito_client_id: str,
        cognito_user_pool_id: str,
        cognito_password_secret_name: str,
        cognito_password_secret_arn: str,
        gateway_token_secret_name: str,
        gateway_token_secret_arn: str,
        telegram_token_secret_name: str,
        telegram_token_secret_arn: str,
        guardrail_id: str = "",
        guardrail_version: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        namer = DeploymentNamer.from_scope(self)
        suffix = namer.suffix
        is_dev = suffix == "dev"
        region = Stack.of(self).region
        account = Stack.of(self).account
        execution_role_name = namer.name(f"openclaw-agentcore-execution-role-{region}")
        cron_schedule_group_name = namer.name("openclaw-cron")
        cron_lambda_name = namer.name("openclaw-cron-executor")
        scheduler_role_name = namer.name(f"openclaw-cron-scheduler-role-{region}")
        identity_table_name = namer.name("openclaw-identity")
        secret_resource_arns = [
            gateway_token_secret_arn,
            cognito_password_secret_arn,
            telegram_token_secret_arn,
        ]

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
        # Deterministic ARN avoids CDK circular dependency when the role
        # references itself in its trust policy and inline policy.
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
                    "arn:aws:bedrock:*::foundation-model/*",
                    f"arn:aws:bedrock:{region}:{account}:inference-profile/*",
                    "arn:aws:bedrock:*::inference-profile/*",
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

        # Secrets Manager — scoped to the 2 secrets the container actually needs
        # (gateway token for WebSocket auth, Cognito secret for identity derivation)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=secret_resource_arns,
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

        # ECR pull (CDK Docker assets publish the runtime image to ECR)
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
        user_files_cmk = kms.Key.from_key_arn(self, "UserFilesCmk", cmk_arn)
        bucket_name = namer.name(f"openclaw-user-files-{account}-{region}")
        reuse_existing_bucket = str(
            self.node.try_get_context("reuse_existing_user_files_bucket") or ""
        ).lower() in {"1", "true", "yes", "on"}

        if reuse_existing_bucket:
            self.user_files_bucket = s3.Bucket.from_bucket_name(
                self, "UserFilesBucket", bucket_name
            )
        else:
            self.user_files_bucket = s3.Bucket(
                self,
                "UserFilesBucket",
                bucket_name=bucket_name,
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

        # S3 per-user file storage permissions
        self.user_files_bucket.grant_read_write(self.execution_role)

        # --- AgentCore Browser (optional) -------------------------------------
        enable_browser = str(self.node.try_get_context("enable_browser") or "false").lower() == "true"
        self.browser = None
        self.browser_id = ""
        if enable_browser:
            if region not in BROWSER_SUPPORTED_REGIONS:
                Annotations.of(self).add_warning(
                    f"enable_browser=true but region {region} is not in "
                    f"BROWSER_SUPPORTED_REGIONS {BROWSER_SUPPORTED_REGIONS}. "
                    f"Browser resource will NOT be deployed."
                )
            else:
                browser_supported_azs = self.node.try_get_context("browser_supported_availability_zones") or []
                browser_subnet_ids = private_subnet_ids
                if browser_supported_azs:
                    browser_subnet_ids = [
                        subnet.subnet_id
                        for subnet in private_subnets
                        if subnet.availability_zone in browser_supported_azs
                    ]

                if not is_dev and not browser_subnet_ids:
                    Annotations.of(self).add_warning(
                        "enable_browser=true but none of the VPC private subnets are in "
                        f"browser_supported_availability_zones={browser_supported_azs}. "
                        "Browser resource will NOT be deployed."
                    )
                    self.browser_id = ""
                else:
                    browser_network_config = {
                        "network_mode": "PUBLIC",
                    }
                    if not is_dev:
                        browser_network_config = {
                            "network_mode": "VPC",
                            "vpc_config": agentcore.CfnBrowserCustom.VpcConfigProperty(
                                subnets=browser_subnet_ids,
                                security_groups=[self.agent_sg.security_group_id],
                            ),
                        }

                    self.browser = agentcore.CfnBrowserCustom(
                        self,
                        "BrowserCustom",
                        name=namer.runtime_name("openclaw_browser"),
                        network_configuration=agentcore.CfnBrowserCustom.BrowserNetworkConfigurationProperty(
                            **browser_network_config
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

                    self.browser_id = self.browser.attr_browser_id

        # --- AgentCore Runtime + Endpoint -------------------------------------
        default_model_id = self.node.try_get_context("default_model_id") or "global.anthropic.claude-sonnet-4-6"
        subagent_model_id = self.node.try_get_context("subagent_model_id") or ""
        workspace_sync_interval_seconds = int(
            self.node.try_get_context("workspace_sync_interval_seconds") or "300"
        )
        session_idle_timeout = int(
            self.node.try_get_context("session_idle_timeout") or "1800"
        )
        suffix = namer.suffix
        # Minimum allowed by Bedrock AgentCore is 60 seconds.
        # Dev: 1 minute (60s) | Prod: 30 minutes (1800s)
        default_max_lifetime = "60" if suffix == "dev" else "1800"
        session_max_lifetime = int(
            self.node.try_get_context("session_max_lifetime") or default_max_lifetime
        )
        # idle timeout must be <= max lifetime
        session_idle_timeout = min(session_idle_timeout, session_max_lifetime)
        cron_lead_time_minutes = int(
            self.node.try_get_context("cron_lead_time_minutes") or "5"
        )

        runtime_env = {
            "AWS_REGION": region,
            "BEDROCK_MODEL_ID": default_model_id,
            "GATEWAY_TOKEN_SECRET_ID": gateway_token_secret_name,
            "COGNITO_USER_POOL_ID": cognito_user_pool_id,
            "COGNITO_CLIENT_ID": cognito_client_id,
            "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
            "S3_USER_FILES_BUCKET": self.user_files_bucket.bucket_name,
            "WORKSPACE_SYNC_INTERVAL_MS": str(workspace_sync_interval_seconds * 1000),
            "EXECUTION_ROLE_ARN": self.execution_role.role_arn,
            "CMK_ARN": cmk_arn,
            "EVENTBRIDGE_SCHEDULE_GROUP": cron_schedule_group_name,
            "CRON_LAMBDA_ARN": f"arn:aws:lambda:{region}:{account}:function:{cron_lambda_name}",
            "EVENTBRIDGE_ROLE_ARN": f"arn:aws:iam::{account}:role/{scheduler_role_name}",
            "IDENTITY_TABLE_NAME": identity_table_name,
            "CRON_LEAD_TIME_MINUTES": str(cron_lead_time_minutes),
            "SUBAGENT_BEDROCK_MODEL_ID": subagent_model_id,
            "TELEGRAM_CHANNEL_SECRET_ID": telegram_token_secret_name,
        }
        if self.browser_id:
            runtime_env["BROWSER_IDENTIFIER"] = self.browser_id
        if guardrail_id:
            runtime_env["BEDROCK_GUARDRAIL_ID"] = guardrail_id
            runtime_env["BEDROCK_GUARDRAIL_VERSION"] = guardrail_version or "DRAFT"

        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        image_version = str(self.node.try_get_context("image_version") or "1")
        runtime_artifact = agentcore.AgentRuntimeArtifact.from_asset(
            project_root,
            file="bridge/Dockerfile.cdk",
            platform=ecr_assets.Platform.LINUX_ARM64,
            display_name=namer.name("openclaw-agentcore-runtime"),
            extra_hash=image_version,
        )

        runtime_network_config = agentcore.RuntimeNetworkConfiguration.using_public_network()
        if not is_dev:
            runtime_network_config = agentcore.RuntimeNetworkConfiguration.using_vpc(
                self,
                vpc=vpc,
                security_groups=[self.agent_sg],
                vpc_subnets=ec2.SubnetSelection(subnets=private_subnets),
            )

        self.runtime = agentcore.Runtime(
            self,
            "Runtime",
            runtime_name=namer.runtime_name("openclaw_agent_v2"),
            agent_runtime_artifact=runtime_artifact,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_iam(),
            environment_variables=runtime_env,
            execution_role=self.execution_role,
            lifecycle_configuration=agentcore.LifecycleConfiguration(
                idle_runtime_session_timeout=Duration.seconds(session_idle_timeout),
                max_lifetime=Duration.seconds(session_max_lifetime),
            ),
            network_configuration=runtime_network_config,
        )

        runtime_cfn = self.runtime.node.default_child
        if isinstance(runtime_cfn, agentcore.CfnRuntime):
            runtime_cfn.filesystem_configurations = [
                agentcore.CfnRuntime.FilesystemConfigurationProperty(
                    session_storage=agentcore.CfnRuntime.SessionStorageConfigurationProperty(
                        mount_path="/mnt/workspace"
                    )
                )
            ]

        self.runtime_endpoint = self.runtime.add_endpoint(
            namer.runtime_name("openclaw_endpoint"),
            description="Default endpoint for the OpenClaw AgentCore runtime",
        )
        self.runtime_arn = self.runtime.agent_runtime_arn
        self.runtime_endpoint_id = self.runtime_endpoint.endpoint_id

        # --- Outputs ----------------------------------------------------------
        CfnOutput(self, "ExecutionRoleArn", value=self.execution_role.role_arn)
        CfnOutput(self, "RuntimeArn", value=self.runtime.agent_runtime_arn)
        CfnOutput(self, "RuntimeId", value=self.runtime.agent_runtime_id)
        CfnOutput(self, "RuntimeEndpointId", value=self.runtime_endpoint.endpoint_id)
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
                    "Runtime-managed CloudWatch logs and workload identities use "
                    "service-defined wildcard resource patterns. Metrics, X-Ray, and "
                    "Secrets Manager APIs are scoped to project prefix (openclaw/*) or "
                    "do not support resource-level permissions. Cognito scoped to "
                    "specific user pool.",
                    applies_to=[
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        "Resource::arn:aws:bedrock:*::inference-profile/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
                        f"Resource::arn:aws:bedrock-agentcore:{region}:{account}:workload-identity-directory/default/workload-identity/*",
                        *[f"Resource::{secret_arn}" for secret_arn in secret_resource_arns],
                        "Resource::*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*",
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:/openclaw/*:*",
                        # S3 per-user file storage bucket (grant_read_write wildcards)
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                        f"Resource::arn:aws:s3:::{bucket_name}/*",
                        "Resource::<UserFilesBucketCFDFD8C0.Arn>/*",
                        # EventBridge cron scheduling (added by CronStack)
                        f"Resource::arn:aws:scheduler:{region}:{account}:schedule/{cron_schedule_group_name}/*",
                        f"Resource::arn:aws:dynamodb:{region}:{account}:table/{identity_table_name}/index/*",
                        # Per-user API key storage in Secrets Manager (manage_secret tool)
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/user/*",
                        # ECR pull (runtime image is published to ECR by CDK assets)
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
