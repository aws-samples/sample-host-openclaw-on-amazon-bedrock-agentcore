"""Build Stack — CodeBuild project for ARM64 bridge container builds.

Provides a CodeBuild project that builds the bridge Docker image inside AWS,
avoiding local Docker / corporate network restrictions. Source code is uploaded
to S3 and the build runs natively on an ARM64 CodeBuild worker.

Trigger via: scripts/codebuild-push.sh
"""

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    aws_codebuild as codebuild,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class BuildStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        ecr_repo_name: str,
        image_version: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        ecr_registry = f"{account}.dkr.ecr.{region}.amazonaws.com"
        log_retention = int(
            self.node.try_get_context("cloudwatch_log_retention_days") or "30"
        )

        # --- S3 bucket for bridge source uploads ---------------------------------
        # Receives a zip of bridge/ uploaded by scripts/codebuild-push.sh
        self.source_bucket = s3.Bucket(
            self,
            "BuildSourceBucket",
            bucket_name=f"openclaw-build-source-{account}-{region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=False,
        )

        # --- CloudWatch log group for build logs ---------------------------------
        build_log_group = logs.LogGroup(
            self,
            "BuildLogGroup",
            log_group_name="/openclaw/codebuild/bridge-build",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- CodeBuild project (ARM64 native, privileged for Docker) -------------
        self.project = codebuild.Project(
            self,
            "BridgeBuildProject",
            project_name="openclaw-bridge-build",
            description="Builds the OpenClaw bridge ARM64 container and pushes to ECR",
            source=codebuild.Source.s3(
                bucket=self.source_bucket,
                path="bridge-source.zip",
            ),
            environment=codebuild.BuildEnvironment(
                # Native ARM64 worker — no QEMU emulation needed
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2_STANDARD_3_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,  # Required for Docker daemon access
            ),
            environment_variables={
                "ECR_REGISTRY": codebuild.BuildEnvironmentVariable(
                    value=ecr_registry
                ),
                "ECR_REPO_NAME": codebuild.BuildEnvironmentVariable(
                    value=ecr_repo_name
                ),
                "IMAGE_VERSION": codebuild.BuildEnvironmentVariable(
                    value=f"v{image_version}"
                ),
            },
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "echo Logging in to ECR...",
                                "aws ecr get-login-password --region $AWS_DEFAULT_REGION"
                                " | docker login --username AWS --password-stdin $ECR_REGISTRY",
                            ]
                        },
                        "build": {
                            "commands": [
                                "echo Building $ECR_REGISTRY/$ECR_REPO_NAME:$IMAGE_VERSION ...",
                                "docker build --platform linux/arm64"
                                " -t $ECR_REPO_NAME:$IMAGE_VERSION .",
                                "docker tag $ECR_REPO_NAME:$IMAGE_VERSION"
                                " $ECR_REGISTRY/$ECR_REPO_NAME:$IMAGE_VERSION",
                            ]
                        },
                        "post_build": {
                            "commands": [
                                "docker push $ECR_REGISTRY/$ECR_REPO_NAME:$IMAGE_VERSION",
                                "echo Done: $ECR_REGISTRY/$ECR_REPO_NAME:$IMAGE_VERSION",
                            ]
                        },
                    },
                }
            ),
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(
                    log_group=build_log_group,
                )
            ),
        )

        # ECR auth token (account-scoped, no resource restriction possible)
        self.project.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )

        # ECR push/pull to the bridge repo only
        self.project.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                resources=[
                    f"arn:aws:ecr:{region}:{account}:repository/{ecr_repo_name}",
                ],
            )
        )

        # --- Outputs -------------------------------------------------------------
        CfnOutput(
            self,
            "BuildProjectName",
            value=self.project.project_name,
            description="CodeBuild project name — pass to scripts/codebuild-push.sh",
        )
        CfnOutput(
            self,
            "BuildSourceBucketName",
            value=self.source_bucket.bucket_name,
            description="S3 bucket where bridge source zip is uploaded before build",
        )

        # --- cdk-nag suppressions ------------------------------------------------
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.project,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="ecr:GetAuthorizationToken does not support resource-level "
                    "permissions — must be '*'. All other ECR actions are scoped "
                    "to the bridge repo ARN.",
                    applies_to=["Resource::*"],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CB3",
                    reason="Privileged mode is required to run the Docker daemon "
                    "inside CodeBuild for building container images.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CB4",
                    reason="Build source bucket uses S3-managed encryption which is "
                    "sufficient for temporary build artifacts. No CMK required.",
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.source_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason="Server access logging not required for ephemeral build "
                    "source bucket — objects are temporary build inputs.",
                ),
            ],
        )
