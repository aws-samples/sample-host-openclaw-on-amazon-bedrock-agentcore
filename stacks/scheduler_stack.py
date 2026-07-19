"""Trusted read-only scheduler: control table, ingress Lambda, and split IAM.

The EventBridge Scheduler role may hold ONLY ``lambda:InvokeFunction`` on the
ingress function. The ingress role holds ONLY strong-read on the control table,
``sqs:SendMessage`` on the update FIFO, and scoped KMS. Neither role holds any
AgentCore, Secrets Manager, connector, browser, or gateway-invoke authority, so
a fired schedule can only enqueue a read-only occurrence. This stack supersedes
the CronStack tombstone.
"""

from __future__ import annotations

import re

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Token,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as _lambda,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct


REQUIRED_REGION = "eu-west-1"
INGRESS_FUNCTION_NAME = "personal-operator-scheduler-ingress"
CONTROL_TABLE_NAME = "personal-operator-scheduler-control"
_QUEUE_ARN = re.compile(r"arn:aws:sqs:eu-west-1:[0-9]{12}:[A-Za-z0-9_-]+\.fifo")


class SchedulerStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        trusted_code_asset_root: str,
        cmk_arn: str,
        update_queue_arn: str,
        update_queue_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        account = Stack.of(self).account
        if region != REQUIRED_REGION:
            raise ValueError(
                f"SchedulerStack must be deployed in {REQUIRED_REGION}; got {region}"
            )
        if not isinstance(trusted_code_asset_root, str) or not trusted_code_asset_root:
            raise ValueError("SchedulerStack requires the trusted Lambda asset")
        if not isinstance(cmk_arn, str) or not cmk_arn:
            raise ValueError("SchedulerStack requires one exact CMK ARN")
        # The FIFO ARN/URL arrive as CDK cross-stack tokens at synth time; only
        # concrete values are pattern-checked.
        if not isinstance(update_queue_arn, str) or (
            not Token.is_unresolved(update_queue_arn)
            and _QUEUE_ARN.fullmatch(update_queue_arn) is None
        ):
            raise ValueError("SchedulerStack requires the exact update FIFO ARN")
        if not isinstance(update_queue_url, str) or (
            not Token.is_unresolved(update_queue_url)
            and (
                not update_queue_url.startswith("https://")
                or not update_queue_url.endswith(".fifo")
            )
        ):
            raise ValueError("SchedulerStack requires the exact update FIFO URL")

        encryption_key = kms.Key.from_key_arn(
            self, "SchedulerControlEncryptionKey", cmk_arn
        )

        # --- Control table (CMK, PITR, RETAIN) ---
        self.control_table = dynamodb.Table(
            self,
            "SchedulerControlTable",
            table_name=CONTROL_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
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
        self.control_table.add_global_secondary_index(
            index_name="userId-index",
            partition_key=dynamodb.Attribute(
                name="userId", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        log_group = logs.LogGroup(
            self,
            "SchedulerIngressLogGroup",
            log_group_name="/personal-operator/lambda/scheduler-ingress",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Ingress execution role: strong-read + FIFO send + scoped KMS ---
        ingress_role = iam.Role(
            self,
            "SchedulerIngressRole",
            role_name=f"personal-operator-scheduler-ingress-{region}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Trusted scheduler ingress with strong-read control state and "
                "read-only occurrence enqueue authority only"
            ),
        )
        ingress_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    "/personal-operator/lambda/scheduler-ingress:*"
                ],
            )
        )
        ingress_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                ],
                resources=[
                    self.control_table.table_arn,
                    f"{self.control_table.table_arn}/index/*",
                ],
            )
        )
        ingress_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sqs:SendMessage"],
                resources=[update_queue_arn],
            )
        )
        ingress_role.add_to_policy(
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
                    },
                    "ForAnyValue:StringEquals": {
                        "kms:ViaService": [
                            f"dynamodb.{region}.amazonaws.com",
                            f"sqs.{region}.amazonaws.com",
                        ]
                    },
                },
            )
        )

        self.ingress_function = _lambda.Function(
            self,
            "SchedulerIngressFunction",
            function_name=INGRESS_FUNCTION_NAME,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="scheduler.ingress.lambda_handler",
            code=_lambda.Code.from_asset(trusted_code_asset_root),
            role=ingress_role,
            timeout=Duration.seconds(15),
            memory_size=256,
            log_group=log_group,
            environment={
                "SCHEDULER_CONTROL_TABLE_NAME": CONTROL_TABLE_NAME,
                "SCHEDULER_UPDATE_QUEUE_URL": update_queue_url,
            },
        )
        self.ingress_function_arn = (
            f"arn:aws:lambda:{region}:{account}:function:{INGRESS_FUNCTION_NAME}"
        )

        # --- EventBridge Scheduler role: ONLY invoke the ingress function ---
        self.scheduler_role = iam.Role(
            self,
            "SchedulerInvokeRole",
            role_name=f"personal-operator-scheduler-invoke-{region}",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
            description=(
                "EventBridge Scheduler target role limited to invoking the "
                "trusted ingress function"
            ),
        )
        self.scheduler_role.add_to_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    self.ingress_function_arn,
                    f"{self.ingress_function_arn}:*",
                ],
            )
        )

        CfnOutput(
            self,
            "SchedulerIngressFunctionArn",
            value=self.ingress_function_arn,
        )
        CfnOutput(
            self,
            "SchedulerInvokeRoleArn",
            value=self.scheduler_role.role_arn,
        )
        CfnOutput(
            self,
            "SchedulerControlTableName",
            value=self.control_table.table_name,
        )

        # --- cdk-nag suppressions (mirroring router_stack style) ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.ingress_function,
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
            ingress_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The only wildcards select streams beneath the one exact "
                        "precreated scheduler-ingress log group and the control "
                        "table's own KEYS_ONLY index; both are resource bound."
                    ),
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "DynamoDB and SQS CMK data access require both ReEncrypt "
                        "directions and both GenerateDataKey variants; these "
                        "actions remain restricted to the exact key, account, and "
                        "the DynamoDB/SQS via-service boundary."
                    ),
                    applies_to=[
                        "Action::kms:GenerateDataKey*",
                        "Action::kms:ReEncrypt*",
                    ],
                ),
            ],
            apply_to_children=True,
        )
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.scheduler_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "The scheduler role invokes only the exact ingress "
                        "function and its published versions/aliases; no other "
                        "resource is reachable."
                    ),
                    applies_to=[
                        f"Resource::{self.ingress_function_arn}:*",
                    ],
                ),
            ],
            apply_to_children=True,
        )


__all__ = [
    "CONTROL_TABLE_NAME",
    "INGRESS_FUNCTION_NAME",
    "SchedulerStack",
]
