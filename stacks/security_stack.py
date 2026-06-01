"""Security Stack — KMS CMK, Secrets Manager secrets, CloudTrail."""

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from aws_cdk import (
    Annotations,
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

from stacks import (
    DeploymentNamer,
    auto_delete_bucket_objects,
    retention_days,
    stateful_removal_policy,
)


class SecurityStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        namer = DeploymentNamer.from_scope(self)
        region = Stack.of(self).region
        log_retention = self.node.try_get_context("cloudwatch_log_retention_days") or 30
        kms_client = boto3.client("kms", region_name=region)
        secrets_client = boto3.client("secretsmanager", region_name=region)
        cognito_client = boto3.client("cognito-idp", region_name=region)
        cmk_alias_name = namer.name("openclaw/secrets")
        gateway_token_secret_name = namer.name("openclaw/gateway-token")
        webhook_secret_name = namer.name("openclaw/webhook-secret")
        cognito_password_secret_name = namer.name("openclaw/cognito-password-secret")
        user_pool_name = namer.name("openclaw-identity-pool")
        user_pool_client_name = namer.name("openclaw-proxy")

        # --- KMS CMK for Secrets Manager ----------------------------------
        try:
            cmk_metadata = kms_client.describe_key(KeyId=f"alias/{cmk_alias_name}")["KeyMetadata"]
            self.cmk = kms.Key.from_key_arn(self, "SecretsCmk", cmk_metadata["Arn"])
            cmk_created = False
            Annotations.of(self).add_info(f"Reusing existing KMS key alias: alias/{cmk_alias_name}")
        except ClientError as err:
            error_code = str(err.response.get("Error", {}).get("Code", ""))
            if error_code in {"NotFoundException", "InvalidArnException"}:
                self.cmk = kms.Key(
                    self,
                    "SecretsCmk",
                    alias=cmk_alias_name,
                    description="CMK for OpenClaw secrets encryption",
                    enable_key_rotation=True,
                    removal_policy=stateful_removal_policy(self),
                )
                cmk_created = True
            else:
                raise ValueError(
                    "Failed to determine whether the secrets KMS key already exists. "
                    f"Alias=alias/{cmk_alias_name}. Fix the KMS lookup error: {error_code}"
                ) from err
        except (NoCredentialsError, EndpointConnectionError) as err:
            raise ValueError(
                "Failed to determine whether the secrets KMS key already exists because "
                "AWS credentials or the KMS endpoint are unavailable."
            ) from err

        # Allow CloudWatch Alarms to publish to KMS-encrypted SNS topics
        if cmk_created:
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
        created_secrets = []

        def resolve_secret(secret_id: str, secret_name: str, description: str, password_length: int):
            try:
                secret_description = secrets_client.describe_secret(SecretId=secret_name)
                Annotations.of(self).add_info(f"Reusing existing secret: {secret_name}")
                return secretsmanager.Secret.from_secret_complete_arn(
                    self,
                    secret_id,
                    secret_complete_arn=secret_description["ARN"],
                )
            except ClientError as err:
                error_code = str(err.response.get("Error", {}).get("Code", ""))
                if error_code == "ResourceNotFoundException":
                    secret = secretsmanager.Secret(
                        self,
                        secret_id,
                        secret_name=secret_name,
                        description=description,
                        encryption_key=self.cmk,
                        generate_secret_string=secretsmanager.SecretStringGenerator(
                            password_length=password_length,
                            exclude_punctuation=True,
                        ),
                    )
                    created_secrets.append(secret)
                    return secret
                raise ValueError(
                    "Failed to determine whether the secret already exists. "
                    f"Secret={secret_name}. Fix the Secrets Manager lookup error: {error_code}"
                ) from err
            except (NoCredentialsError, EndpointConnectionError) as err:
                raise ValueError(
                    "Failed to determine whether the secret already exists because "
                    "AWS credentials or the Secrets Manager endpoint are unavailable."
                ) from err

        self.gateway_token_secret = resolve_secret(
            "GatewayTokenSecret",
            gateway_token_secret_name,
            "Token for CloudFront Web UI access",
            64,
        )

        # --- Channel bot token placeholders -------------------------------
        channel_names = ["whatsapp", "telegram", "discord", "slack", "feishu"]
        self.channel_secrets: dict[str, secretsmanager.Secret] = {}
        for channel in channel_names:
            self.channel_secrets[channel] = resolve_secret(
                f"{channel.capitalize()}BotTokenSecret",
                namer.name(f"openclaw/channels/{channel}"),
                f"Bot token for {channel} channel",
                32,
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
            account = Stack.of(self).account
            cloudtrail_bucket_name = namer.name(
                f"openclaw-cloudtrail-{account}-{region}"
            )
            s3_client = boto3.client("s3", region_name=region)
            try:
                s3_client.head_bucket(Bucket=cloudtrail_bucket_name)
                trail_bucket = s3.Bucket.from_bucket_name(
                    self,
                    "CloudTrailBucket",
                    cloudtrail_bucket_name,
                )
                Annotations.of(self).add_info(
                    f"Reusing existing CloudTrail bucket: {cloudtrail_bucket_name}"
                )
            except ClientError as err:
                error_code = str(err.response.get("Error", {}).get("Code", ""))
                status_code = int(err.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
                if error_code in {"404", "NoSuchBucket", "NotFound"} or status_code == 404:
                    trail_bucket = s3.Bucket(
                        self,
                        "CloudTrailBucket",
                        bucket_name=cloudtrail_bucket_name,
                        encryption=s3.BucketEncryption.S3_MANAGED,
                        enforce_ssl=True,
                        block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                        versioned=True,
                        removal_policy=stateful_removal_policy(self),
                        auto_delete_objects=auto_delete_bucket_objects(self),
                    )
                else:
                    raise ValueError(
                        "Failed to determine whether the CloudTrail bucket already exists. "
                        f"Bucket={cloudtrail_bucket_name}. Fix the S3 lookup error: {error_code or status_code}"
                    ) from err
            except (NoCredentialsError, EndpointConnectionError) as err:
                raise ValueError(
                    "Failed to determine whether the CloudTrail bucket already exists because "
                    "AWS credentials or the S3 endpoint are unavailable."
                ) from err

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
        try:
            paginator = cognito_client.get_paginator("list_user_pools")
            existing_user_pool_id = None
            for page in paginator.paginate(MaxResults=60):
                for user_pool in page.get("UserPools", []):
                    if user_pool.get("Name") == user_pool_name:
                        existing_user_pool_id = user_pool["Id"]
                        break
                if existing_user_pool_id:
                    break
        except ClientError as err:
            error_code = str(err.response.get("Error", {}).get("Code", ""))
            raise ValueError(
                "Failed to determine whether the Cognito user pool already exists. "
                f"UserPool={user_pool_name}. Fix the Cognito lookup error: {error_code}"
            ) from err
        except (NoCredentialsError, EndpointConnectionError) as err:
            raise ValueError(
                "Failed to determine whether the Cognito user pool already exists because "
                "AWS credentials or the Cognito endpoint are unavailable."
            ) from err

        user_pool_created = existing_user_pool_id is None
        if user_pool_created:
            self.user_pool = cognito.UserPool(
                self,
                "IdentityPool",
                user_pool_name=user_pool_name,
                self_sign_up_enabled=False,
                sign_in_aliases=cognito.SignInAliases(username=True),
                password_policy=cognito.PasswordPolicy(
                    min_length=16,
                    require_lowercase=False,
                    require_uppercase=False,
                    require_digits=False,
                    require_symbols=False,
                ),
                removal_policy=stateful_removal_policy(self),
                account_recovery=cognito.AccountRecovery.NONE,
            )

            self.user_pool_client = self.user_pool.add_client(
                "ProxyClient",
                user_pool_client_name=user_pool_client_name,
                auth_flows=cognito.AuthFlow(
                    admin_user_password=True,
                ),
                generate_secret=False,
            )
            self.user_pool_id = self.user_pool.user_pool_id
            self.user_pool_client_id = self.user_pool_client.user_pool_client_id
        else:
            Annotations.of(self).add_info(f"Reusing existing Cognito user pool: {user_pool_name}")
            self.user_pool = cognito.UserPool.from_user_pool_id(
                self,
                "IdentityPool",
                user_pool_id=existing_user_pool_id,
            )
            try:
                paginator = cognito_client.get_paginator("list_user_pool_clients")
                existing_client_id = None
                for page in paginator.paginate(
                    UserPoolId=existing_user_pool_id,
                    MaxResults=60,
                ):
                    for user_pool_client in page.get("UserPoolClients", []):
                        if user_pool_client.get("ClientName") == user_pool_client_name:
                            existing_client_id = user_pool_client["ClientId"]
                            break
                    if existing_client_id:
                        break
            except ClientError as err:
                error_code = str(err.response.get("Error", {}).get("Code", ""))
                raise ValueError(
                    "Failed to determine whether the Cognito user pool client already exists. "
                    f"UserPoolClient={user_pool_client_name}. Fix the Cognito lookup error: {error_code}"
                ) from err
            except (NoCredentialsError, EndpointConnectionError) as err:
                raise ValueError(
                    "Failed to determine whether the Cognito user pool client already exists because "
                    "AWS credentials or the Cognito endpoint are unavailable."
                ) from err

            if existing_client_id is None:
                user_pool_client = cognito.CfnUserPoolClient(
                    self,
                    "ProxyClient",
                    user_pool_id=existing_user_pool_id,
                    client_name=user_pool_client_name,
                    explicit_auth_flows=["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
                    generate_secret=False,
                )
                self.user_pool_client_id = user_pool_client.ref
            else:
                Annotations.of(self).add_info(
                    f"Reusing existing Cognito user pool client: {user_pool_client_name}"
                )
                self.user_pool_client_id = existing_client_id

        # Expose Cognito outputs for downstream stacks
        self.user_pool_id = self.user_pool.user_pool_id
        self.cognito_issuer_url = (
            f"https://cognito-idp.{Stack.of(self).region}.amazonaws.com/"
            f"{self.user_pool.user_pool_id}"
        )

        # --- Webhook validation secret (Telegram secret_token, Slack signing) --
        self.webhook_secret = resolve_secret(
            "WebhookSecret",
            webhook_secret_name,
            "Secret token for validating incoming webhook requests "
            "(Telegram X-Telegram-Bot-Api-Secret-Token, Slack signing secret)",
            64,
        )

        # --- HMAC secret for deriving Cognito user passwords -----------------
        self.cognito_password_secret = resolve_secret(
            "CognitoPasswordSecret",
            cognito_password_secret_name,
            "HMAC secret for deriving Cognito user passwords",
            64,
        )

        # --- cdk-nag suppressions ---
        if created_secrets:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                created_secrets,
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
        if user_pool_created:
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
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-COG8",
                        reason="This user pool backs admin-provisioned, non-interactive service "
                        "identities for channel users. Plus tier threat protection adds recurring "
                        "cost but no meaningful security benefit for this programmatic-only flow.",
                    ),
                ],
            )
