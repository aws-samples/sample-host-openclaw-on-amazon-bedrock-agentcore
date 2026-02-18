"""Fargate Stack — ECS cluster, task def, ALB, Fargate service."""

from aws_cdk import (
    Stack,
    Duration,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr as ecr,
    aws_iam as iam,
    aws_logs as logs,
    aws_elasticloadbalancingv2 as elbv2,
    RemovalPolicy,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class FargateStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        vpc: ec2.IVpc,
        gateway_token_secret_name: str,
        cmk_arn: str,
        runtime_id: str,
        runtime_endpoint_id: str,
        memory_id: str,
        cognito_user_pool_id: str,
        cognito_client_id: str,
        cognito_password_secret_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Create Fargate SG locally to avoid cross-stack cycles
        self.fargate_sg = ec2.SecurityGroup(
            self,
            "FargateSecurityGroup",
            vpc=vpc,
            description="Fargate service security group",
            allow_all_outbound=True,
        )

        cpu = self.node.try_get_context("fargate_cpu") or 256
        memory = self.node.try_get_context("fargate_memory_mib") or 512
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        # --- ECR Repository for Bridge image ------------------------------
        self.bridge_repo = ecr.Repository(
            self,
            "BridgeRepo",
            repository_name="openclaw-bridge",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            image_scan_on_push=True,
        )

        # --- ECS Cluster --------------------------------------------------
        self.cluster = ecs.Cluster(
            self,
            "Cluster",
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # --- Task Role (what the container can do) ------------------------
        task_role = iam.Role(
            self,
            "BridgeTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        # Read secrets — use inline policy with ARN strings to avoid cross-stack grant cycles
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:DescribeSecret",
                ],
                resources=[
                    f"arn:aws:secretsmanager:{Stack.of(self).region}:{Stack.of(self).account}:secret:openclaw/*",
                ],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
            )
        )

        # Invoke Bedrock + AgentCore Runtime
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:InvokeAgent",
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                    "bedrock-agentcore:InvokeRuntime",
                    "bedrock-agentcore:InvokeRuntimeEndpoint",
                ],
                resources=["*"],
            )
        )

        # Cognito admin operations for auto-provisioning identities
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cognito-idp:AdminCreateUser",
                    "cognito-idp:AdminSetUserPassword",
                    "cognito-idp:AdminInitiateAuth",
                    "cognito-idp:AdminGetUser",
                ],
                resources=[
                    f"arn:aws:cognito-idp:{Stack.of(self).region}:{Stack.of(self).account}:userpool/*",
                ],
            )
        )

        # --- Execution Role (ECR pull, log creation) ----------------------
        execution_role = iam.Role(
            self,
            "BridgeExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonECSTaskExecutionRolePolicy"
            )
        )
        self.bridge_repo.grant_pull(execution_role)

        # --- Log Group ----------------------------------------------------
        log_group = logs.LogGroup(
            self,
            "BridgeLogGroup",
            log_group_name="/openclaw/bridge",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Task Definition ----------------------------------------------
        task_def = ecs.FargateTaskDefinition(
            self,
            "BridgeTaskDef",
            cpu=cpu,
            memory_limit_mib=memory,
            task_role=task_role,
            execution_role=execution_role,
        )

        container = task_def.add_container(
            "bridge",
            image=ecs.ContainerImage.from_ecr_repository(self.bridge_repo, tag="latest"),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="bridge",
                log_group=log_group,
            ),
            environment={
                "AWS_REGION": Stack.of(self).region,
                "GATEWAY_TOKEN_SECRET_ID": gateway_token_secret_name,
                "NODE_OPTIONS": "--max-old-space-size=768",
                "AGENTCORE_RUNTIME_ID": runtime_id,
                "AGENTCORE_RUNTIME_ENDPOINT_ID": runtime_endpoint_id,
                "AGENTCORE_MEMORY_ID": memory_id,
                "PROXY_MODE": self.node.try_get_context("proxy_mode") or "bedrock-direct",
                "BEDROCK_MODEL_ID": self.node.try_get_context("default_model_id") or "au.anthropic.claude-sonnet-4-6",
                "CLOUDFRONT_DOMAIN": self.node.try_get_context("cloudfront_domain") or "",
                "COGNITO_USER_POOL_ID": cognito_user_pool_id,
                "COGNITO_CLIENT_ID": cognito_client_id,
                "COGNITO_PASSWORD_SECRET_ID": cognito_password_secret_name,
            },
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", "curl -f http://localhost:18790/health || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=Duration.seconds(60),
            ),
        )
        container.add_port_mappings(
            ecs.PortMapping(container_port=18789, protocol=ecs.Protocol.TCP),
        )
        container.add_port_mappings(
            ecs.PortMapping(container_port=18790, protocol=ecs.Protocol.TCP),
        )

        # --- Fargate Service ----------------------------------------------
        self.service = ecs.FargateService(
            self,
            "BridgeService",
            cluster=self.cluster,
            task_definition=task_def,
            desired_count=1,
            security_groups=[self.fargate_sg],
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            assign_public_ip=False,
            enable_execute_command=True,
            min_healthy_percent=0,  # single-task service; allow 0 during rolling deploy
            max_healthy_percent=200,
        )

        # Allow ALB → Fargate (gateway + proxy health check)
        self.fargate_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(18789),
            description="ALB to Fargate bridge gateway",
        )
        self.fargate_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(18790),
            description="ALB to Fargate bridge health check",
        )

        # --- Internet-facing ALB (CloudFront HTTP origin + WebSocket) --------
        # CloudFront VPC Origins do not support WebSocket upgrade, so we need
        # an internet-facing ALB restricted to CloudFront origin-facing IPs.
        self.public_alb = elbv2.ApplicationLoadBalancer(
            self,
            "PublicAlb",
            vpc=vpc,
            internet_facing=True,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC,
            ),
        )
        # Restrict to CloudFront origin-facing IPs only (managed prefix list)
        self.public_alb.connections.allow_from(
            ec2.Peer.prefix_list("pl-b8a742d1"),
            ec2.Port.tcp(80),
            "Allow HTTP from CloudFront origin-facing IPs only",
        )

        public_listener = self.public_alb.add_listener(
            "PublicHttpListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,  # Do not auto-create 0.0.0.0/0 ingress rule
        )

        public_listener.add_targets(
            "PublicBridgeTarget",
            port=18789,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.service],
            health_check=elbv2.HealthCheck(
                path="/health",
                port="18790",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(10),
            ),
        )

        # Public ALB → Fargate health check port egress
        self.public_alb.connections.allow_to(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(18790),
            "Public ALB to Fargate health check port",
        )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            task_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Secrets Manager resources use openclaw/* prefix — scoped to "
                    "project secrets only. Bedrock InvokeAgent/InvokeModel do not "
                    "support resource-level ARNs for AgentCore endpoints. "
                    "Cognito userpool/* is scoped to this account/region.",
                    applies_to=[
                        f"Resource::arn:aws:secretsmanager:{Stack.of(self).region}:{Stack.of(self).account}:secret:openclaw/*",
                        f"Resource::arn:aws:cognito-idp:{Stack.of(self).region}:{Stack.of(self).account}:userpool/*",
                        "Resource::*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="AmazonECSTaskExecutionRolePolicy is the AWS-recommended managed "
                    "policy for ECS task execution (ECR pull + CloudWatch Logs). "
                    "See https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="Wildcard on ECR repository actions is generated by CDK "
                    "grant_pull() and scoped to the specific repository.",
                    applies_to=["Resource::*"],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            task_def,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-ECS2",
                    reason="Environment variables (AWS_REGION, GATEWAY_TOKEN_SECRET_ID, "
                    "AGENTCORE_RUNTIME_ID, AGENTCORE_RUNTIME_ENDPOINT_ID, "
                    "AGENTCORE_MEMORY_ID, PROXY_MODE, COGNITO_USER_POOL_ID, "
                    "COGNITO_CLIENT_ID, COGNITO_PASSWORD_SECRET_ID) contain only "
                    "non-sensitive configuration. Actual secret values are fetched "
                    "at runtime from Secrets Manager by the entrypoint script.",
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.public_alb,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-ELB2",
                    reason="ALB access logs not enabled to minimize cost. CloudFront logging "
                    "and Fargate container logs provide sufficient observability. "
                    "The public ALB is restricted to CloudFront origin-facing IPs only.",
                ),
            ],
        )
        all_sgs = [
            self.fargate_sg,
            self.public_alb.connections.security_groups[0],
        ]
        for sg in all_sgs:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                sg,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-EC23",
                        reason="Ingress uses VPC CIDR or CloudFront managed prefix list; "
                        "not open to 0.0.0.0/0.",
                    ),
                    cdk_nag.NagPackSuppression(
                        id="CdkNagValidationFailure",
                        reason="Security group rule uses Fn::GetAtt/prefix list which "
                        "cannot be validated at synth time.",
                    ),
                ],
            )
