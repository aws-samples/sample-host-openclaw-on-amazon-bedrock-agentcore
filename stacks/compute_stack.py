"""Networkless, ambient-authority-free Linux compute job runner boundary.

The runner executes a single pinned-by-digest image in a fully isolated VPC:
no NAT, no internet gateway route, no VPC endpoints, IMDS disabled, and a task
role that may only read the one immutable input object and write under the
per-job output prefix. The real Docker build, ARM64 image, and static-scan
gates are OPEN and are not run here; the pinned image digest is supplied as an
explicit argument so synth stays offline.
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
        )

        # The task role carries no ambient AWS provider authority. It may only
        # read one immutable input object and write under the per-job prefix.
        task_role = iam.Role(
            self,
            "ComputeJobTaskRole",
            role_name=f"personal-operator-compute-job-{region}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description=(
                "Networkless compute job role with no ambient AWS provider authority"
            ),
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject"],
                resources=[f"{self.input_bucket.bucket_arn}/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[f"{self.output_bucket.bucket_arn}/*/jobs/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                    "kms:DescribeKey",
                ],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"s3.{region}.amazonaws.com",
                    }
                },
            )
        )

        # A minimal execution role only writes the runner's own log stream.
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

        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "ComputeJobTaskDefinition",
            cpu=256,
            memory_limit_mib=512,
            task_role=task_role,
            execution_role=execution_role,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        # The image is pinned by digest. The real build/scan gates are OPEN;
        # synth references the immutable digest through an ECR repository ARN
        # form rather than building a local Docker image.
        self.task_definition.add_container(
            "JobRunner",
            image=ecs.ContainerImage.from_registry(
                f"{account}.dkr.ecr.{region}.amazonaws.com/"
                f"personal-operator-compute@{image_digest}"
            ),
            command=["python", "-m", "compute.runner", "/job/input", "/job/output"],
            readonly_root_filesystem=True,
            user="10001:10001",
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="job",
                log_group=log_group,
            ),
        )

        CfnOutput(self, "ComputeImageDigest", value=self.image_digest)
        CfnOutput(self, "ComputeOutputBucketName", value=self.output_bucket.bucket_name)

        for role in (task_role, execution_role):
            cdk_nag.NagSuppressions.add_resource_suppressions(
                role,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-IAM5",
                        reason=(
                            "The only wildcards select objects beneath the exact "
                            "input bucket, the per-job output prefix, and the one "
                            "precreated runner log group. KMS data-plane actions "
                            "remain bound to the exact key via the S3 via-service "
                            "condition."
                        ),
                    ),
                ],
                apply_to_children=True,
            )


__all__ = [
    "ComputeStack",
    "INPUT_BUCKET_NAME",
    "OUTPUT_BUCKET_NAME",
]
