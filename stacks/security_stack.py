"""Security Stack — KMS CMK, Secrets Manager secrets, CloudTrail."""

from aws_cdk import (
    CfnOutput,
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

from stacks import DeploymentNamer, retention_days


COGNITO_ADVANCED_SECURITY_MODES = ("OFF", "AUDIT", "ENFORCED")


def cognito_advanced_security_mode(scope: Construct) -> str:
    """Read the cognito_advanced_security_mode context key (default OFF).

    AUDIT and ENFORCED turn on Cognito threat protection and the Plus feature
    plan it requires, which is billed per monthly active user.
    """
    raw = scope.node.try_get_context("cognito_advanced_security_mode")
    if raw is None or str(raw).strip() == "":
        return "OFF"
    value = str(raw).strip().upper()
    if value not in COGNITO_ADVANCED_SECURITY_MODES:
        raise ValueError(
            "cognito_advanced_security_mode must be one of OFF, AUDIT or ENFORCED, "
            f"got {raw!r}"
        )
    return value


class SecurityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        user_files_bucket_arn: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        namer = DeploymentNamer.from_scope(self)
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30

        # --- KMS CMK for Secrets Manager ----------------------------------
        self.cmk = kms.Key(
            self,
            "SecretsCmk",
            alias=namer.name("openclaw/secrets"),
            description="CMK for OpenClaw secrets encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # Allow CloudWatch Alarms to publish to KMS-encrypted SNS topics.
        # Scope the grant to this account's CloudWatch alarms only (confused-
        # deputy guard): add aws:SourceAccount + aws:SourceArn conditions so
        # the service principal cannot be induced from any other account or on
        # behalf of a non-alarm resource. kms:Decrypt is retained because
        # CloudWatch must decrypt the data key when publishing an alarm
        # notification to the SSE-KMS SNS topic (see observability_stack.py).
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
                conditions={
                    "StringEquals": {
                        "aws:SourceAccount": Stack.of(self).account,
                    },
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:cloudwatch:{Stack.of(self).region}:"
                            f"{Stack.of(self).account}:alarm:*"
                        ),
                    },
                },
            )
        )


        # --- Gateway token (auto-generated 64-char) -----------------------
        self.gateway_token_secret = secretsmanager.Secret(
            self,
            "GatewayTokenSecret",
            secret_name=namer.name("openclaw/gateway-token"),
            description="Token for CloudFront Web UI access",
            encryption_key=self.cmk,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )

        # --- Channel bot token placeholders -------------------------------
        # Append new channels at the end so existing construct IDs stay stable.
        channel_names = ["whatsapp", "telegram", "discord", "slack", "feishu", "line"]
        self.channel_secrets: dict[str, secretsmanager.Secret] = {}
        for channel in channel_names:
            self.channel_secrets[channel] = secretsmanager.Secret(
                self,
                f"{channel.capitalize()}BotTokenSecret",
                secret_name=namer.name(f"openclaw/channels/{channel}"),
                description=f"Bot token for {channel} channel",
                encryption_key=self.cmk,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=32,
                    exclude_punctuation=True,
                ),  # placeholder — replace via console/CLI
            )

        # --- CloudTrail (optional, off by default) -------------------------
        # Most AWS accounts already have an organization-level or account-level
        # CloudTrail, and an account can hold only a few trails per region, so
        # the dedicated trail stays opt-in: "enable_cloudtrail": true in
        # cdk.json. When enabled it is multi-region and also records S3 data
        # events on the user-files bucket, whose per-user namespace boundary is
        # enforced in application code.
        enable_cloudtrail = bool(self.node.try_get_context("enable_cloudtrail") or False)
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
                removal_policy=RemovalPolicy.RETAIN,
            )

            self.trail = cloudtrail.Trail(
                self,
                "CloudTrail",
                bucket=trail_bucket,
                send_to_cloud_watch_logs=True,
                cloud_watch_log_group=trail_log_group,
                # Multi-region so sts:AssumeRole / Secrets Manager / KMS calls
                # made from any region (e.g. global endpoints) are captured.
                is_multi_region_trail=True,
                include_global_service_events=True,
                enable_file_validation=True,
            )

            # S3 data events for the multi-tenant user-files bucket. Object-
            # level reads/writes across namespaces are only visible through
            # these selectors; without them the trail records only management
            # events, which is what the user-files bucket's AwsSolutions-S1
            # suppression already assumes exists.
            if not user_files_bucket_arn:
                # Same name the AgentCore stack gives the bucket. Built from
                # this stack's account/region so it also resolves when the CDK
                # environment is not set at synth time.
                user_files_bucket_arn = "arn:aws:s3:::" + namer.name(
                    f"openclaw-user-files-{self.account}-{self.region}"
                )
            if user_files_bucket_arn:
                user_files_bucket = s3.Bucket.from_bucket_arn(
                    self, "UserFilesBucketRefForTrail", user_files_bucket_arn,
                )
                self.trail.add_s3_event_selector(
                    [cloudtrail.S3EventSelector(bucket=user_files_bucket)],
                    read_write_type=cloudtrail.ReadWriteType.ALL,
                    include_management_events=True,
                )

        # --- Cognito User Pool (admin-provisioned identities) ---------------
        # NOTE on password_policy: this pool exposes only the IAM-gated
        # ADMIN_USER_PASSWORD_AUTH flow (no self sign-up, no hosted UI, no
        # USER_PASSWORD_AUTH/SRP), so the policy is applied solely at admin
        # password-set time. Complexity is re-enabled as defense-in-depth so
        # that if a future caller ever uses a user-chosen password, the pool
        # enforces strong characters. The derivation module
        # (bridge/cognito-token.js) is responsible for producing passwords
        # that satisfy this policy for machine-provisioned users; see the
        # cdk-nag COG1 note below.
        advanced_security_mode = cognito_advanced_security_mode(self)
        threat_protection_props = {}
        if advanced_security_mode != "OFF":
            threat_protection_props = {
                "feature_plan": cognito.FeaturePlan.PLUS,
                "standard_threat_protection_mode": {
                    "AUDIT": cognito.StandardThreatProtectionMode.AUDIT_ONLY,
                    "ENFORCED": cognito.StandardThreatProtectionMode.FULL_FUNCTION,
                }[advanced_security_mode],
            }
        self.user_pool = cognito.UserPool(
            self,
            "IdentityPool",
            user_pool_name=namer.name("openclaw-identity-pool"),
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True),
            password_policy=cognito.PasswordPolicy(
                min_length=16,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                # Symbols intentionally not required: base64url output used by
                # the admin-provisioning derivation only contains '-' and '_'
                # as symbols (~3% of alphabet), which would make password-set
                # non-deterministically fail. Entropy (~192 bits) is well
                # above anything a symbol requirement would add.
                require_symbols=False,
            ),
            # Threat protection is opt-in (cognito_advanced_security_mode).
            # It needs the Plus feature plan, which is billed per monthly
            # active user, so the default leaves both unset.
            **threat_protection_props,
            removal_policy=RemovalPolicy.RETAIN,
            account_recovery=cognito.AccountRecovery.NONE,
        )

        self.user_pool_client = self.user_pool.add_client(
            "ProxyClient",
            user_pool_client_name=namer.name("openclaw-proxy"),
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
            secret_name=namer.name("openclaw/webhook-secret"),
            description="Secret token for validating incoming webhook requests "
            "(Telegram X-Telegram-Bot-Api-Secret-Token, Slack signing secret)",
            encryption_key=self.cmk,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )

        # --- Cognito password secret (previous derivation scheme) -----------
        # The runtime no longer reads this secret: each Cognito user now has a
        # random password in its own secret (bridge/cognito-token.js), and
        # existing users move to one on their next authentication. The secret
        # is kept unchanged so an older runtime image still deploys.
        self.cognito_password_secret = secretsmanager.Secret(
            self,
            "CognitoPasswordSecret",
            secret_name=namer.name("openclaw/cognito-password-secret"),
            description="HMAC secret for deriving Cognito user passwords",
            encryption_key=self.cmk,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_punctuation=True,
            ),
        )

        # --- Stack outputs consumed by scripts/deploy.sh ---------------------
        # Stable, hand-chosen OutputKeys. deploy.sh matches these with an exact
        # OutputKey== query; never rely on CDK's auto-generated ExportsOutput*
        # keys (they embed a logical-id hash and only exist while another stack
        # happens to reference the value).
        CfnOutput(self, "SecretsCmkArn", value=self.cmk.key_arn)
        CfnOutput(self, "CognitoUserPoolId", value=self.user_pool_id)
        CfnOutput(self, "CognitoProxyClientId", value=self.user_pool_client_id)

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
                    reason="Password policy now requires lower/upper/digits with min_length=16. "
                    "Symbol requirement is intentionally omitted because this pool is only ever "
                    "authenticated against via ADMIN_USER_PASSWORD_AUTH with machine-provisioned "
                    "base64url passwords (~192 bits entropy, bridge/cognito-token.js), whose "
                    "alphabet contains only '-' and '_' as symbols and so would hit a non-zero "
                    "password-set failure rate under require_symbols=True. The remaining "
                    "complexity gap vs. COG1's full requirement is tracked by the companion "
                    "architectural remediation (per-user credentials / scoped token service).",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG2",
                    reason="Users are service identities auto-provisioned from channel user IDs "
                    "(e.g. telegram:12345). MFA is not applicable for non-interactive accounts.",
                ),
            ],
        )
        threat_protection_suppressions = []
        if advanced_security_mode != "ENFORCED":
            threat_protection_suppressions.append(
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG3",
                    reason="Threat protection is opt-in through the "
                    "cognito_advanced_security_mode context key because it is billed per "
                    "monthly active user. All auth is admin-initiated.",
                ),
            )
        if advanced_security_mode == "OFF":
            threat_protection_suppressions.append(
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-COG8",
                    reason="The Plus feature plan only adds threat protection, which is "
                    "opt-in through the cognito_advanced_security_mode context key. This "
                    "pool has no user-facing sign-in: self sign-up is off, and the only "
                    "client is the bridge, which provisions users and authenticates them "
                    "via ADMIN_USER_PASSWORD_AUTH.",
                ),
            )
        if threat_protection_suppressions:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                self.user_pool, threat_protection_suppressions,
            )
