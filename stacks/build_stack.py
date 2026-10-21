"""Build Stack — CodeBuild project for ARM64 bridge container builds.

Provides a CodeBuild project that builds the bridge Docker image inside AWS,
avoiding local Docker / corporate network restrictions. Source code is uploaded
to S3 and the build runs natively on an ARM64 CodeBuild worker.

Opt-in: the stack is only part of the app when ``enable_codebuild`` is true
(``cdk deploy OpenClawBuild -c enable_codebuild=true``). It pushes to the
same ECR repository that ``scripts/deploy.sh`` uses (``ECR_REPOSITORY``,
default ``openclaw-bridge``); the repository itself is created by
``scripts/runtime_image.py``, not by CDK.

Trigger via: scripts/codebuild-push.sh, then hand the printed image URI to
``BUILD_MODE=codebuild ./scripts/deploy.sh --image-uri URI``.
"""

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
    Token,
    aws_codebuild as codebuild,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days

# Same prefixes scripts/runtime_deploy.py accepts and the AgentCore execution
# role may pull from (stacks/agentcore_stack.py).
ECR_PREFIXES = ("openclaw-bridge", "openclaw_agent", "bedrock-agentcore-")


class BuildStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        ecr_repo_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        if not ecr_repo_name.startswith(ECR_PREFIXES):
            raise ValueError(
                "ECR repository must use an execution-role prefix: " + ", ".join(ECR_PREFIXES)
            )

        region = Stack.of(self).region
        account = Stack.of(self).account
        partition = Stack.of(self).partition
        # cdk-nag matches applies_to against ARNs with unresolved pseudo
        # parameters rendered as <AWS::...>; mirror that for env-agnostic synth.
        nag_partition = "<AWS::Partition>" if Token.is_unresolved(partition) else partition
        nag_region = "<AWS::Region>" if Token.is_unresolved(region) else region
        nag_account = "<AWS::AccountId>" if Token.is_unresolved(account) else account
        ecr_repository_uri = f"{account}.dkr.ecr.{region}.{Stack.of(self).url_suffix}/{ecr_repo_name}"
        ecr_repository_arn = self.format_arn(
            service="ecr", resource="repository", resource_name=ecr_repo_name
        )
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
        # IMAGE_TAG has no default: scripts/codebuild-push.sh sets it per build
        # (v<image_version>), so bumping image_version never changes this stack.
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
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,  # Required for Docker daemon access
            ),
            environment_variables={
                "ECR_REPOSITORY_URI": codebuild.BuildEnvironmentVariable(
                    value=ecr_repository_uri
                ),
                "IMAGE_TAG": codebuild.BuildEnvironmentVariable(value=""),
            },
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "echo \"$IMAGE_TAG\" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'"
                                " || { echo 'IMAGE_TAG must be set to a valid ECR tag'; exit 1; }",
                                "echo Logging in to ECR...",
                                "aws ecr get-login-password --region $AWS_DEFAULT_REGION"
                                " | docker login --username AWS --password-stdin ${ECR_REPOSITORY_URI%%/*}",
                            ]
                        },
                        "build": {
                            # Build, verify and push in one phase: post_build runs
                            # even after a failed build, so nothing is pushed there.
                            "commands": [
                                "echo Building $ECR_REPOSITORY_URI:$IMAGE_TAG ...",
                                "docker build --platform linux/arm64"
                                " -t $ECR_REPOSITORY_URI:$IMAGE_TAG .",
                                "ARCH=$(docker image inspect --format '{{.Architecture}}'"
                                " $ECR_REPOSITORY_URI:$IMAGE_TAG)"
                                " && test \"$ARCH\" = arm64"
                                " || { echo \"image architecture is $ARCH, expected arm64\"; exit 1; }",
                                "docker push $ECR_REPOSITORY_URI:$IMAGE_TAG",
                                "echo Done: $ECR_REPOSITORY_URI:$IMAGE_TAG",
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
                resources=[ecr_repository_arn],
            )
        )

        # --- Outputs -------------------------------------------------------------
        CfnOutput(
            self,
            "BuildProjectName",
            value=self.project.project_name,
            description="CodeBuild project name — used by scripts/codebuild-push.sh",
        )
        CfnOutput(
            self,
            "BuildSourceBucketName",
            value=self.source_bucket.bucket_name,
            description="S3 bucket where bridge source zip is uploaded before build",
        )
        CfnOutput(
            self,
            "EcrRepositoryName",
            value=ecr_repo_name,
            description="ECR repository the CodeBuild project may push to",
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
                    id="AwsSolutions-IAM5",
                    reason="S3 wildcard permissions required for CodeBuild to access source bucket "
                    "and write build artifacts. Resource-level permissions not supported for "
                    "CodeBuild S3 operations.",
                    applies_to=[
                        "Action::s3:GetBucket*",
                        "Action::s3:GetObject*",
                        "Action::s3:List*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="CloudWatch Logs wildcard permissions required for CodeBuild to write "
                    "build logs. Resource-level permissions not supported for CodeBuild "
                    "CloudWatch operations.",
                    applies_to=[
                        f"Resource::arn:{nag_partition}:logs:{nag_region}:{nag_account}:log-group:/aws/codebuild/<BridgeBuildProject483AAFF2>:*",
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason="CodeBuild report group wildcard permissions required for CodeBuild to "
                    "generate build reports. Resource-level permissions not supported for "
                    "CodeBuild report group operations.",
                    applies_to=[
                        f"Resource::arn:{nag_partition}:codebuild:{nag_region}:{nag_account}:report-group/<BridgeBuildProject483AAFF2>-*",
                    ],
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
