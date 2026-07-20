"""Trusted Browser Gateway IAM boundary, disabled by default (Task 10).

This SEPARATE stack owns ALL browser IAM in its own role/policies. It is
disabled by default: no ``CfnBrowserCustom`` and no browser IAM authority are
synthesized unless an explicit release context flag (``enable_browser``) is set.
The browser role is NEVER the AgentCore runtime execution role and is never
passed into :class:`AgentCoreStack`, so a compromised runtime can hold no
browser authority.
"""

from __future__ import annotations

from aws_cdk import (
    CfnOutput,
    Stack,
    aws_iam as iam,
)
import cdk_nag
from constructs import Construct

REQUIRED_REGION = "eu-west-1"
BROWSER_ROLE_NAME = "personal-operator-trusted-browser-gateway-eu-west-1"


class BrowserStack(Stack):
    """Own the trusted browser authority separately from the runtime role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        region = Stack.of(self).region
        if region != REQUIRED_REGION:
            raise ValueError(
                f"BrowserStack must be deployed in {REQUIRED_REGION}; got {region}"
            )

        # Disabled by default. The external browser provider gate is OPEN, so no
        # browser resource or IAM authority is synthesized unless a release
        # explicitly opts in. Even when enabled, this role is deliberately NOT
        # the runtime execution role.
        enable_browser = str(
            self.node.try_get_context("enable_browser") or "false"
        ).casefold()
        self.browser_enabled = enable_browser != "false"

        # The role exists so the trusted-side gateway has a distinct identity,
        # but it carries NO browser IAM authority while the provider gate is
        # OPEN (disabled by default). This keeps all future browser IAM out of
        # the runtime execution role by construction.
        self.browser_role = iam.Role(
            self,
            "TrustedBrowserGatewayRole",
            role_name=BROWSER_ROLE_NAME,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=(
                "Trusted Browser Gateway identity, disabled by default; owns all "
                "browser IAM separately from the AgentCore runtime execution role"
            ),
        )

        CfnOutput(
            self,
            "TrustedBrowserGatewayRoleName",
            value=BROWSER_ROLE_NAME,
        )
        CfnOutput(
            self,
            "BrowserGatewayEnabled",
            value=str(self.browser_enabled).casefold(),
        )

        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.browser_role,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-IAM4",
                    reason=(
                        "The disabled-by-default browser role carries no attached "
                        "policies while the external browser provider gate is OPEN."
                    ),
                ),
            ],
            apply_to_children=True,
        )


__all__ = ["BrowserStack", "BROWSER_ROLE_NAME"]
