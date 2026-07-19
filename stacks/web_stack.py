"""Trusted same-origin consumer web surface and control-plane API."""

from __future__ import annotations

import json
from pathlib import Path
import re

from aws_cdk import (
    AssetHashType,
    CfnOutput,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
    Token,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_cloudwatch as cloudwatch,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_dynamodb as dynamodb,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3_deployment,
    aws_secretsmanager as secretsmanager,
)
import cdk_nag
from constructs import Construct

from stacks import retention_days


REQUIRED_REGION = "eu-west-1"
_SECRET_NAME = re.compile(r"[A-Za-z0-9/_+=.@-]{1,512}")
_USER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}")
_CONNECTION_ID = re.compile(r"[A-Za-z0-9_-]{8,128}")
_EMAIL = re.compile(
    r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
)
ORIGIN_VERIFICATION_HEADER = "X-Personal-Operator-Origin-Verify"
_CLOUDFRONT_WEB_ACL = re.compile(
    r"arn:aws:wafv2:us-east-1:[0-9]{12}:global/webacl/"
    r"[A-Za-z0-9_-]{1,128}/[0-9a-fA-F-]{36}"
)


class WebStack(Stack):
    """Private static UI plus a narrowly routed trusted control-plane Lambda.

    The CloudFront distribution is the browser's only application origin. Its
    three dynamic path families proxy to an HTTP API with nine explicit
    method/path pairs. The AgentCore runtime endpoint is never an origin or a
    route on this distribution.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cmk_arn: str,
        runtime_state_table: dynamodb.ITable,
        capability_state_table: dynamodb.ITable,
        identity_table: dynamodb.ITable | None = None,
        message_ledger_table: dynamodb.ITable | None = None,
        user_files_bucket: s3.IBucket,
        runtime_arn: str,
        runtime_iam_arn: str,
        runtime_endpoint_name: str,
        trusted_code_asset_root: str,
        trusted_code_asset_hash: str | None = None,
        web_asset_root: str,
        control_table: dynamodb.ITable | None = None,
        auth_secret: secretsmanager.ISecret | None = None,
        approval_secret: secretsmanager.ISecret | None = None,
        origin_verification_secret: secretsmanager.ISecret | None = None,
        google_readonly_oauth_secret: secretsmanager.ISecret | None = None,
        google_send_oauth_secret: secretsmanager.ISecret | None = None,
        openai_api_key_secret: secretsmanager.ISecret | None = None,
        auth_secret_name: str = "personal-operator/web-auth",
        approval_secret_name: str = "personal-operator/approval-signing",
        origin_verification_secret_name: str = (
            "personal-operator/cloudfront-origin-verification"
        ),
        google_readonly_oauth_secret_name: str = "personal-operator/google-readonly-oauth",
        google_send_oauth_secret_name: str = "personal-operator/google-send-oauth",
        openai_api_key_secret_name: str = "personal-operator/openai-api-key",
        founder_user_ids: str = "",
        gmail_send_connection_id: str = "",
        gmail_send_account_email: str = "",
        web_acl_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        def trusted_lambda_code() -> _lambda.Code:
            if trusted_code_asset_hash is None:
                return _lambda.Code.from_asset(trusted_code_asset_root)
            if re.fullmatch(r"[0-9a-f]{64}", trusted_code_asset_hash) is None:
                raise ValueError("trusted Lambda asset hash is not canonical")
            return _lambda.Code.from_asset(
                trusted_code_asset_root,
                asset_hash=trusted_code_asset_hash,
                asset_hash_type=AssetHashType.CUSTOM,
            )

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"WebStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        if (
            runtime_state_table is None
            or capability_state_table is None
            or identity_table is None
            or message_ledger_table is None
            or user_files_bucket is None
        ):
            raise ValueError(
                "runtime, capability, identity, message-ledger, and user-files stores are required"
            )
        if not isinstance(cmk_arn, str) or not cmk_arn:
            raise ValueError("cmk_arn is required")
        runtime_values = (runtime_arn, runtime_iam_arn, runtime_endpoint_name)
        if runtime_values == ("PLACEHOLDER", "PLACEHOLDER", "PLACEHOLDER"):
            pass
        else:
            if "PLACEHOLDER" in runtime_values:
                raise ValueError("runtime placeholders must be configured together")
            runtime_id_pattern = r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}"
            invocation_pattern = (
                rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
                rf"{re.escape(account)}:agent/"
                r"[A-Fa-f0-9]{8}-[A-Fa-f0-9]{4}-[A-Fa-f0-9]{4}-"
                r"[A-Fa-f0-9]{4}-[A-Fa-f0-9]{12}:[1-9][0-9]{0,4}"
            )
            runtime_iam_pattern = (
                rf"arn:aws:bedrock-agentcore:{re.escape(region)}:"
                rf"{re.escape(account)}:runtime/{runtime_id_pattern}"
            )
            if re.fullmatch(invocation_pattern, runtime_arn) is None:
                raise ValueError("runtime_arn must be an exact versioned invocation ARN")
            if re.fullmatch(runtime_iam_pattern, runtime_iam_arn) is None:
                raise ValueError("runtime_iam_arn must be an exact runtime resource ARN")
            if re.fullmatch(r"release_[0-9a-f]{40}", runtime_endpoint_name) is None:
                raise ValueError("runtime endpoint must be an exact release endpoint")
        for label, value in (
            ("auth_secret_name", auth_secret_name),
            ("approval_secret_name", approval_secret_name),
            ("origin_verification_secret_name", origin_verification_secret_name),
            ("google_readonly_oauth_secret_name", google_readonly_oauth_secret_name),
            ("google_send_oauth_secret_name", google_send_oauth_secret_name),
            ("openai_api_key_secret_name", openai_api_key_secret_name),
        ):
            if not isinstance(value, str) or _SECRET_NAME.fullmatch(value) is None:
                raise ValueError(f"{label} is invalid")
        if not isinstance(founder_user_ids, str) or len(founder_user_ids) > 4_096:
            raise ValueError("founder_user_ids is invalid")
        founder_ids = [
            item.strip() for item in founder_user_ids.split(",") if item.strip()
        ]
        if founder_ids and (
            len(founder_ids) != 1 or _USER_ID.fullmatch(founder_ids[0]) is None
        ):
            raise ValueError("exactly one valid founder identity is required")
        if any((gmail_send_connection_id, gmail_send_account_email)) and not founder_ids:
            raise ValueError("Gmail send binding requires an exact founder identity")
        if founder_ids and (
            _CONNECTION_ID.fullmatch(gmail_send_connection_id or "") is None
            or _EMAIL.fullmatch(gmail_send_account_email or "") is None
        ):
            raise ValueError(
                "founder configuration requires an exact Gmail connection and account"
            )
        founder_user_id = founder_ids[0] if founder_ids else ""
        if (
            web_acl_id
            and not Token.is_unresolved(web_acl_id)
            and _CLOUDFRONT_WEB_ACL.fullmatch(web_acl_id) is None
        ):
            raise ValueError(
                "CloudFront Web ACL must be a global us-east-1 WAFv2 ARN"
            )

        log_retention = int(
            self.node.try_get_context("cloudwatch_log_retention_days") or "30"
        )
        cmk = kms.Key.from_key_arn(self, "ImportedControlPlaneCmk", cmk_arn)

        # One composite-key table holds bounded sessions, one-time tickets,
        # OAuth state/envelopes, derived Gmail records, actions, and receipts.
        # Tombstones deliberately omit TTL; every expiring record carries ttl.
        if control_table is None:
            self.control_table = dynamodb.Table(
                self,
                "WebControlTable",
                table_name="personal-operator-control",
                partition_key=dynamodb.Attribute(
                    name="PK", type=dynamodb.AttributeType.STRING
                ),
                sort_key=dynamodb.Attribute(
                    name="SK", type=dynamodb.AttributeType.STRING
                ),
                billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                removal_policy=RemovalPolicy.RETAIN,
                time_to_live_attribute="ttl",
                point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                ),
                encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
                encryption_key=cmk,
            )
        else:
            self.control_table = control_table
        if isinstance(self.control_table, dynamodb.Table):
            self.control_table.add_global_secondary_index(
                index_name="userId-index",
                partition_key=dynamodb.Attribute(
                    name="userId", type=dynamodb.AttributeType.STRING
                ),
                projection_type=dynamodb.ProjectionType.ALL,
            )

        # The browser/session HMAC key is created ready for use. The Google
        # secret starts as unstructured random data, not credential JSON: a
        # staging preflight must replace and validate its exact clientId and
        # clientSecret object before OAuth can be enabled.
        owned_secrets: list[secretsmanager.Secret] = []

        def generated_signing_secret(
            construct_name: str, *, name: str, description: str
        ) -> secretsmanager.Secret:
            value = secretsmanager.Secret(
                self,
                construct_name,
                secret_name=name,
                description=description,
                encryption_key=cmk,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=64,
                    exclude_punctuation=True,
                    include_space=False,
                    require_each_included_type=False,
                ),
            )
            owned_secrets.append(value)
            return value

        def provider_placeholder(
            construct_name: str,
            *,
            name: str,
            description: str,
            fields: dict[str, str],
        ) -> secretsmanager.Secret:
            value = secretsmanager.Secret(
                self,
                construct_name,
                secret_name=name,
                description=description,
                encryption_key=cmk,
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    secret_string_template=json.dumps(fields),
                    generate_string_key="bootstrap_nonce",
                    password_length=32,
                    exclude_punctuation=True,
                ),
            )
            owned_secrets.append(value)
            return value

        self.auth_secret = auth_secret or generated_signing_secret(
            "WebAuthSecret",
            name=auth_secret_name,
            description="HMAC key for one-time connect tickets and opaque sessions",
        )
        self.approval_secret = approval_secret or generated_signing_secret(
            "ApprovalSigningSecret",
            name=approval_secret_name,
            description="HMAC key for exact-payload founder approvals",
        )
        self.origin_verification_secret = (
            origin_verification_secret
            or generated_signing_secret(
                "OriginVerificationSecret",
                name=origin_verification_secret_name,
                description=(
                    "CloudFront-to-HTTP-API origin proof; never accepted from a viewer"
                ),
            )
        )
        self.google_readonly_oauth_secret = (
            google_readonly_oauth_secret
            or provider_placeholder(
                "GoogleReadonlyOAuthSecret",
                name=google_readonly_oauth_secret_name,
                description="Google OAuth client for the Gmail read-only pilot",
                fields={"client_id": "REPLACE_ME", "client_secret": "REPLACE_ME"},
            )
        )
        self.google_send_oauth_secret = google_send_oauth_secret or provider_placeholder(
            "GoogleSendOAuthSecret",
            name=google_send_oauth_secret_name,
            description="Separate founder-only Gmail send OAuth connection",
            fields={
                "client_id": "REPLACE_ME",
                "client_secret": "REPLACE_ME",
                "refresh_token": "REPLACE_ME",
                "email": "REPLACE_ME",
                "connection_id": "REPLACE_ME",
                "user_id": "REPLACE_ME",
            },
        )
        self.openai_api_key_secret = openai_api_key_secret or provider_placeholder(
            "OpenAiApiKeySecret",
            name=openai_api_key_secret_name,
            description="OpenAI API key for non-retained Gmail opportunity ranking",
            fields={"api_key": "REPLACE_ME"},
        )

        self.control_log_bucket = s3.Bucket(
            self,
            "WebAccessLogBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
            removal_policy=RemovalPolicy.RETAIN,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-control-plane-access-logs",
                    expiration=Duration.days(90),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
        )
        # SSE-S3 is deliberate here: CloudFront OAC can read it without
        # widening the existing cross-stack CMK policy to distribution/*.
        # Trust-bearing control data and secrets remain on the supplied CMK.
        self.web_assets_bucket = s3.Bucket(
            self,
            "WebAssetsBucket",
            bucket_name=f"personal-operator-web-{account}-{region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            server_access_logs_bucket=self.control_log_bucket,
            server_access_logs_prefix="s3/web-assets/",
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-noncurrent-web-assets",
                    noncurrent_version_expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
        )

        web_log_group = logs.LogGroup(
            self,
            "WebLambdaLogGroup",
            log_group_name="/personal-operator/lambda/web",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.RETAIN,
        )
        control_log_group = logs.LogGroup(
            self,
            "ControlCommandLogGroup",
            log_group_name="/personal-operator/lambda/control-command",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.RETAIN,
        )
        maintenance_log_group = logs.LogGroup(
            self,
            "MaintenanceLogGroup",
            log_group_name="/personal-operator/lambda/maintenance",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.RETAIN,
        )
        api_access_log_group = logs.LogGroup(
            self,
            "WebApiAccessLogGroup",
            log_group_name="/personal-operator/api/web-access",
            retention=retention_days(log_retention),
            removal_policy=RemovalPolicy.RETAIN,
        )

        web_environment = {
            "AWS_REGION_LOCK": REQUIRED_REGION,
            "CONTROL_TABLE_NAME": self.control_table.table_name,
            "RUNTIME_STATE_TABLE_NAME": runtime_state_table.table_name,
            "CAPABILITY_STATE_TABLE_NAME": capability_state_table.table_name,
            "IDENTITY_TABLE_NAME": identity_table.table_name,
            "MESSAGE_LEDGER_TABLE_NAME": message_ledger_table.table_name,
            "USER_FILES_BUCKET_NAME": user_files_bucket.bucket_name,
            "AGENTCORE_RUNTIME_ARN": runtime_arn,
            "AGENTCORE_QUALIFIER": runtime_endpoint_name,
            "WEB_AUTH_SECRET_ID": self.auth_secret.secret_name,
            "APPROVAL_SIGNING_SECRET_ID": self.approval_secret.secret_name,
            "ORIGIN_VERIFICATION_SECRET_ID": (
                self.origin_verification_secret.secret_name
            ),
            "GOOGLE_READONLY_OAUTH_SECRET_ID": self.google_readonly_oauth_secret.secret_name,
            "GOOGLE_SEND_OAUTH_SECRET_ID": self.google_send_oauth_secret.secret_name,
            "OAUTH_KMS_KEY_ID": cmk_arn,
            "FOUNDER_USER_IDS": founder_user_id,
            "DERIVED_RECORD_TTL_DAYS": "14",
            "SESSION_TTL_SECONDS": "86400",
        }
        self.web_fn = _lambda.Function(
            self,
            "WebApiFunction",
            function_name="personal-operator-web-api",
            description="Trusted Personal Operator browser control-plane boundary",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="web.index.lambda_handler",
            # auth, actions, workflows, retention, and RuntimeDriver are one
            # reviewed trusted Lambda asset rooted at lambda/.
            code=trusted_lambda_code(),
            timeout=Duration.seconds(30),
            memory_size=512,
            reserved_concurrent_executions=20,
            tracing=_lambda.Tracing.ACTIVE,
            environment_encryption=cmk,
            environment=web_environment,
            log_group=web_log_group,
        )
        self.maintenance_fn = _lambda.Function(
            self,
            "MaintenanceFunction",
            function_name="personal-operator-maintenance",
            description="Bounded action recovery, retention, and deletion finalization",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="web.index.lambda_handler",
            code=trusted_lambda_code(),
            timeout=Duration.seconds(900),
            memory_size=512,
            reserved_concurrent_executions=1,
            tracing=_lambda.Tracing.ACTIVE,
            environment_encryption=cmk,
            environment=web_environment,
            role=self.web_fn.role,
            log_group=maintenance_log_group,
        )
        self.control_fn = _lambda.Function(
            self,
            "ControlCommandFunction",
            function_name="personal-operator-control-command",
            description="Trusted Telegram product-command control boundary",
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="control.index.lambda_handler",
            code=trusted_lambda_code(),
            timeout=Duration.seconds(180),
            memory_size=512,
            reserved_concurrent_executions=20,
            tracing=_lambda.Tracing.ACTIVE,
            environment_encryption=cmk,
            environment={
                "AWS_REGION_LOCK": REQUIRED_REGION,
                "CONTROL_TABLE_NAME": self.control_table.table_name,
                "WEB_AUTH_SECRET_ID": self.auth_secret.secret_name,
                "GOOGLE_READONLY_OAUTH_SECRET_ID": self.google_readonly_oauth_secret.secret_name,
                "OPENAI_API_KEY_SECRET_ID": self.openai_api_key_secret.secret_name,
                "APPROVAL_SIGNING_SECRET_ID": self.approval_secret.secret_name,
                "OAUTH_KMS_KEY_ID": cmk_arn,
                "OPENAI_RANKER_MODEL": "gpt-5-mini",
                "FOUNDER_USER_IDS": founder_user_id,
                "GMAIL_SEND_CONNECTION_ID": gmail_send_connection_id,
                "GMAIL_SEND_ACCOUNT_EMAIL": gmail_send_account_email,
            },
            log_group=control_log_group,
        )
        for alarm_id, alarm_name, metric in (
            (
                "WebLambdaErrorsAlarm",
                "personal-operator-web-errors",
                self.web_fn.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            ),
            (
                "WebLambdaThrottlesAlarm",
                "personal-operator-web-throttles",
                self.web_fn.metric_throttles(period=Duration.minutes(5), statistic="Sum"),
            ),
            (
                "ControlLambdaErrorsAlarm",
                "personal-operator-control-errors",
                self.control_fn.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            ),
            (
                "ControlLambdaThrottlesAlarm",
                "personal-operator-control-throttles",
                self.control_fn.metric_throttles(period=Duration.minutes(5), statistic="Sum"),
            ),
            (
                "MaintenanceLambdaErrorsAlarm",
                "personal-operator-maintenance-errors",
                self.maintenance_fn.metric_errors(period=Duration.minutes(5), statistic="Sum"),
            ),
            (
                "MaintenanceLambdaThrottlesAlarm",
                "personal-operator-maintenance-throttles",
                self.maintenance_fn.metric_throttles(period=Duration.minutes(5), statistic="Sum"),
            ),
        ):
            cloudwatch.Alarm(
                self,
                alarm_id,
                alarm_name=alarm_name,
                metric=metric,
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )

        integration = apigwv2_integrations.HttpLambdaIntegration(
            "WebLambdaIntegration", handler=self.web_fn
        )
        self.http_api = apigwv2.HttpApi(
            self,
            "WebHttpApi",
            api_name="personal-operator-web",
            description="Explicit trusted browser control-plane routes only",
        )
        route_contract = (
            ("/api/session/connect", apigwv2.HttpMethod.POST),
            ("/api/session/logout", apigwv2.HttpMethod.POST),
            ("/oauth/google/start", apigwv2.HttpMethod.GET),
            ("/oauth/google/callback", apigwv2.HttpMethod.GET),
            ("/approve/{token}", apigwv2.HttpMethod.GET),
            ("/api/actions/{id}/approve", apigwv2.HttpMethod.POST),
            ("/api/actions/{id}/reject", apigwv2.HttpMethod.POST),
            ("/api/gmail", apigwv2.HttpMethod.GET),
            ("/api/gmail/drafts/{action}", apigwv2.HttpMethod.POST),
            ("/api/workspace", apigwv2.HttpMethod.GET),
            ("/api/overview", apigwv2.HttpMethod.GET),
            ("/api/export", apigwv2.HttpMethod.GET),
            ("/api/import/plan", apigwv2.HttpMethod.POST),
            ("/api/import/activate", apigwv2.HttpMethod.POST),
            ("/api/delete", apigwv2.HttpMethod.POST),
            (
                "/api/connections/google-gmail-readonly/disconnect",
                apigwv2.HttpMethod.POST,
            ),
            ("/api/scans/{scan}/feedback", apigwv2.HttpMethod.POST),
        )
        for route_path, method in route_contract:
            self.http_api.add_routes(
                path=route_path, methods=[method], integration=integration
            )

        self.retention_rule = events.Rule(
            self,
            "HourlyMaintenanceSweep",
            rule_name="personal-operator-hourly-maintenance",
            description="Expire bounded control records and resume pending deletion",
            enabled=True,
            schedule=events.Schedule.rate(Duration.hours(1)),
            targets=[
                events_targets.LambdaFunction(
                    self.maintenance_fn,
                    event=events.RuleTargetInput.from_object(
                        {
                            "detail-type": "ScheduledRetentionSweep",
                            "source": "personal-operator.retention",
                            "version": 1,
                        }
                    ),
                    max_event_age=Duration.hours(1),
                    retry_attempts=2,
                )
            ],
        )

        default_stage = self.http_api.default_stage
        if default_stage is None:
            raise RuntimeError("web HTTP API must have its explicit default stage")
        cfn_stage = default_stage.node.default_child
        cfn_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            detailed_metrics_enabled=True,
            throttling_burst_limit=20,
            throttling_rate_limit=10,
        )
        cfn_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=api_access_log_group.log_group_arn,
            format=(
                '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp",'
                '"routeKey":"$context.routeKey",'
                '"status":"$context.status","latency":"$context.responseLatency",'
                '"responseLength":"$context.responseLength"}'
            ),
        )

        security_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "WebSecurityHeaders",
            response_headers_policy_name="personal-operator-security-headers",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
                        "font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                        "img-src 'self' data:; object-src 'none'; "
                        "script-src 'self'; style-src 'self'"
                    ),
                    override=True,
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                    override=True
                ),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY,
                    override=True,
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER,
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365),
                    include_subdomains=True,
                    preload=True,
                    override=True,
                ),
                xss_protection=cloudfront.ResponseHeadersXSSProtection(
                    protection=True, mode_block=True, override=True
                ),
            ),
        )
        app_shell_path = Path(web_asset_root) / "index.html"
        if not app_shell_path.is_file() or app_shell_path.is_symlink():
            raise ValueError("web asset root must contain a regular index.html")
        app_shell = app_shell_path.read_text(encoding="utf-8")
        app_shell_literal = json.dumps(app_shell, ensure_ascii=True)
        edge_router_source = f"""function handler(event) {{
    var request = event.request;
    var accept = request.headers.accept ? request.headers.accept.value.toLowerCase() : '';
    if (request.uri.indexOf('/approve/') === 0) {{
        if (accept.indexOf('text/html') !== -1 && accept.indexOf('application/json') === -1) {{
            return {{
                statusCode: 200,
                statusDescription: 'OK',
                headers: {{
                    'content-type': {{ value: 'text/html; charset=utf-8' }},
                    'cache-control': {{ value: 'no-store, max-age=0' }},
                    'content-security-policy': {{ value: "default-src 'self'; base-uri 'none'; connect-src 'self'; font-src 'self'; form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; script-src 'self'; style-src 'self'" }},
                    'strict-transport-security': {{ value: 'max-age=31536000; includeSubDomains; preload' }},
                    'x-content-type-options': {{ value: 'nosniff' }},
                    'x-frame-options': {{ value: 'DENY' }},
                    'referrer-policy': {{ value: 'no-referrer' }}
                }},
                body: {{ encoding: 'text', data: {app_shell_literal} }}
            }};
        }}
        return request;
    }}
    var leaf = request.uri.substring(request.uri.lastIndexOf('/') + 1);
    if (request.uri === '/' || request.uri.endsWith('/') || leaf.indexOf('.') === -1) {{
        request.uri = '/index.html';
    }}
    return request;
}}"""
        if len(edge_router_source.encode("utf-8")) > 10_000:
            raise ValueError("web app shell exceeds the CloudFront Function size limit")
        self.edge_router = cloudfront.Function(
            self,
            "WebEdgeRouter",
            function_name="personal-operator-edge-router",
            comment="SPA fallback and content-negotiated approval navigation",
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            code=cloudfront.FunctionCode.from_inline(edge_router_source),
            auto_publish=True,
        )
        edge_association = cloudfront.FunctionAssociation(
            event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
            function=self.edge_router,
        )
        static_origin = origins.S3BucketOrigin.with_origin_access_control(
            self.web_assets_bucket
        )
        api_domain = Fn.select(2, Fn.split("/", self.http_api.api_endpoint))
        api_origin = origins.HttpOrigin(
            api_domain,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
            origin_ssl_protocols=[cloudfront.OriginSslPolicy.TLS_V1_2],
            # CloudFront strips a same-named viewer header and supplies this
            # deployment-resolved value at the origin. The template contains
            # only the Secrets Manager dynamic reference, never the secret.
            custom_headers={
                ORIGIN_VERIFICATION_HEADER: (
                    self.origin_verification_secret.secret_value.unsafe_unwrap()
                )
            },
        )
        def api_behavior(*, approval_navigation: bool = False):
            return cloudfront.BehaviorOptions(
                origin=api_origin,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                response_headers_policy=security_headers,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                compress=True,
                function_associations=(
                    [edge_association] if approval_navigation else None
                ),
            )

        self.distribution = cloudfront.Distribution(
            self,
            "WebDistribution",
            comment="Personal Operator trusted consumer web origin",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=static_origin,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=security_headers,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                compress=True,
                function_associations=[edge_association],
            ),
            additional_behaviors={
                "api/*": api_behavior(),
                "oauth/*": api_behavior(),
                "approve/*": api_behavior(approval_navigation=True),
            },
            enable_ipv6=True,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            publish_additional_metrics=True,
            web_acl_id=web_acl_id,
        )
        web_origin = f"https://{self.distribution.distribution_domain_name}"
        google_redirect_uri = f"{web_origin}/oauth/google/callback"
        self.web_fn.add_environment("WEB_ORIGIN", web_origin)
        self.web_fn.add_environment("GOOGLE_REDIRECT_URI", google_redirect_uri)
        self.maintenance_fn.add_environment("WEB_ORIGIN", web_origin)
        self.maintenance_fn.add_environment(
            "GOOGLE_REDIRECT_URI", google_redirect_uri
        )
        self.control_fn.add_environment("WEB_ORIGIN", web_origin)
        self.control_alias = _lambda.Alias(
            self,
            "ControlCommandLiveAlias",
            alias_name="live",
            version=self.control_fn.current_version,
        )

        self.web_deployment = s3_deployment.BucketDeployment(
            self,
            "WebDistDeployment",
            destination_bucket=self.web_assets_bucket,
            sources=[s3_deployment.Source.asset(web_asset_root)],
            distribution=self.distribution,
            distribution_paths=["/*"],
            prune=True,
            retain_on_delete=False,
        )

        # Composite-table operations remain exact-table scoped. Runtime state
        # can be tombstoned but never deleted, and the web plane can stop but
        # never invoke an AgentCore runtime.
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[capability_state_table.table_arn],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:BatchGetItem",
                    "dynamodb:BatchWriteItem",
                    "dynamodb:TransactWriteItems",
                ],
                resources=[
                    self.control_table.table_arn,
                    f"{self.control_table.table_arn}/index/userId-index",
                ],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                ],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Scan",
                ],
                resources=[runtime_state_table.table_arn],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query", "dynamodb:DeleteItem"],
                resources=[
                    identity_table.table_arn,
                    f"{identity_table.table_arn}/index/userId-index",
                    message_ledger_table.table_arn,
                    f"{message_ledger_table.table_arn}/index/userId-index",
                ],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:TransactWriteItems",
                ],
                resources=[identity_table.table_arn],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:ListBucket",
                    "s3:ListBucketVersions",
                    "s3:ListBucketMultipartUploads",
                ],
                resources=[user_files_bucket.bucket_arn],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "s3:GetObject",
                    "s3:GetObjectVersion",
                    "s3:DeleteObject",
                    "s3:DeleteObjectVersion",
                    "s3:AbortMultipartUpload",
                ],
                resources=[user_files_bucket.arn_for_objects("*")],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:PutObject"],
                resources=[
                    user_files_bucket.arn_for_objects(
                        "*/.system/portable/v2/*"
                    )
                ],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"s3.{region}.amazonaws.com",
                    }
                },
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:StopRuntimeSession"],
                resources=[
                    runtime_iam_arn,
                    f"{runtime_iam_arn}/runtime-endpoint/{runtime_endpoint_name}",
                ],
            )
        )
        def allow_secret_read(function: _lambda.Function, secret) -> None:
            # Keep the grant entirely on the consumer role. Calling
            # Secret.grant_read across stacks can mutate the producer key/secret
            # policy and create a Security<->Web CloudFormation cycle.
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret",
                    ],
                    resources=[secret.secret_arn],
                )
            )

        def allow_secret_value_read(function: _lambda.Function, secret) -> None:
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[secret.secret_arn],
                )
            )

        allow_secret_read(self.web_fn, self.auth_secret)
        allow_secret_read(self.web_fn, self.approval_secret)
        allow_secret_read(self.web_fn, self.origin_verification_secret)
        allow_secret_read(self.web_fn, self.google_readonly_oauth_secret)
        allow_secret_read(self.web_fn, self.google_send_oauth_secret)
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:DeleteSecret",
                ],
                resources=[self.google_send_oauth_secret.secret_arn],
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:EncryptionContext:application": "personal-operator",
                    },
                    "ForAnyValue:StringEquals": {
                        "kms:EncryptionContext:provider": [
                            "google-gmail-readonly",
                            "google-gmail-send",
                        ],
                    }
                },
            )
        )
        self.web_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {"kms:CallerAccount": account},
                    "ForAnyValue:StringEquals": {
                        "kms:ViaService": [
                            f"s3.{region}.amazonaws.com",
                            f"secretsmanager.{region}.amazonaws.com",
                        ]
                    },
                },
            )
        )

        self.control_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:DeleteItem",
                    "dynamodb:Query",
                ],
                resources=[
                    self.control_table.table_arn,
                    f"{self.control_table.table_arn}/index/userId-index",
                ],
            )
        )
        self.control_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "kms:Decrypt",
                    "kms:DescribeKey",
                    "kms:Encrypt",
                    "kms:GenerateDataKey*",
                    "kms:ReEncrypt*",
                ],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )
        allow_secret_read(self.control_fn, self.auth_secret)
        allow_secret_value_read(self.control_fn, self.approval_secret)
        allow_secret_read(self.control_fn, self.google_readonly_oauth_secret)
        allow_secret_read(self.control_fn, self.openai_api_key_secret)
        self.control_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:GenerateDataKey", "kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:EncryptionContext:application": "personal-operator",
                        "kms:EncryptionContext:provider": "google-gmail-readonly",
                    }
                },
            )
        )
        self.control_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Decrypt"],
                resources=[cmk_arn],
                conditions={
                    "StringEquals": {
                        "kms:CallerAccount": account,
                        "kms:ViaService": f"secretsmanager.{region}.amazonaws.com",
                    }
                },
            )
        )

        CfnOutput(
            self,
            "WebUrl",
            value=web_origin,
            description="Single trusted browser origin",
        )
        CfnOutput(
            self,
            "GoogleOAuthRedirectUri",
            value=google_redirect_uri,
            description="Register this exact callback in the Google OAuth client",
        )
        CfnOutput(self, "WebControlTableName", value=self.control_table.table_name)
        CfnOutput(self, "WebAssetsBucketName", value=self.web_assets_bucket.bucket_name)
        CfnOutput(self, "WebAuthSecretName", value=self.auth_secret.secret_name)
        CfnOutput(
            self, "ApprovalSigningSecretName", value=self.approval_secret.secret_name
        )
        CfnOutput(
            self,
            "GoogleReadonlyOAuthSecretName",
            value=self.google_readonly_oauth_secret.secret_name,
        )
        CfnOutput(
            self,
            "ControlCommandAliasArn",
            value=self.control_alias.function_arn,
        )
        CfnOutput(
            self,
            "WebDistributionId",
            value=self.distribution.distribution_id,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            [self.web_fn, self.maintenance_fn, self.control_fn],
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason="Lambda basic execution is the AWS-managed log delivery policy.",
                    applies_to=[
                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "User IDs are application-authenticated, so object deletion/export "
                        "requires the exact existing bucket plus its namespace wildcard; "
                        "Secrets Manager appends an unknown six-character suffix; the "
                        "CMK policy uses only AWS-defined GenerateDataKey/ReEncrypt action "
                        "families constrained to this account and DynamoDB service."
                    ),
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason="Python 3.13 is the current stable Lambda runtime in eu-west-1.",
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.http_api,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG4",
                    reason=(
                        "The trusted Lambda enforces opaque sessions, CSRF, exact Origin, "
                        "one-time tokens, and matching-user checks; the connect route must "
                        "remain reachable before a browser session exists."
                    ),
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-APIG1",
                    reason=(
                        "Access logging is configured directly on the HTTP API CfnStage; "
                        "cdk-nag cannot observe the L1 escape hatch."
                    ),
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.control_log_bucket,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-S1",
                    reason=(
                        "This is the terminal S3 and CloudFront access-log bucket; "
                        "logging it again would create a recursive logging chain."
                    ),
                )
            ],
        )
        if owned_secrets:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                owned_secrets,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-SMG4",
                        reason=(
                            "Automatic rotation would invalidate active browser sessions and "
                            "approval links, while provider-secret rotation requires a "
                            "coordinated external change. The pilot uses an audited manual "
                            "rotation runbook."
                        ),
                    )
                ],
            )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.distribution,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR1",
                    reason=(
                        "The invite-only consumer surface is intentionally available to "
                        "pilots while travelling; authentication, not geography, is the "
                        "authorization boundary."
                    ),
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR4",
                    reason=(
                        "The generated CloudFront hostname uses the CloudFront default "
                        "certificate, for which AWS does not expose a configurable viewer "
                        "TLS floor. Every behavior redirects HTTP to HTTPS; a custom-domain "
                        "certificate is required before a strict viewer TLS policy can be set."
                    ),
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR3",
                    reason=(
                        "CloudFront standard logs persist the literal URI and would leak "
                        "bearer approval tokens from /approve/{token}. The HTTP API instead "
                        "logs only its route template, status, latency, and request ID; the "
                        "static S3 origin retains server access logs separately."
                    ),
                ),
            ],
        )
        if not web_acl_id:
            cdk_nag.NagSuppressions.add_resource_suppressions(
                self.distribution,
                [
                    cdk_nag.NagPackSuppression(
                        id="AwsSolutions-CFR2",
                        reason=(
                            "A CloudFront-scope Web ACL must be provisioned in us-east-1. "
                            "This eu-west-1 stack accepts an audited global Web ACL ARN; "
                            "API throttling remains mandatory when it is absent."
                        ),
                    )
                ],
            )

        # BucketDeployment owns one CDK singleton provider at stack scope rather
        # than below the deployment construct. Its generated AWS-managed basic
        # role, object-prefix wildcards, and provider runtime are controlled by
        # the pinned CDK library rather than application code.
        for child in self.node.children:
            if child.node.id.startswith("Custom::CDKBucketDeployment"):
                cdk_nag.NagSuppressions.add_resource_suppressions(
                    child,
                    [
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM4",
                            reason=(
                                "CDK's singleton BucketDeployment provider uses the AWS "
                                "Lambda basic execution policy solely for log delivery."
                            ),
                            applies_to=[
                                "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
                            ],
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-IAM5",
                            reason=(
                                "CDK generates object-prefix permissions for its immutable "
                                "asset source, exact destination bucket, and CloudFront "
                                "invalidation API; the provider is not reachable by users."
                            ),
                        ),
                        cdk_nag.NagPackSuppression(
                            id="AwsSolutions-L1",
                            reason=(
                                "The singleton provider runtime is selected by the pinned "
                                "aws-cdk-lib implementation and contains no product logic."
                            ),
                        ),
                    ],
                    apply_to_children=True,
                )
