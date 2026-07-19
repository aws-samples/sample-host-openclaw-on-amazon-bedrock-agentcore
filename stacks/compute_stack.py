"""Networkless, ambient-authority-free Linux compute job runner boundary.

The runner executes a single pinned-by-digest image in a fully isolated VPC:
no NAT, no internet gateway route, no VPC endpoints, IMDS disabled, and no ECS
task role at all. A trusted control-plane service must copy exact inputs into
the job before launch and collect outputs after termination; the container
never receives an AWS credential endpoint. The real Docker build, ARM64 image,
and static-scan gates are OPEN and are not run here; the pinned image digest is
supplied as an explicit argument so synth stays offline.
"""

from __future__ import annotations

import re

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
    aws_s3 as s3,
)
import cdk_nag
from constructs import Construct

REQUIRED_REGION = "eu-west-1"
OUTPUT_BUCKET_NAME = "personal-operator-compute-outputs"
INPUT_BUCKET_NAME = "personal-operator-compute-inputs"
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ComputeStack(Stack):
    """Isolated Fargate job runner with no ambient AWS or network authority."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        image_digest: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"ComputeStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        if not isinstance(cmk_arn, str) or not cmk_arn:
            raise ValueError("ComputeStack requires one exact CMK ARN")
        if (
            not isinstance(image_digest, str)
            or _IMAGE_DIGEST.fullmatch(image_digest) is None
        ):
            raise ValueError("ComputeStack requires one pinned sha256 image digest")

        self.image_digest = image_digest
        encryption_key = kms.Key.from_key_arn(
            self, "ComputeStateEncryptionKey", cmk_arn
        )

        # A fully isolated VPC: only PRIVATE_ISOLATED subnets, no NAT, no
        # internet gateway. There is no route to the internet, IMDS, or any
        # AWS service, which proves the runner is networkless.
        self.vpc = ec2.Vpc(
            self,
            "ComputeIsolatedVpc",
            ip_addresses=ec2.IpAddresses.cidr("10.64.0.0/16"),
            nat_gateways=0,
            max_azs=2,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Isolated",
                    subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                    cidr_mask=24,
                ),
            ],
        )

        # An isolated VPC flow log proves and audits the networkless posture.
        flow_log_group = logs.LogGroup(
            self,
            "ComputeVpcFlowLogGroup",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        flow_log_role = iam.Role(
            self,
            "ComputeVpcFlowLogRole",
            assumed_by=iam.ServicePrincipal("vpc-flow-logs.amazonaws.com"),
        )
        self.vpc.add_flow_log(
            "ComputeFlowLog",
            destination=ec2.FlowLogDestination.to_cloud_watch_logs(
                flow_log_group, flow_log_role
            ),
            traffic_type=ec2.FlowLogTrafficType.ALL,
        )

        # Server-access logs for the input/output stores.
        self.access_log_bucket = s3.Bucket(
            self,
            "ComputeAccessLogBucket",
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # The immutable input object store and the per-job output store. Both
        # are private, encrypted, versioned, and retained.
        self.input_bucket = s3.Bucket(
            self,
            "ComputeInputBucket",
            bucket_name=INPUT_BUCKET_NAME,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            server_access_logs_bucket=self.access_log_bucket,
            server_access_logs_prefix="s3/compute-inputs/",
        )
        self.output_bucket = s3.Bucket(
            self,
            "ComputeOutputBucket",
            bucket_name=OUTPUT_BUCKET_NAME,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            server_access_logs_bucket=self.access_log_bucket,
            server_access_logs_prefix="s3/compute-outputs/",
        )

        log_group = logs.LogGroup(
            self,
            "ComputeRunnerLogGroup",
            log_group_name="/personal-operator/compute/job-runner",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        cluster = ecs.Cluster(
            self,
            "ComputeCluster",
            vpc=self.vpc,
            container_insights=True,
        )

        # The execution role is consumed by the ECS agent, not exposed to the
        # workload. It can pull the one repository image and write the runner's
        # own log stream. The task definition deliberately has no TaskRoleArn,
        # so ECS does not inject a container credential endpoint.
        execution_role = iam.Role(
            self,
            "ComputeJobExecutionRole",
            role_name=f"personal-operator-compute-exec-{region}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    "/personal-operator/compute/job-runner:*"
                ],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/"
                    "personal-operator-compute"
                ],
            )
        )

        image = (
            f"{account}.dkr.ecr.{region}.amazonaws.com/"
            f"personal-operator-compute@{image_digest}"
        )
        self.task_definition = ecs.CfnTaskDefinition(
            self,
            "ComputeJobTaskDefinition",
            cpu="256",
            memory="512",
            network_mode="awsvpc",
            requires_compatibilities=["FARGATE"],
            execution_role_arn=execution_role.role_arn,
            runtime_platform=ecs.CfnTaskDefinition.RuntimePlatformProperty(
                cpu_architecture="ARM64",
                operating_system_family="LINUX",
            ),
            container_definitions=[
                ecs.CfnTaskDefinition.ContainerDefinitionProperty(
                    name="JobRunner",
                    image=image,
                    command=[
                        "python",
                        "-m",
                        "compute.runner",
                        "/job/input",
                        "/job/output",
                    ],
                    essential=True,
                    readonly_root_filesystem=True,
                    user="10001:10001",
                    log_configuration=ecs.CfnTaskDefinition.LogConfigurationProperty(
                        log_driver="awslogs",
                        options={
                            "awslogs-group": log_group.log_group_name,
                            "awslogs-region": region,
                            "awslogs-stream-prefix": "job",
                        },
                    ),
                )
            ],
        )

        CfnOutput(self, "ComputeImageDigest", value=self.image_digest)
        CfnOutput(self, "ComputeOutputBucketName", value=self.output_bucket.bucket_name)

        for role in (execution_role,):
            cdk_nag.NagSuppressions.add_resource_suppressions(
                role,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-IAM5",
                        reason=(
                            "The execution role is not exposed to the container. "
                            "Its only wildcard is required by ECR token retrieval; "
                            "image reads are bound to the exact repository and logs "
                            "to the precreated runner log group."
                        ),
                    ),
                ],
                apply_to_children=True,
            )

        # The access-log bucket is the terminal server-access-log sink for the
        # input/output stores; it cannot log to itself.
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.access_log_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason=(
                        "This bucket is the terminal server-access-log sink for "
                        "the compute input/output stores and cannot log to itself."
                    ),
                ),
            ],
        )


__all__ = [
    "ComputeStack",
    "INPUT_BUCKET_NAME",
    "OUTPUT_BUCKET_NAME",
]
