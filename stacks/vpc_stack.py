"""VPC Foundation Stack — subnets, NAT, VPC endpoints, security groups, flow logs."""

import json
import boto3
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from aws_cdk import (
    Annotations,
    Stack,
    aws_ec2 as ec2,
    aws_logs as logs,
    aws_iam as iam,
    RemovalPolicy,
)
import cdk_nag
from constructs import Construct

from stacks import DeploymentNamer, retention_days, stateful_removal_policy


class VpcStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        namer = DeploymentNamer.from_scope(self)
        region = Stack.of(self).region
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        suffix = namer.suffix
        is_dev = suffix == "dev"

        # --- VPC ----------------------------------------------------------
        # Allow users to override AZs via context if AgentCore Runtime has AZ restrictions
        # Context: "availability_zones": ["us-east-1b", "us-east-1c"]
        availability_zones_raw = self.node.try_get_context("availability_zones")
        if isinstance(availability_zones_raw, str):
            availability_zones_raw = availability_zones_raw.strip()
            if availability_zones_raw:
                if availability_zones_raw.startswith("["):
                    try:
                        availability_zones = json.loads(availability_zones_raw)
                    except json.JSONDecodeError:
                        availability_zones = [
                            zone.strip()
                            for zone in availability_zones_raw.split(",")
                            if zone.strip()
                        ]
                else:
                    availability_zones = [
                        zone.strip()
                        for zone in availability_zones_raw.split(",")
                        if zone.strip()
                    ]
            else:
                availability_zones = []
        else:
            availability_zones = availability_zones_raw or []

        vpc_kwargs = {
            "ip_addresses": ec2.IpAddresses.cidr("10.0.0.0/16"),
            "nat_gateways": 0 if is_dev else 1,
            "subnet_configuration": [
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
            ],
        }

        if not is_dev:
            vpc_kwargs["subnet_configuration"].append(
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                )
            )

        if availability_zones:
            vpc_kwargs["availability_zones"] = availability_zones
        else:
            vpc_kwargs["max_azs"] = 2

        self.vpc = ec2.Vpc(self, "Vpc", **vpc_kwargs)

        # VPC Flow Logs
        flow_log_group_name = namer.name("/openclaw/vpc-flow-logs")
        logs_client = boto3.client("logs", region_name=region)
        try:
            flow_log_group_exists = any(
                group.get("logGroupName") == flow_log_group_name
                for group in logs_client.describe_log_groups(
                    logGroupNamePrefix=flow_log_group_name
                ).get("logGroups", [])
            )
        except ClientError as err:
            error_code = str(err.response.get("Error", {}).get("Code", ""))
            raise ValueError(
                "Failed to determine whether the VPC flow log group already exists. "
                f"LogGroup={flow_log_group_name}. Fix the CloudWatch Logs lookup error: {error_code}"
            ) from err
        except (NoCredentialsError, EndpointConnectionError) as err:
            raise ValueError(
                "Failed to determine whether the VPC flow log group already exists because "
                "AWS credentials or the CloudWatch Logs endpoint are unavailable."
            ) from err

        if flow_log_group_exists:
            Annotations.of(self).add_info(
                f"Reusing existing VPC flow log group: {flow_log_group_name}"
            )
            flow_log_group = logs.LogGroup.from_log_group_name(
                self,
                "VpcFlowLogGroup",
                log_group_name=flow_log_group_name,
            )
        else:
            flow_log_group = logs.LogGroup(
                self,
                "VpcFlowLogGroup",
                log_group_name=flow_log_group_name,
                retention=retention_days(log_retention),
                removal_policy=stateful_removal_policy(self),
            )
        flow_log_role = iam.Role(
            self,
            "VpcFlowLogRole",
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
        )
        self.vpc.add_flow_log(
            "FlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                flow_log_group, flow_log_role
            ),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # --- Security Groups ---------------------------------------------
        self.vpce_sg = ec2.SecurityGroup(
            self,
            "VpceSecurityGroup",
            vpc=self.vpc,
            description="VPC Endpoint interface security group",
            allow_all_outbound=False,
        )

        # Allow HTTPS from anywhere in the VPC to VPC endpoints (covers Fargate tasks)
        self.vpce_sg.add_ingress_rule(
            peer=ec2.Peer.ipv4(self.vpc.vpc_cidr_block),
            connection=ec2.Port.tcp(443),
            description="HTTPS from VPC (Fargate tasks)",
        )

        # --- VPC Endpoints (Only in non-dev) ------------------------------
        if not is_dev:
            private_subnets = ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            )

            # Bedrock Runtime endpoint: Private DNS disabled so global/* cross-region
            # inference profiles (e.g. global.anthropic.claude-sonnet-4-6) can route
            # via NAT gateway to AWS's global routing layer. With private DNS enabled,
            # bedrock-runtime.{region}.amazonaws.com resolves to the VPC endpoint IP
            # even when the proxy sets a custom endpoint URL, blocking cross-region calls.
            # Regional model calls still work — they route via NAT to the public endpoint.
            self.vpc.add_interface_endpoint(
                "BedrockRuntimeEndpoint",
                service=ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
                subnets=private_subnets,
                security_groups=[self.vpce_sg],
                private_dns_enabled=False,  # Disabled: cross-region profiles need NAT→global routing
            )

            interface_endpoints = {
                # NOTE: bedrock-agentcore-runtime VPC endpoint service does not exist
                # in ap-southeast-2 yet. Re-add when the service becomes available.
                "Ssm": ec2.InterfaceVpcEndpointAwsService.SSM,
                "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
                "EcrDkr": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
                "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
                "CwLogs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
                "Monitoring": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING,
            }

            for name, service in interface_endpoints.items():
                self.vpc.add_interface_endpoint(
                    f"{name}Endpoint",
                    service=service,
                    subnets=private_subnets,
                    security_groups=[self.vpce_sg],
                    private_dns_enabled=True,
                )

            # S3 gateway endpoint (free, no SG needed)
            self.vpc.add_gateway_endpoint(
                "S3Endpoint",
                service=ec2.GatewayVpcEndpointAwsService.S3,
                subnets=[private_subnets],
            )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.vpce_sg,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-EC23",
                    reason="Ingress uses VPC CIDR (10.0.0.0/16) which resolves via Fn::GetAtt at deploy time; not open to 0.0.0.0/0.",
                ),
                cdk_nag.NagPackSuppression(
                    id="CdkNagValidationFailure",
                    reason="Security group rule uses Fn::GetAtt for VPC CIDR which cannot be validated at synth time.",
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            flow_log_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="VPC Flow Logs writes to a single environment-scoped CloudWatch Logs "
                    "group. CloudWatch Logs IAM resources use the required trailing :* "
                    "log-stream wildcard on the specific log group ARN.",
                    applies_to=[
                        f"Resource::arn:aws:logs:{region}:{Stack.of(self).account}:log-group:{flow_log_group_name}:*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
