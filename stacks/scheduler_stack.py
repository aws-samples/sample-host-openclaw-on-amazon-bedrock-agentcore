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
    aws_scheduler as scheduler,
)
import cdk_nag
from constructs import Construct


REQUIRED_REGION = "eu-west-1"
INGRESS_FUNCTION_NAME = "personal-operator-scheduler-ingress"
CONTROL_FUNCTION_NAME = "personal-operator-scheduler-control"
CONTROL_TABLE_NAME = "personal-operator-scheduler-control"
SCHEDULE_GROUP_NAME = "personal-operator-v1"
_QUEUE_ARN = re.compile(r"arn:aws:sqs:eu-west-1:[0-9]{12}:[A-Za-z0-9_-]+\.fifo")
_CATALOG_DIGEST = re.compile(r"[0-9a-f]{64}")
_TABLE_NAME = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_TABLE_ARN = re.compile(
    r"arn:aws:dynamodb:eu-west-1:[0-9]{12}:table/[A-Za-z0-9_.-]{3,255}"
)


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
        catalog_digest: str,
        capability_state_table_name: str,
        capability_state_table_arn: str,
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
        if (
            not isinstance(catalog_digest, str)
            or _CATALOG_DIGEST.fullmatch(catalog_digest) is None
        ):
            raise ValueError("SchedulerStack requires the frozen catalog digest")
        if (
            not isinstance(capability_state_table_name, str)
            or (
                not Token.is_unresolved(capability_state_table_name)
                and _TABLE_NAME.fullmatch(capability_state_table_name) is None
            )
        ):
            raise ValueError("SchedulerStack requires the capability state table")
        if (
            not isinstance(capability_state_table_arn, str)
            or (
                not Token.is_unresolved(capability_state_table_arn)
                and _TABLE_ARN.fullmatch(capability_state_table_arn) is None
            )
        ):
            raise ValueError("SchedulerStack requires the capability state table ARN")
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

        self.schedule_group = scheduler.CfnScheduleGroup(
            self,
            "PersonalOperatorScheduleGroup",
            name=SCHEDULE_GROUP_NAME,
        )
        self.schedule_group.apply_removal_policy(RemovalPolicy.RETAIN)

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
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.control_table.add_global_secondary_index(
            index_name="schedule-user-index-v1",
            partition_key=dynamodb.Attribute(
                name="scheduleUserId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="scheduleSortKey", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )
        self.control_table.add_global_secondary_index(
            index_name="proposal-user-index-v1",
            partition_key=dynamodb.Attribute(
                name="proposalUserId", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="proposalSortKey", type=dynamodb.AttributeType.STRING
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
        control_log_group = logs.LogGroup(
            self,
            "SchedulerControlLogGroup",
            log_group_name="/personal-operator/lambda/scheduler-control",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
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
                    "dynamodb:TransactWriteItems",
                ],
                resources=[self.control_table.table_arn],
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

        # --- Approval/apply Lambda: exact local intent + exact Scheduler group ---
        control_role = iam.Role(
            self,
            "SchedulerControlRole",
            role_name=f"personal-operator-scheduler-control-{region}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Trusted exact one-time schedule approval boundary with no "
                "runtime, external integration, secret, or queue authority"
            ),
        )
        control_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:"
                    "/personal-operator/lambda/scheduler-control:*"
                ],
            )
        )
        control_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem"],
                resources=[capability_state_table_arn],
            )
        )
        control_role.add_to_policy(
            iam.PolicyStatement(
                actions=["dynamodb:TransactWriteItems"],
                resources=[
                    capability_state_table_arn,
                    self.control_table.table_arn,
                ],
            )
        )
        control_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:UpdateItem",
                    "dynamodb:Query",
                ],
                resources=[self.control_table.table_arn],
            )
        )
        schedule_group_arn = (
            f"arn:aws:scheduler:{region}:{account}:"
            f"schedule-group/{SCHEDULE_GROUP_NAME}"
        )
        schedule_arn = (
            f"arn:aws:scheduler:{region}:{account}:"
            f"schedule/{SCHEDULE_GROUP_NAME}/po-*"
        )
        control_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:DeleteSchedule",
                ],
                resources=[schedule_group_arn, schedule_arn],
            )
        )
        control_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[self.scheduler_role.role_arn],
                conditions={
                    "StringEquals": {
                        "iam:PassedToService": "scheduler.amazonaws.com"
                    }
                },
            )
        )
        control_role.add_to_policy(
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
                    "StringEquals": {"kms:CallerAccount": account},
                    "ForAnyValue:StringEquals": {
                        "kms:ViaService": [
                            f"dynamodb.{region}.amazonaws.com",
                            f"scheduler.{region}.amazonaws.com",
                        ]
                    },
                },
            )
        )

        self.control_function = _lambda.Function(
            self,
            "SchedulerControlFunction",
            function_name=CONTROL_FUNCTION_NAME,
            runtime=_lambda.Runtime.PYTHON_3_13,
            architecture=_lambda.Architecture.ARM_64,
            handler="scheduler.control.lambda_handler",
            code=_lambda.Code.from_asset(trusted_code_asset_root),
            role=control_role,
            timeout=Duration.seconds(30),
            memory_size=256,
            reserved_concurrent_executions=10,
            log_group=control_log_group,
            environment={
                "AWS_REGION_LOCK": REQUIRED_REGION,
                "CAPABILITY_CATALOG_DIGEST": catalog_digest,
                "CAPABILITY_STATE_TABLE_NAME": capability_state_table_name,
                "SCHEDULER_CONTROL_TABLE_NAME": CONTROL_TABLE_NAME,
                "SCHEDULER_GROUP_NAME": SCHEDULE_GROUP_NAME,
                "SCHEDULER_INGRESS_FUNCTION_ARN": self.ingress_function_arn,
                "SCHEDULER_INVOKE_ROLE_ARN": self.scheduler_role.role_arn,
            },
        )
        self.control_function.node.add_dependency(self.schedule_group)
        self.control_function_arn = (
            f"arn:aws:lambda:{region}:{account}:function:{CONTROL_FUNCTION_NAME}"
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
            "SchedulerControlFunctionArn",
            value=self.control_function_arn,
        )
        CfnOutput(
            self,
            "SchedulerControlTableName",
            value=self.control_table.table_name,
        )

        # --- cdk-nag suppressions (mirroring router_stack style) ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            [self.ingress_function, self.control_function],
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
                        "precreated scheduler-ingress log group; no other log "
                        "group or data resource is reachable."
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
            control_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "Wildcards are limited to streams beneath one exact log "
                        "group and opaque po-* schedules in one exact retained "
                        "Scheduler group."
                    ),
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM5",
                    reason=(
                        "DynamoDB and Scheduler CMK data access require both "
                        "ReEncrypt directions and GenerateDataKey variants; the "
                        "actions remain exact-key, exact-account, and via-service "
                        "restricted."
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
    "CONTROL_FUNCTION_NAME",
    "CONTROL_TABLE_NAME",
    "INGRESS_FUNCTION_NAME",
    "SCHEDULE_GROUP_NAME",
    "SchedulerStack",
]
