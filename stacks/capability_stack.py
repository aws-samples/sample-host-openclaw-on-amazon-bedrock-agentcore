"""Fail-closed capability gateway Lambda with no adapter authority."""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_logs as logs,
)
import cdk_nag
from constructs import Construct


REQUIRED_REGION = "eu-west-1"
GATEWAY_FUNCTION_NAME = "personal-operator-capability-gateway"


class CapabilityStack(Stack):
    """Package the typed gateway without enabling any production adapter."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        trusted_code_asset_root: str,
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
                "Fail-closed capability admission gateway with log-only base authority"
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
            ],
            apply_to_children=True,
        )


__all__ = ["CapabilityStack", "GATEWAY_FUNCTION_NAME"]
