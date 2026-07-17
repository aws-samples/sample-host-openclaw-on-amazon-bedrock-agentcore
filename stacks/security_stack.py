"""Security Stack — KMS CMK, Secrets Manager secrets, CloudTrail."""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_iam as iam,
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
    aws_s3 as s3,
    aws_cloudtrail as cloudtrail,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


REQUIRED_REGION = "eu-west-1"


class SecurityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        if region != REQUIRED_REGION:
            raise ValueError(
                f"SecurityStack must be deployed in {REQUIRED_REGION}; got {region}"
            )

        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        # --- KMS CMK for Secrets Manager ----------------------------------
        self.cmk = kms.Key(
            self,
            "SecretsCmk",
            alias="openclaw/secrets",
            description="CMK for OpenClaw secrets encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Allow CloudWatch Alarms to publish to KMS-encrypted SNS topics
        self.cmk.add_to_resource_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:GenerateDataKey*",
                ],
                principals=[
                    iam.ServicePrincipal("cloudwatch.amazonaws.com"),
                ],
                resources=["*"],
            )
        )
        # --- Channel bot token placeholders -------------------------------
        channel_names = ["telegram", "slack", "feishu"]
        self.channel_secrets: dict[str, secretsmanager.Secret] = {}
        for channel in channel_names:
            self.channel_secrets[channel] = secretsmanager.Secret(
                self,
                f"{channel.capitalize()}BotTokenSecret",
                secret_name=f"openclaw/channels/{channel}",
                description=f"Bot token for {channel} channel",
                encryption_key=self.cmk,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=32,
                    exclude_punctuation=True,
                ),  # placeholder — replace via console/CLI
            )

        # --- CloudTrail (optional, off by default) -------------------------
        # Most AWS accounts already have an organization-level or account-level
        # CloudTrail. Deploying a second trail adds cost (S3 storage + log
        # delivery) with no additional security benefit. Enable via cdk.json
        # context: "enable_cloudtrail": true
        enable_cloudtrail = self.node.try_get_context("enable_cloudtrail") or False
        self.trail = None
        trail_bucket = None

        if enable_cloudtrail:
            trail_bucket = s3.Bucket(
                self,
                "CloudTrailBucket",
                encryption=s3.BucketEncryption.S3_MANAGED,
                enforce_ssl=True,
                block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                versioned=True,
                removal_policy=RemovalPolicy.RETAIN,
                auto_delete_objects=False,
            )

            trail_log_group = logs.LogGroup(
                self,
                "CloudTrailLogGroup",
                retention=retention_days(log_retention),
                removal_policy=RemovalPolicy.DESTROY,
            )

            self.trail = cloudtrail.Trail(
                self,
                "CloudTrail",
                bucket=trail_bucket,
                send_to_cloud_watch_logs=True,
                cloud_watch_log_group=trail_log_group,
                is_multi_region_trail=False,
                include_global_service_events=True,
                enable_file_validation=True,
            )

        # --- Webhook validation secret (Telegram secret_token, Slack signing) --
        self.webhook_secret = secretsmanager.Secret(
            self,
            "WebhookSecret",
            secret_name="openclaw/webhook-secret",
            description="Secret token for validating incoming webhook requests "
            "(Telegram X-Telegram-Bot-Api-Secret-Token, Slack signing secret)",
            encryption_key=self.cmk,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )

        # --- cdk-nag suppressions ---
        all_secrets = [self.webhook_secret] + list(self.channel_secrets.values())
        cdk_nag.NagSuppressions.add_resource_suppressions(
            all_secrets,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-SMG4",
                    reason="Secrets are rotated manually via scripts/rotate-token.sh. "
                    "Channel bot tokens are managed externally by each messaging platform. "
                    "Automatic rotation is not applicable for third-party API keys.",
                ),
            ],
        )
        if trail_bucket:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                trail_bucket,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-S1",
                        reason="This is the CloudTrail log bucket itself. Enabling access logs "
                        "would require an additional bucket, creating a recursive logging chain. "
                        "CloudTrail file validation is enabled as an integrity check instead.",
                    ),
                ],
            )
