"""Security Stack — KMS CMK, Secrets Manager secrets, CloudTrail."""

from aws_cdk import (
    Stack,
    RemovalPolicy,
    aws_iam as iam,
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
    aws_cognito as cognito,
    aws_s3 as s3,
    aws_cloudtrail as cloudtrail,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


class SecurityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

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


        # --- Gateway token (auto-generated 64-char) -----------------------
        self.gateway_token_secret = secretsmanager.Secret(
            self,
            "GatewayTokenSecret",
            secret_name="openclaw/gateway-token",
            description="Token for CloudFront Web UI access",
            encryption_key=self.cmk,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )

        # --- Channel bot token placeholders -------------------------------
        channel_names = ["whatsapp", "telegram", "discord", "slack", "feishu"]
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

        # --- Cognito User Pool (admin-provisioned identities) ---------------
        self.user_pool = cognito.UserPool(
            self,
            "IdentityPool",
            user_pool_name="openclaw-identity-pool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            password_policy=cognito.PasswordPolicy(
                min_length=16,
                require_lowercase=False,
                require_uppercase=False,
                require_digits=False,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.RETAIN,
            account_recovery=cognito.AccountRecovery.NONE,
        )

        self.user_pool_client = self.user_pool.add_client(
            "ProxyClient",
            user_pool_client_name="openclaw-proxy",
            auth_flows=cognito.AuthFlow(
                admin_user_password=True,
            ),
            generate_secret=False,
        )

        # Expose Cognito outputs for downstream stacks
        self.user_pool_id = self.user_pool.user_pool_id
        self.user_pool_client_id = self.user_pool_client.user_pool_client_id
        self.cognito_issuer_url = (
            f"https://cognito-idp.{Stack.of(self).region}.amazonaws.com/"
            f"{self.user_pool.user_pool_id}"
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

        # --- HMAC secret for deriving Cognito user passwords -----------------
        self.cognito_password_secret = secretsmanager.Secret(
            self,
            "CognitoPasswordSecret",
            secret_name="openclaw/cognito-password-secret",
            description="HMAC secret for deriving Cognito user passwords",
            encryption_key=self.cmk,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )

        # --- cdk-nag suppressions ---
        all_secrets = [self.gateway_token_secret, self.cognito_password_secret, self.webhook_secret] + list(self.channel_secrets.values())
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
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.user_pool,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG1",
                    reason="Passwords are HMAC-derived by the proxy, not user-chosen. "
                    "Complexity requirements are unnecessary for deterministic passwords.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG2",
                    reason="Users are service identities auto-provisioned from channel user IDs "
                    "(e.g. telegram:12345). MFA is not applicable for non-interactive accounts.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG3",
                    reason="Advanced security mode (WAF integration) adds cost with no benefit "
                    "for programmatic-only service identities. All auth is admin-initiated.",
                ),
            ],
        )
