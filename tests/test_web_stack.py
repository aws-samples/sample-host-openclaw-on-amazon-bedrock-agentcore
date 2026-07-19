"""Deployment contracts for the trusted consumer web control surface."""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_s3 as s3,
)
import pytest

from stacks.web_stack import WebStack


ROOT = Path(__file__).resolve().parents[1]
ENV = cdk.Environment(account="123456789012", region="eu-west-1")
RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "agent/12345678-1234-1234-1234-123456789abc:1"
)
RUNTIME_IAM_ARN = (
    "arn:aws:bedrock-agentcore:eu-west-1:123456789012:"
    "runtime/personal_operator-0123456789"
)
RELEASE_ENDPOINT = "release_" + "a" * 40


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == resource_type
    ]


def _logical_resource(template: dict, prefix: str) -> dict:
    return next(
        resource
        for logical_id, resource in template["Resources"].items()
        if logical_id.startswith(prefix)
    )


def _flatten_actions(statements: list[dict]) -> set[str]:
    actions: set[str] = set()
    for statement in statements:
        value = statement.get("Action", [])
        actions.update([value] if isinstance(value, str) else value)
    return actions


def _web_statements(template: dict) -> list[dict]:
    function = next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName")
        == "personal-operator-web-api"
    )
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    statements: list[dict] = []
    for resource in _resources(template, "AWS::IAM::Policy"):
        if {"Ref": role_id} in resource["Properties"].get("Roles", []):
            statements.extend(
                resource["Properties"]["PolicyDocument"].get("Statement", [])
            )
    return statements


def _statements_for_function(template: dict, function_name: str) -> list[dict]:
    function = next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName") == function_name
    )
    role_id = function["Properties"]["Role"]["Fn::GetAtt"][0]
    return [
        statement
        for resource in _resources(template, "AWS::IAM::Policy")
        if {"Ref": role_id} in resource["Properties"].get("Roles", [])
        for statement in resource["Properties"]["PolicyDocument"].get("Statement", [])
    ]


def _synth_web_template(
    *,
    web_acl_id: str | None = None,
    founder_user_ids: str = "",
    gmail_send_connection_id: str = "",
    gmail_send_account_email: str = "",
) -> dict:
    app = cdk.App()
    resources = cdk.Stack(app, "WebTestResources", env=ENV)
    cmk = kms.Key(resources, "Cmk")
    runtime_table = dynamodb.Table(
        resources,
        "RuntimeState",
        partition_key=dynamodb.Attribute(
            name="userId", type=dynamodb.AttributeType.STRING
        ),
        encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=cmk,
    )
    identity_table = dynamodb.Table(
        resources,
        "Identity",
        table_name="openclaw-identity",
        partition_key=dynamodb.Attribute(
            name="PK", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(
            name="SK", type=dynamodb.AttributeType.STRING
        ),
    )
    message_ledger_table = dynamodb.Table(
        resources,
        "MessageLedger",
        table_name="personal-operator-message-ledger",
        partition_key=dynamodb.Attribute(
            name="eventId", type=dynamodb.AttributeType.STRING
        ),
    )
    capability_state_table = dynamodb.Table(
        resources,
        "CapabilityState",
        table_name="personal-operator-capability-state",
        partition_key=dynamodb.Attribute(
            name="PK", type=dynamodb.AttributeType.STRING
        ),
        sort_key=dynamodb.Attribute(
            name="SK", type=dynamodb.AttributeType.STRING
        ),
        encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryption_key=cmk,
    )
    user_files_bucket = s3.Bucket(
        resources,
        "UserFiles",
        encryption=s3.BucketEncryption.KMS,
        encryption_key=cmk,
    )
    stack = WebStack(
        app,
        "PersonalOperatorWeb",
        cmk_arn=cmk.key_arn,
        runtime_state_table=runtime_table,
        capability_state_table=capability_state_table,
        identity_table=identity_table,
        message_ledger_table=message_ledger_table,
        user_files_bucket=user_files_bucket,
        runtime_arn=RUNTIME_ARN,
        runtime_iam_arn=RUNTIME_IAM_ARN,
        runtime_endpoint_name=RELEASE_ENDPOINT,
        trusted_code_asset_root="lambda",
        web_asset_root="tests/fixtures/web-dist",
        founder_user_ids=founder_user_ids,
        gmail_send_connection_id=gmail_send_connection_id,
        gmail_send_account_email=gmail_send_account_email,
        web_acl_id=web_acl_id,
        env=ENV,
    )
    return app.synth().get_stack_by_name(stack.stack_name).template


def test_web_stack_rejects_every_region_except_eu_west_1() -> None:
    app = cdk.App()
    resources = cdk.Stack(app, "WrongRegionResources", env=ENV)
    cmk = kms.Key(resources, "Cmk")
    table = dynamodb.Table(
        resources,
        "RuntimeState",
        partition_key=dynamodb.Attribute(
            name="userId", type=dynamodb.AttributeType.STRING
        ),
    )
    bucket = s3.Bucket(resources, "UserFiles")

    with pytest.raises(ValueError, match="eu-west-1"):
        WebStack(
            app,
            "WrongRegionWeb",
            cmk_arn=cmk.key_arn,
            runtime_state_table=table,
            capability_state_table=table,
            user_files_bucket=bucket,
            runtime_arn=RUNTIME_ARN,
            runtime_iam_arn=RUNTIME_IAM_ARN,
            runtime_endpoint_name=RELEASE_ENDPOINT,
            trusted_code_asset_root="lambda",
            web_asset_root="tests/fixtures/web-dist",
            env=cdk.Environment(account="123456789012", region="us-east-1"),
        )


def test_web_export_reads_capability_installations_without_write_authority() -> None:
    template = _synth_web_template()
    web_function = next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName")
        == "personal-operator-web-api"
    )
    environment = web_function["Properties"]["Environment"]["Variables"]
    assert environment["CAPABILITY_STATE_TABLE_NAME"]

    capability_statements = [
        statement
        for statement in _web_statements(template)
        if "CapabilityState" in repr(statement.get("Resource"))
    ]
    assert len(capability_statements) == 1
    assert capability_statements[0]["Action"] == "dynamodb:GetItem"


def test_static_site_is_private_encrypted_and_cloudfront_only() -> None:
    template = _synth_web_template()
    assets = _logical_resource(template, "WebAssetsBucket")
    distribution = _resources(template, "AWS::CloudFront::Distribution")[0]
    config = distribution["Properties"]["DistributionConfig"]

    assert assets["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][
        0
    ]["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"
    assert assets["Properties"]["PublicAccessBlockConfiguration"] == {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    assert "WebsiteConfiguration" not in assets["Properties"]
    assert assets["Properties"]["VersioningConfiguration"] == {"Status": "Enabled"}
    assert assets["DeletionPolicy"] == "Retain"
    assert len(_resources(template, "AWS::CloudFront::OriginAccessControl")) == 1
    assert config["DefaultRootObject"] == "index.html"
    assert config["DefaultCacheBehavior"]["ViewerProtocolPolicy"] == "redirect-to-https"
    assert config["HttpVersion"] == "http2and3"
    # Standard CloudFront logs would persist the bearer approval token from
    # /approve/{token}; safe route-template API logs are asserted below.
    assert "Logging" not in config
    # The default CloudFront certificate has no configurable minimum version;
    # every behavior still redirects plaintext viewers to HTTPS.
    assert "ViewerCertificate" not in config


def test_cloudfront_applies_security_headers_and_never_caches_api_calls() -> None:
    template = _synth_web_template()
    distribution = _resources(template, "AWS::CloudFront::Distribution")[0]
    config = distribution["Properties"]["DistributionConfig"]
    policies = _resources(template, "AWS::CloudFront::ResponseHeadersPolicy")
    assert len(policies) == 1
    headers = policies[0]["Properties"]["ResponseHeadersPolicyConfig"][
        "SecurityHeadersConfig"
    ]

    assert headers["ContentSecurityPolicy"]["Override"] is True
    assert "default-src 'self'" in headers["ContentSecurityPolicy"][
        "ContentSecurityPolicy"
    ]
    assert "frame-ancestors 'none'" in headers["ContentSecurityPolicy"][
        "ContentSecurityPolicy"
    ]
    assert headers["StrictTransportSecurity"]["AccessControlMaxAgeSec"] >= 31_536_000
    assert headers["StrictTransportSecurity"]["Preload"] is True
    assert headers["FrameOptions"]["FrameOption"] == "DENY"
    assert headers["ContentTypeOptions"]["Override"] is True

    behaviors = {item["PathPattern"]: item for item in config["CacheBehaviors"]}
    assert set(behaviors) == {"api/*", "oauth/*", "approve/*"}
    for behavior in behaviors.values():
        assert behavior["ViewerProtocolPolicy"] == "redirect-to-https"
        assert set(behavior["AllowedMethods"]) == {
            "DELETE",
            "GET",
            "HEAD",
            "OPTIONS",
            "PATCH",
            "POST",
            "PUT",
        }
        assert behavior["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


def test_cloudfront_is_the_only_accepted_http_api_origin() -> None:
    template = _synth_web_template()
    distribution = _resources(template, "AWS::CloudFront::Distribution")[0]
    origins = distribution["Properties"]["DistributionConfig"]["Origins"]
    api_origin = next(origin for origin in origins if "CustomOriginConfig" in origin)
    custom_headers = api_origin["OriginCustomHeaders"]

    assert len(custom_headers) == 1
    assert custom_headers[0]["HeaderName"] == (
        "X-Personal-Operator-Origin-Verify"
    )
    # CloudFormation resolves this at deployment. The synthesized template
    # carries only a Secrets Manager reference, never the generated value.
    rendered = str(custom_headers[0]["HeaderValue"])
    assert "{{resolve:secretsmanager:" in rendered
    assert "OriginVerificationSecret" in rendered

    web = next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName")
        == "personal-operator-web-api"
    )
    assert "ORIGIN_VERIFICATION_SECRET_ID" in web["Properties"]["Environment"][
        "Variables"
    ]


def test_origin_gate_rejects_direct_api_calls_before_application_construction(
    monkeypatch,
) -> None:
    import sys

    sys.path.insert(0, str(ROOT / "lambda"))
    from web import index as web_index

    expected = "o" * 64
    monkeypatch.setattr(web_index, "_origin_verification_secret", lambda: expected)
    handled = []

    class Application:
        def handle(self, event):
            handled.append(event)
            return {"status": "ok"}

    monkeypatch.setattr(web_index, "_application", lambda: Application())
    event = {
        "requestContext": {"http": {"method": "GET", "path": "/api/workspace"}},
        "headers": {},
    }
    assert web_index.lambda_handler(event, None)["statusCode"] == 403
    assert handled == []

    event["headers"] = {"X-Personal-Operator-Origin-Verify": "wrong"}
    assert web_index.lambda_handler(event, None)["statusCode"] == 403
    assert handled == []

    event["headers"] = {"X-Personal-Operator-Origin-Verify": expected}
    assert web_index.lambda_handler(event, None) == {"status": "ok"}
    assert handled == [event]

    scheduled = {
        "detail-type": "ScheduledRetentionSweep",
        "source": "personal-operator.retention",
        "version": 1,
    }
    assert web_index.lambda_handler(scheduled, None) == {"status": "ok"}
    assert handled[-1] == scheduled


def test_edge_router_serves_spa_routes_and_splits_approval_html_from_json() -> None:
    template = _synth_web_template()
    functions = _resources(template, "AWS::CloudFront::Function")
    distribution = _resources(template, "AWS::CloudFront::Distribution")[0]
    config = distribution["Properties"]["DistributionConfig"]

    assert len(functions) == 1
    code = functions[0]["Properties"]["FunctionCode"]
    assert "text/html" in code
    assert "application/json" in code
    assert "/index.html" in code
    assert "statusCode: 200" in code
    assert "content-security-policy" in code
    assert "strict-transport-security" in code
    assert "x-content-type-options" in code
    assert "referrer-policy" in code
    assert config["DefaultCacheBehavior"]["FunctionAssociations"][0][
        "EventType"
    ] == "viewer-request"
    approval = next(
        behavior
        for behavior in config["CacheBehaviors"]
        if behavior["PathPattern"] == "approve/*"
    )
    assert approval["FunctionAssociations"][0]["EventType"] == "viewer-request"
    assert approval["CachePolicyId"] == "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"


def test_synthesis_reads_the_explicit_web_asset_root_not_an_untracked_build() -> None:
    source = (ROOT / "stacks" / "web_stack.py").read_text(encoding="utf-8")
    assert 'Path(web_asset_root) / "index.html"' in source
    assert 'Path("web/dist/index.html")' not in source


def test_http_api_exposes_only_the_seventeen_trusted_control_routes() -> None:
    template = _synth_web_template()
    routes = {
        route["Properties"]["RouteKey"]
        for route in _resources(template, "AWS::ApiGatewayV2::Route")
    }

    assert routes == {
        "POST /api/session/connect",
        "POST /api/session/logout",
        "GET /oauth/google/start",
        "GET /oauth/google/callback",
        "GET /approve/{token}",
        "POST /api/actions/{id}/approve",
        "POST /api/actions/{id}/reject",
        "GET /api/gmail",
        "POST /api/gmail/drafts/{action}",
        "GET /api/workspace",
        "GET /api/overview",
        "GET /api/export",
        "POST /api/import/plan",
        "POST /api/import/activate",
        "POST /api/delete",
        "POST /api/connections/google-gmail-readonly/disconnect",
        "POST /api/scans/{scan}/feedback",
    }
    assert not any("openclaw" in route.casefold() for route in routes)
    assert not any("agentcore" in route.casefold() for route in routes)
    assert _resources(template, "AWS::Lambda::Url") == []


def test_web_and_control_failures_have_explicit_alarms() -> None:
    template = _synth_web_template()
    names = {
        alarm["Properties"].get("AlarmName")
        for alarm in _resources(template, "AWS::CloudWatch::Alarm")
    }
    assert {
        "personal-operator-web-errors",
        "personal-operator-web-throttles",
        "personal-operator-control-errors",
        "personal-operator-control-throttles",
    }.issubset(names)


def test_web_lambda_packages_the_lambda_root_and_has_exact_resource_bindings() -> None:
    template = _synth_web_template()
    function = next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName")
        == "personal-operator-web-api"
    )
    variables = function["Properties"]["Environment"]["Variables"]

    assert function["Properties"]["Handler"] == "web.index.lambda_handler"
    assert function["Properties"]["Runtime"] == "python3.13"
    assert function["Properties"]["Timeout"] <= 30
    assert function["Properties"]["TracingConfig"] == {"Mode": "Active"}
    assert variables["AWS_REGION_LOCK"] == "eu-west-1"
    assert variables["AGENTCORE_QUALIFIER"] == RELEASE_ENDPOINT
    assert variables["CONTROL_TABLE_NAME"]["Ref"].startswith("WebControlTable")
    assert "RUNTIME_STATE_TABLE_NAME" in variables
    assert "IDENTITY_TABLE_NAME" in variables
    assert "MESSAGE_LEDGER_TABLE_NAME" in variables
    assert "USER_FILES_BUCKET_NAME" in variables
    assert "APPROVAL_SIGNING_SECRET_ID" in variables
    assert variables["OAUTH_KMS_KEY_ID"]
    assert variables["WEB_ORIGIN"]["Fn::Join"][1][0] == "https://"
    assert variables["GOOGLE_REDIRECT_URI"]["Fn::Join"][1][-1] == (
        "/oauth/google/callback"
    )

    stack_source = (ROOT / "stacks/web_stack.py").read_text(encoding="utf-8")
    assert "_lambda.Code.from_asset(trusted_code_asset_root)" in stack_source


def test_scheduled_maintenance_has_a_dedicated_long_running_singleton() -> None:
    template = _synth_web_template()
    functions = {
        resource["Properties"].get("FunctionName"): resource["Properties"]
        for resource in _resources(template, "AWS::Lambda::Function")
    }
    web = functions["personal-operator-web-api"]
    maintenance = functions["personal-operator-maintenance"]

    assert web["Timeout"] == 30
    assert maintenance["Timeout"] == 900
    assert maintenance["ReservedConcurrentExecutions"] == 1
    assert maintenance["Handler"] == "web.index.lambda_handler"
    assert maintenance["Code"] == web["Code"]
    assert maintenance["Role"] == web["Role"]
    assert maintenance["Environment"]["Variables"] == web["Environment"]["Variables"]


def test_versioned_control_lambda_is_the_only_telegram_command_target() -> None:
    template = _synth_web_template()
    functions = {
        resource["Properties"].get("FunctionName"): resource
        for resource in _resources(template, "AWS::Lambda::Function")
    }
    control = functions["personal-operator-control-command"]
    variables = control["Properties"]["Environment"]["Variables"]

    assert control["Properties"]["Handler"] == "control.index.lambda_handler"
    assert variables["CONTROL_TABLE_NAME"]["Ref"].startswith("WebControlTable")
    assert variables["WEB_AUTH_SECRET_ID"]
    assert variables["GOOGLE_READONLY_OAUTH_SECRET_ID"]
    assert variables["OPENAI_API_KEY_SECRET_ID"]
    assert variables["OAUTH_KMS_KEY_ID"]
    assert variables["WEB_ORIGIN"]["Fn::Join"][1][0] == "https://"
    aliases = _resources(template, "AWS::Lambda::Alias")
    assert any(
        alias["Properties"].get("Name") == "live"
        and alias["Properties"]["FunctionName"] == {"Ref": next(
            logical_id
            for logical_id, resource in template["Resources"].items()
            if resource is control
        )}
        for alias in aliases
    )
    actions = _flatten_actions(
        _statements_for_function(template, "personal-operator-control-command")
    )
    assert {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query",
        "secretsmanager:GetSecretValue",
        "kms:Decrypt",
        "kms:GenerateDataKey",
    }.issubset(actions)
    assert {
        "bedrock-agentcore:InvokeAgentRuntime",
        "bedrock-agentcore:StopRuntimeSession",
        "lambda:InvokeFunction",
        "ses:SendEmail",
    }.isdisjoint(actions)


def test_control_founder_approval_binding_is_exact_and_has_no_send_credential() -> None:
    template = _synth_web_template(
        founder_user_ids="founder-1",
        gmail_send_connection_id="google_conn_1234",
        gmail_send_account_email="founder@example.com",
    )
    control = next(
        resource
        for resource in _resources(template, "AWS::Lambda::Function")
        if resource["Properties"].get("FunctionName")
        == "personal-operator-control-command"
    )
    variables = control["Properties"]["Environment"]["Variables"]
    assert variables["FOUNDER_USER_IDS"] == "founder-1"
    assert variables["GMAIL_SEND_CONNECTION_ID"] == "google_conn_1234"
    assert variables["GMAIL_SEND_ACCOUNT_EMAIL"] == "founder@example.com"
    assert "APPROVAL_SIGNING_SECRET_ID" in variables
    assert "GOOGLE_SEND_OAUTH_SECRET_ID" not in variables

    approval_secret_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::SecretsManager::Secret"
        and resource["Properties"].get("Name")
        == "personal-operator/approval-signing"
    )
    matching = [
        statement
        for statement in _statements_for_function(
            template, "personal-operator-control-command"
        )
        if statement.get("Resource") == {"Ref": approval_secret_id}
    ]
    assert matching == [
        {
            "Action": "secretsmanager:GetSecretValue",
            "Effect": "Allow",
            "Resource": {"Ref": approval_secret_id},
        }
    ]


def test_shared_web_role_can_only_describe_and_schedule_founder_send_secret_deletion() -> None:
    template = _synth_web_template()
    send_secret_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::SecretsManager::Secret"
        and resource["Properties"].get("Name")
        == "personal-operator/google-send-oauth"
    )
    resource = {"Ref": send_secret_id}
    matching = [
        statement
        for statement in _web_statements(template)
        if statement.get("Resource") == resource
    ]

    assert {
        frozenset(
            action if isinstance(action, list) else [action]
        )
        for action in (statement["Action"] for statement in matching)
    } == {
        frozenset(
            {"secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"}
        ),
        frozenset(
            {"secretsmanager:DescribeSecret", "secretsmanager:DeleteSecret"}
        ),
    }
    control_matches = [
        statement
        for statement in _statements_for_function(
            template, "personal-operator-control-command"
        )
        if statement.get("Resource") == resource
    ]
    assert control_matches == []


def test_founder_effect_context_fails_closed_without_an_exact_account_binding() -> None:
    invalid = (
        {"founder_user_ids": "founder-1"},
        {
            "founder_user_ids": "founder-1,founder-2",
            "gmail_send_connection_id": "google_conn_1234",
            "gmail_send_account_email": "founder@example.com",
        },
        {
            "founder_user_ids": "../founder",
            "gmail_send_connection_id": "google_conn_1234",
            "gmail_send_account_email": "founder@example.com",
        },
        {
            "founder_user_ids": "founder-1",
            "gmail_send_connection_id": "short",
            "gmail_send_account_email": "founder@example.com",
        },
        {
            "founder_user_ids": "founder-1",
            "gmail_send_connection_id": "google_conn_1234",
            "gmail_send_account_email": "not-an-email",
        },
    )
    for values in invalid:
        with pytest.raises(ValueError):
            _synth_web_template(**values)


def test_only_web_lambda_can_read_the_cloudfront_origin_secret() -> None:
    template = _synth_web_template()
    origin_secret_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::SecretsManager::Secret"
        and resource["Properties"].get("Name")
        == "personal-operator/cloudfront-origin-verification"
    )
    resource = {"Ref": origin_secret_id}
    web_matches = [
        statement
        for statement in _web_statements(template)
        if statement.get("Resource") == resource
    ]
    control_matches = [
        statement
        for statement in _statements_for_function(
            template, "personal-operator-control-command"
        )
        if statement.get("Resource") == resource
    ]

    assert len(web_matches) == 1
    assert set(web_matches[0]["Action"]) == {
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
    }
    assert control_matches == []


def test_session_approval_and_google_credentials_have_separate_cmk_secrets() -> None:
    template = _synth_web_template()
    secrets = _resources(template, "AWS::SecretsManager::Secret")
    by_name = {secret["Properties"]["Name"]: secret for secret in secrets}

    assert set(by_name) == {
        "personal-operator/web-auth",
        "personal-operator/approval-signing",
        "personal-operator/cloudfront-origin-verification",
        "personal-operator/google-readonly-oauth",
        "personal-operator/google-send-oauth",
        "personal-operator/openai-api-key",
    }
    assert "GenerateSecretString" in by_name["personal-operator/web-auth"][
        "Properties"
    ]
    assert "GenerateSecretString" in by_name[
        "personal-operator/approval-signing"
    ]["Properties"]
    assert by_name["personal-operator/cloudfront-origin-verification"][
        "Properties"
    ]["GenerateSecretString"]["PasswordLength"] == 64
    # Google credentials are externally issued. CDK initializes the container
    # with an unstructured random placeholder, not a clientId/clientSecret JSON
    # object, so production composition must fail closed until it is replaced.
    google_generation = by_name["personal-operator/google-readonly-oauth"]["Properties"][
        "GenerateSecretString"
    ]
    assert "REPLACE_ME" in google_generation["SecretStringTemplate"]
    assert google_generation["GenerateStringKey"] == "bootstrap_nonce"
    send_template = by_name["personal-operator/google-send-oauth"]["Properties"][
        "GenerateSecretString"
    ]["SecretStringTemplate"]
    assert '"user_id": "REPLACE_ME"' in send_template
    assert all(secret["Properties"].get("KmsKeyId") for secret in secrets)


def test_control_records_are_ttl_scoped_encrypted_and_recoverable() -> None:
    template = _synth_web_template()
    table = _logical_resource(template, "WebControlTable")
    properties = table["Properties"]

    assert properties["KeySchema"] == [
        {"AttributeName": "PK", "KeyType": "HASH"},
        {"AttributeName": "SK", "KeyType": "RANGE"},
    ]
    assert properties["TimeToLiveSpecification"] == {
        "AttributeName": "ttl",
        "Enabled": True,
    }
    assert properties["PointInTimeRecoverySpecification"] == {
        "PointInTimeRecoveryEnabled": True
    }
    assert properties["SSESpecification"]["SSEEnabled"] is True
    assert table["DeletionPolicy"] == "Retain"
    assert table["UpdateReplacePolicy"] == "Retain"
    assert properties["GlobalSecondaryIndexes"] == [
        {
            "IndexName": "userId-index",
            "KeySchema": [{"AttributeName": "userId", "KeyType": "HASH"}],
            "Projection": {"ProjectionType": "ALL"},
        }
    ]


def test_web_lambda_has_only_control_plane_authority() -> None:
    template = _synth_web_template()
    statements = _web_statements(template)
    actions = _flatten_actions(statements)

    assert {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:TransactWriteItems",
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:PutObject",
        "secretsmanager:GetSecretValue",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "bedrock-agentcore:StopRuntimeSession",
    }.issubset(actions)
    assert {
        "bedrock-agentcore:InvokeAgentRuntime",
        "lambda:InvokeFunction",
        "sqs:SendMessage",
        "ses:SendEmail",
        "ses:SendRawEmail",
    }.isdisjoint(actions)
    footprint = [
        statement
        for statement in statements
        if set(
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
        == {"dynamodb:Query", "dynamodb:DeleteItem"}
        and len(statement.get("Resource", [])) == 4
    ]
    assert len(footprint) == 1
    assert sum(
        "userId-index" in str(resource)
        for resource in footprint[0]["Resource"]
    ) == 2
    identity_fence = [
        statement
        for statement in statements
        if set(
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
        == {
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:TransactWriteItems",
        }
    ]
    assert len(identity_fence) == 1
    assert not isinstance(identity_fence[0]["Resource"], list)
    assert "index" not in str(identity_fence[0]["Resource"])
    workspace_bucket_statement = next(
        statement
        for statement in statements
        if "s3:ListBucketVersions" in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    )
    assert set(workspace_bucket_statement["Action"]) == {
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:ListBucketMultipartUploads",
    }
    assert workspace_bucket_statement["Resource"] != "*"
    workspace_object_statement = next(
        statement
        for statement in statements
        if "s3:DeleteObjectVersion" in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    )
    assert set(workspace_object_statement["Action"]) == {
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:AbortMultipartUpload",
    }
    assert workspace_object_statement["Resource"] != "*"
    portable_write = [
        statement
        for statement in statements
        if "s3:PutObject" in (
            statement["Action"]
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    ]
    assert len(portable_write) == 1
    assert portable_write[0]["Action"] == "s3:PutObject"
    assert "/.system/portable/v2/" in str(portable_write[0]["Resource"])

    portable_blob_kms = [
        statement
        for statement in statements
        if statement.get("Action") == "kms:GenerateDataKey"
        and statement.get("Condition")
        == {
            "StringEquals": {
                "kms:CallerAccount": "123456789012",
                "kms:ViaService": "s3.eu-west-1.amazonaws.com",
            }
        }
    ]
    assert len(portable_blob_kms) == 1
    assert portable_blob_kms[0]["Resource"] != "*"
    for statement in statements:
        if set(
            [statement["Action"]]
            if isinstance(statement.get("Action"), str)
            else statement.get("Action", [])
        ) & {
            "bedrock-agentcore:StopRuntimeSession",
            "secretsmanager:GetSecretValue",
            "s3:GetObject",
            "s3:DeleteObject",
            "s3:AbortMultipartUpload",
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
        }:
            assert statement.get("Resource") != "*"


def test_web_and_control_have_exact_separate_dynamodb_cmk_authority() -> None:
    template = _synth_web_template()
    expected_actions = {
        "kms:Decrypt",
        "kms:DescribeKey",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
    }
    expected_condition = {
        "StringEquals": {
            "kms:CallerAccount": "123456789012",
            "kms:ViaService": "dynamodb.eu-west-1.amazonaws.com",
        }
    }
    for function_name in (
        "personal-operator-web-api",
        "personal-operator-control-command",
    ):
        matches = [
            statement
            for statement in _statements_for_function(template, function_name)
            if set(
                statement["Action"]
                if isinstance(statement.get("Action"), list)
                else [statement.get("Action")]
            )
            == expected_actions
        ]
        assert len(matches) == 1
        assert matches[0]["Condition"] == expected_condition
        assert matches[0]["Resource"] != "*"


def test_api_has_access_logs_metrics_and_account_level_throttling() -> None:
    template = _synth_web_template()
    stage = _resources(template, "AWS::ApiGatewayV2::Stage")[0]["Properties"]
    assert stage["AccessLogSettings"]["DestinationArn"]
    assert "requestId" in stage["AccessLogSettings"]["Format"]
    assert "routeKey" in stage["AccessLogSettings"]["Format"]
    assert "$context.path" not in stage["AccessLogSettings"]["Format"]
    assert stage["DefaultRouteSettings"]["DetailedMetricsEnabled"] is True
    assert 1 <= stage["DefaultRouteSettings"]["ThrottlingRateLimit"] <= 20
    assert 1 <= stage["DefaultRouteSettings"]["ThrottlingBurstLimit"] <= 40

    log_groups = _resources(template, "AWS::Logs::LogGroup")
    assert any(
        group["Properties"].get("LogGroupName")
        == "/personal-operator/api/web-access"
        for group in log_groups
    )
    assert any(
        group["Properties"].get("LogGroupName")
        == "/personal-operator/lambda/web"
        for group in log_groups
    )


def test_hourly_maintenance_sweep_invokes_only_the_trusted_web_handler() -> None:
    template = _synth_web_template()
    rules = _resources(template, "AWS::Events::Rule")

    assert len(rules) == 1
    properties = rules[0]["Properties"]
    assert properties["ScheduleExpression"] == "rate(1 hour)"
    assert properties["State"] == "ENABLED"
    assert len(properties["Targets"]) == 1
    assert properties["Targets"][0]["Input"] == (
        '{"detail-type":"ScheduledRetentionSweep",'
        '"source":"personal-operator.retention","version":1}'
    )
    target_arn = properties["Targets"][0]["Arn"]
    maintenance_logical_id = next(
        logical_id
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName")
        == "personal-operator-maintenance"
    )
    assert target_arn == {"Fn::GetAtt": [maintenance_logical_id, "Arn"]}
    permissions = _resources(template, "AWS::Lambda::Permission")
    scheduled = [
        permission
        for permission in permissions
        if permission["Properties"].get("Principal") == "events.amazonaws.com"
    ]
    assert len(scheduled) == 1


def test_optional_global_web_acl_is_attached_without_regional_waf_resources() -> None:
    web_acl = (
        "arn:aws:wafv2:us-east-1:123456789012:global/webacl/"
        "personal-operator/12345678-1234-1234-1234-123456789abc"
    )
    template = _synth_web_template(web_acl_id=web_acl)
    distribution = _resources(template, "AWS::CloudFront::Distribution")[0]

    assert distribution["Properties"]["DistributionConfig"]["WebACLId"] == web_acl
    assert _resources(template, "AWS::WAFv2::WebACL") == []


def test_app_wires_existing_runtime_state_bucket_and_cmk_into_web_stack() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert "from stacks.web_stack import WebStack" in source
    assert "WebStack(" in source
    assert '"PersonalOperatorWeb"' in source
    assert "cmk_arn=security_stack.cmk.key_arn" in source
    assert "runtime_state_table=router_stack.runtime_state_table" in source
    assert "capability_state_table=capability_stack.state_table" in source
    assert "user_files_bucket=agentcore_stack.user_files_bucket" in source
    assert "runtime_arn=agentcore_stack.runtime_arn" in source
    assert "runtime_iam_arn=agentcore_stack.runtime_iam_arn" in source
    assert "runtime_endpoint_name=agentcore_stack.runtime_endpoint_name" in source
    assert "auth_secret=security_stack.web_auth_secret" in source
    assert "approval_secret=security_stack.approval_signing_secret" in source
    assert (
        "origin_verification_secret=security_stack.origin_verification_secret"
        in source
    )
    assert "google_readonly_oauth_secret=security_stack.google_readonly_oauth_secret" in source
    assert "google_send_oauth_secret=security_stack.google_send_oauth_secret" in source
    assert "openai_api_key_secret=security_stack.openai_api_key_secret" in source
    assert 'try_get_context("gmail_send_connection_id")' in source
    assert 'try_get_context("gmail_send_account_email")' in source
