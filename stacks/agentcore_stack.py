"""AgentCore Stack — Hosts OpenClaw on AgentCore Runtime.

Deploys the OpenClaw messaging bridge as a container on AgentCore Runtime,
replacing Fargate. The container runs OpenClaw (Telegram/Discord/Slack),
a Bedrock proxy, and an AgentCore contract server on port 8080.
"""

from aws_cdk import (
    CfnOutput,
    Duration,
    Stack,
    RemovalPolicy,
    aws_bedrockagentcore as agentcore,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_kms as kms,
    aws_s3 as s3,
)
import cdk_nag
from constructs import Construct


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
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account

        # --- ECR Repository for OpenClaw bridge image -------------------------
        self.bridge_repo = ecr.Repository(
            self,
            "BridgeRepo",
            repository_name="openclaw-bridge",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,
        )

        # --- Security Group for AgentCore Runtime containers ------------------
        self.agent_sg = ec2.SecurityGroup(
            self,
            "AgentRuntimeSecurityGroup",
            vpc=vpc,
            description="AgentCore Runtime container security group",
            allow_all_outbound=True,
        )
        self.agent_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from VPC",
        )

        # --- Execution Role (what the container can do) -----------------------
        self.execution_role = iam.Role(
            self,
            "OpenClawExecutionRole",
            role_name="openclaw-agentcore-execution-role",
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
                ],
            )
        )

        # Secrets Manager (gateway token, channel tokens, Cognito secret)
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                ],
            )
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # Cognito admin operations for auto-provisioning identities
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{region}:{account}:userpool/*",
                ],
            )
        )

        # CloudWatch Logs + Metrics + X-Ray
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "cloudwatch:PutMetricData",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                ],
                resources=["*"],
            )
        )

        # ECR pull
        self.bridge_repo.grant_pull(self.execution_role)

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
            versioned=False,
        )

        # S3 per-user file storage permissions
        self.user_files_bucket.grant_read_write(self.execution_role)

        # --- AgentCore WorkloadIdentity ---------------------------------------
        self.workload_identity = agentcore.CfnWorkloadIdentity(
            self,
            "WorkloadIdentity",
            name="openclaw_identity",
        )

        # --- Default Bedrock model ID -----------------------------------------
        default_model_id = self.node.try_get_context("default_model_id") or "us.anthropic.claude-sonnet-4-6"

        # --- AgentCore Runtime (hosts OpenClaw container) ---------------------
        self.runtime = agentcore.CfnRuntime(
            self,
            "AgentRuntime",
            agent_runtime_name="openclaw_agent",
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=f"{account}.dkr.ecr.{region}.amazonaws.com/openclaw-bridge:latest"
                )
            ),
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="VPC",
                network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
                    subnets=private_subnet_ids,
                    security_groups=[self.agent_sg.security_group_id],
                ),
            ),
            role_arn=self.execution_role.role_arn,
            environment_variables={
                "AWS_REGION": region,
                "BEDROCK_MODEL_ID": default_model_id,
                "GATEWAY_TOKEN_SECRET_ID": gateway_token_secret_name,
                "COGNITO_USER_POOL_ID": cognito_user_pool_id,
                "COGNITO_CLIENT_ID": cognito_client_id,
                "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
                "S3_USER_FILES_BUCKET": self.user_files_bucket.bucket_name,
                "AGENTCORE_WORKLOAD_IDENTITY_NAME": "openclaw_identity",
                "IMAGE_VERSION": "29",  # bump to force container redeploy
            },
            description="OpenClaw messaging bridge on AgentCore Runtime",
            lifecycle_configuration=agentcore.CfnRuntime.LifecycleConfigurationProperty(
                # Max values to keep the container running as long as possible.
                # HealthyBusy ping status prevents idle termination.
                idle_runtime_session_timeout=28800,  # 8 hours
                max_lifetime=28800,  # 8 hours
            ),
        )

        # --- AgentCore Runtime Endpoint ---------------------------------------
        self.runtime_endpoint = agentcore.CfnRuntimeEndpoint(
            self,
            "AgentRuntimeEndpoint",
            agent_runtime_id=self.runtime.attr_agent_runtime_id,
            name="openclaw_agent_live",
            description="Production endpoint for OpenClaw on AgentCore",
            agent_runtime_version=self.runtime.attr_agent_runtime_version,
        )
        self.runtime_endpoint.add_dependency(self.runtime)

        # --- Outputs ----------------------------------------------------------
        self.runtime_id = self.runtime.attr_agent_runtime_id
        self.runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}"
        self.runtime_endpoint_id = self.runtime_endpoint.attr_id

        CfnOutput(self, "RuntimeId", value=self.runtime.attr_agent_runtime_id)
        CfnOutput(self, "RuntimeEndpointId", value=self.runtime_endpoint.attr_id)
        CfnOutput(self, "UserFilesBucketName", value=self.user_files_bucket.bucket_name)
        CfnOutput(self, "WorkloadIdentityArn", value=self.workload_identity.attr_workload_identity_arn)
        CfnOutput(
            self,
            "RuntimeArn",
            value=f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{self.runtime.attr_agent_runtime_id}",
        )

        # --- cdk-nag suppressions ---------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Bedrock foundation model ARNs require wildcard for model ID. "
                    "Logs, Metrics, X-Ray, and Secrets Manager APIs are scoped to "
                    "project prefix (openclaw/*) or do not support resource-level "
                    "permissions. Cognito userpool/* is scoped to this account/region.",
                    applies_to=[
                        "Resource::arn:aws:bedrock:*::foundation-model/*",
                        f"Resource::arn:aws:bedrock:{region}:{account}:inference-profile/*",
                        f"Resource::arn:aws:secretsmanager:{region}:{account}:secret:openclaw/*",
                        f"Resource::arn:aws:cognito-idp:{region}:{account}:userpool/*",
                        "Resource::*",
                        # S3 per-user file storage bucket (grant_read_write wildcards)
                        "Action::s3:Abort*",
                        "Action::s3:DeleteObject*",
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
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
