"""Fail-closed capability gateway Lambda with no adapter authority."""

from __future__ import annotations

import re

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_dynamodb as dynamodb,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct

REQUIRED_REGION = "eu-west-1"
GATEWAY_FUNCTION_NAME = "personal-operator-capability-gateway"
STATE_TABLE_NAME = "personal-operator-capability-state"
SCHEDULER_CONTROL_TABLE_NAME = "personal-operator-scheduler-control"
PORTABLE_STATE_TABLE_NAME = "personal-operator-control"
_RELEASE_COMMIT = re.compile(r"[0-9a-f]{40}")
_CATALOG_DIGEST = re.compile(r"[0-9a-f]{64}")


class CapabilityStack(Stack):
    """Package the typed gateway without enabling any production adapter."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        trusted_code_asset_root: str,
        cmk_arn: str,
        release_commit: str,
        catalog_digest: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"CapabilityStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        if not isinstance(trusted_code_asset_root, str) or not trusted_code_asset_root:
            raise ValueError("CapabilityStack requires the trusted Lambda asset")
        if not isinstance(cmk_arn, str) or not cmk_arn:
            raise ValueError("CapabilityStack requires one exact CMK ARN")
        if (
            not isinstance(release_commit, str)
            or _RELEASE_COMMIT.fullmatch(release_commit) is None
        ):
            raise ValueError("CapabilityStack requires one exact release commit")
        if (
            not isinstance(catalog_digest, str)
            or _CATALOG_DIGEST.fullmatch(catalog_digest) is None
        ):
            raise ValueError("CapabilityStack requires one exact catalog digest")

        encryption_key = kms.Key.from_key_arn(
            self,
            "CapabilityStateEncryptionKey",
            cmk_arn,
        )
        self.state_table = dynamodb.Table(
            self,
            "CapabilityStateTable",
            table_name=STATE_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="PK",
                type=dynamodb.AttributeType.STRING,
            ),
            sort_key=dynamodb.Attribute(
                name="SK",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=encryption_key,
            point_in_time_recovery_specification=(
                dynamodb.PointInTimeRecoverySpecification(
                    point_in_time_recovery_enabled=True
                )
            ),
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        log_group = logs.LogGroup(
            self,
            "GatewayLogGroup",
            log_group_name="/personal-operator/lambda/capability-gateway",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        execution_role = iam.Role(
            self,
            "GatewayExecutionRole",
            role_name=f"personal-operator-capability-gateway-{region}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Fail-closed capability admission gateway with exact durable state authority"
            ),
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    "/personal-operator/lambda/capability-gateway:*"
                ],
            )
        )
        scheduler_table_arn = (
            f"arn:aws:dynamodb:{region}:{account}:table/"
            f"{SCHEDULER_CONTROL_TABLE_NAME}"
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:PutItem"],
                resources=[scheduler_table_arn],
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:Query"],
                resources=[
                    f"{scheduler_table_arn}/index/schedule-user-index-v1"
                ],
            )
        )
        portable_table_arn = (
            f"arn:aws:dynamodb:{region}:{account}:table/"
            f"{PORTABLE_STATE_TABLE_NAME}"
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[portable_table_arn],
                conditions={
                    "ForAllValues:StringEquals": {
                        "dynamodb:Attributes": [
                            "PK",
                            "SK",
                            "recordType",
                            "userId",
                            "generation",
                            "liveBundleHash",
                            "liveScheduleProjectionJson",
                        ]
                    },
                    "ForAllValues:StringLike": {
                        "dynamodb:LeadingKeys": ["USER#*"],
                    },
                },
            )
        )
        execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:TransactWriteItems",
                ],
                resources=[self.state_table.table_arn],
            )
        )
        execution_role.add_to_policy(
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
                        "kms:ViaService": f"dynamodb.{region}.amazonaws.com",
                    }
                },
            )
        )

        allowed_caller_arn = (
            f"arn:aws:iam::{account}:role/"
            f"openclaw-agentcore-execution-role-{region}"
        )

        self.gateway_function = _lambda.Function(
            self,
            "GatewayFunction",
            function_name=GATEWAY_FUNCTION_NAME,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="capabilities.gateway.lambda_handler",
            code=_lambda.Code.from_asset(trusted_code_asset_root),
            role=execution_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            log_group=log_group,
            environment={
                "CAPABILITY_ALLOWED_CALLER_ARN": allowed_caller_arn,
                "CAPABILITY_CATALOG_DIGEST": catalog_digest,
                "CAPABILITY_RELEASE_COMMIT": release_commit,
                "CAPABILITY_STATE_TABLE_NAME": STATE_TABLE_NAME,
                "PORTABLE_STATE_TABLE_NAME": PORTABLE_STATE_TABLE_NAME,
                "SCHEDULER_CONTROL_TABLE_NAME": SCHEDULER_CONTROL_TABLE_NAME,
            },
        )
        self.gateway_function_arn = (
            f"arn:aws:lambda:{region}:{account}:function:{GATEWAY_FUNCTION_NAME}"
        )

        CfnOutput(
            self,
            "CapabilityGatewayFunctionArn",
            value=self.gateway_function_arn,
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.gateway_function,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-L1",
                    reason=(
                        "Python 3.13 is the latest stable Lambda runtime in the "
                        "required region."
                    ),
                ),
            ],
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            execution_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The only wildcard selects streams beneath the one exact "
                        "precreated capability-gateway log group."
                    ),
                    applies_to=[
                        f"Resource::arn:aws:logs:{region}:{account}:log-group:"
                        "/personal-operator/lambda/capability-gateway:*"
                    ],
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "DynamoDB CMK data access requires both ReEncrypt directions "
                        "and both GenerateDataKey variants; these actions remain "
                        "restricted to the exact key, account, region, and "
                        "DynamoDB via-service boundary."
                    ),
                    applies_to=[
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                    ],
                ),
            ],
            apply_to_children=True,
        )


__all__ = ["CapabilityStack", "GATEWAY_FUNCTION_NAME", "STATE_TABLE_NAME"]
