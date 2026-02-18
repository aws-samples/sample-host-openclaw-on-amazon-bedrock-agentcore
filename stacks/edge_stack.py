"""Edge Stack — CloudFront distribution with VPC origin, token auth + WAF."""

from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_elasticloadbalancingv2 as elbv2,
    aws_wafv2 as wafv2,
)
import cdk_nag
from constructs import Construct


def _build_cf_function_code(gateway_token: str) -> str:
    """Build CloudFront Function code with the expected token embedded."""
    # If no token provided, fall back to presence-only check
    if not gateway_token:
        token_check = "!params.token || !params.token.value"
    else:
        # Embed token value for exact comparison.
        # CF Function code is only visible to IAM users with
        # cloudfront:GetFunction — not exposed to end users.
        escaped = gateway_token.replace("\\", "\\\\").replace("'", "\\'")
        token_check = f"!params.token || params.token.value !== '{escaped}'"

    return f"""
function handler(event) {{
    var request = event.request;
    var headers = request.headers;
    var params = request.querystring;

    // Allow WebSocket upgrade requests through — OpenClaw gateway handles
    // its own token-based auth on the WebSocket connection.
    // Note: CloudFront strips hop-by-hop headers (Upgrade, Connection)
    // before passing to Functions, so we detect WebSocket via the
    // Sec-WebSocket-Version header which is always present in WS handshakes.
    if (headers['sec-websocket-version']) {{
        return request;
    }}

    // Validate token value in query string
    if ({token_check}) {{
        return {{
            statusCode: 403,
            statusDescription: 'Forbidden',
            headers: {{
                'content-type': {{ value: 'text/plain' }}
            }},
            body: {{ encoding: 'text', data: 'Access denied' }}
        }};
    }}

    return request;
}}
"""


class EdgeStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        alb: elbv2.IApplicationLoadBalancer,
        gateway_token: str = "",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        waf_rate_limit = self.node.try_get_context("waf_rate_limit") or 100

        # --- CloudFront Function for token auth ---------------------------
        auth_function = cloudfront.Function(
            self,
            "TokenAuthFunction",
            code=cloudfront.FunctionCode.from_inline(
                _build_cf_function_code(gateway_token)
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
            comment="Validate access token in query string",
        )

        # --- WAF WebACL (REGIONAL scope on ALB) ---
        waf_acl = wafv2.CfnWebACL(
            self,
            "WafAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="OpenClawWaf",
                sampled_requests_enabled=True,
            ),
            rules=[
                # Rate limiting
                wafv2.CfnWebACL.RuleProperty(
                    name="RateLimit",
                    priority=1,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="OpenClawRateLimit",
                        sampled_requests_enabled=True,
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                            limit=waf_rate_limit,
                            aggregate_key_type="IP",
                        ),
                    ),
                ),
                # AWS Managed — Common Rule Set
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesCommonRuleSet",
                    priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="OpenClawCommonRules",
                        sampled_requests_enabled=True,
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesCommonRuleSet",
                        ),
                    ),
                ),
                # AWS Managed — Known Bad Inputs
                wafv2.CfnWebACL.RuleProperty(
                    name="AWSManagedRulesKnownBadInputs",
                    priority=3,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        metric_name="OpenClawBadInputs",
                        sampled_requests_enabled=True,
                    ),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS",
                            name="AWSManagedRulesKnownBadInputsRuleSet",
                        ),
                    ),
                ),
            ],
        )

        # --- CloudFront Distribution with HTTP Origin (public ALB) ----------
        # Using HTTP origin instead of VPC origin because VPC Origins do not
        # support WebSocket protocol upgrade (returns 502 on WS handshake).
        # The public ALB is restricted to CloudFront origin-facing IPs via
        # managed prefix list on the ALB security group.
        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.HttpOrigin(
                    alb.load_balancer_dns_name,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    read_timeout=Duration.seconds(60),
                    keepalive_timeout=Duration.seconds(60),
                    custom_headers={
                        "X-Forwarded-Proto": "https",
                    },
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=auth_function,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    ),
                ],
            ),
            default_root_object="index.html",
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            http_version=cloudfront.HttpVersion.HTTP2,
            comment="OpenClaw Web UI",
        )

        # Associate WAF with ALB (regional scope)
        wafv2.CfnWebACLAssociation(
            self,
            "WafAlbAssociation",
            resource_arn=alb.load_balancer_arn,
            web_acl_arn=waf_acl.attr_arn,
        )

        # --- Outputs ------------------------------------------------------
        CfnOutput(
            self,
            "CloudFrontUrl",
            value=f"https://{self.distribution.distribution_domain_name}",
            description="CloudFront Web UI URL (append ?token=<value>)",
        )

        # --- cdk-nag suppressions ---
        cdk_nag.NagSuppressions.add_resource_suppressions(
            self.distribution,
            [
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR1",
                    reason="Geo restrictions are intentionally not enabled — the service "
                    "is designed for global access via messaging platforms. Access control "
                    "is enforced via token-based authentication at the CloudFront Function layer.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR2",
                    reason="WAF is attached to the public ALB (regional scope) rather than "
                    "CloudFront directly, because the WAF WebACL uses REGIONAL scope to "
                    "stay in the same region as the ALB. Rate limiting, Common Rule Set, "
                    "and Known Bad Inputs rules are active on the ALB.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR3",
                    reason="CloudFront access logging is not enabled to minimize cost for this "
                    "single-user deployment. WAF logging on the ALB and Fargate container "
                    "logs provide sufficient request-level observability.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR4",
                    reason="Distribution uses the default CloudFront viewer certificate which "
                    "enforces TLS 1.2 via minimum_protocol_version=TLS_V1_2_2021. The "
                    "cdk-nag rule flags this because the default certificate's SslSupportMethod "
                    "is 'sni-only' but the rule expects a custom certificate. No custom domain "
                    "is configured for this deployment.",
                ),
                cdk_nag.NagPackSuppression(
                    id="AwsSolutions-CFR5",
                    reason="Origin uses HTTP_ONLY protocol because the ALB does not have "
                    "an SSL certificate. The ALB is restricted to CloudFront origin-facing "
                    "IPs via managed prefix list. End-to-end HTTPS would require an ACM "
                    "certificate on the ALB.",
                ),
            ],
        )
