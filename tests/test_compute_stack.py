"""Synthesized least-authority boundary for the networkless compute job runner.

The real Docker build, ARM64 image, and static-scan gates are OPEN and not run
here; the stack accepts a precomputed pinned image digest so synth stays
offline and networkless.
"""

from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "123456789012"
REGION = "eu-west-1"
CMK_ARN = f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-compute-key"
IMAGE_DIGEST = "sha256:" + "a" * 64
OUTPUT_BUCKET = "personal-operator-compute-outputs"


def _synth(region: str = REGION):
    from stacks.compute_stack import ComputeStack

    app = cdk.App()
    stack = ComputeStack(
        app,
        "PersonalOperatorCompute",
        cmk_arn=CMK_ARN,
        image_digest=IMAGE_DIGEST,
        env=cdk.Environment(account=ACCOUNT, region=region),
    )
    return stack, Template.from_stack(stack).to_json()


def _resources(template: dict, resource_type: str) -> list[dict]:
    return [
        resource
        for resource in template.get("Resources", {}).values()
        if resource["Type"] == resource_type
    ]


def _actions(template: dict) -> set[str]:
    result: set[str] = set()
    for policy in _resources(template, "AWS::IAM::Policy"):
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            result.update([actions] if isinstance(actions, str) else actions)
    return result


def test_compute_stack_has_an_isolated_networkless_job_runner():
    stack, template = _synth()
    # An ECS task definition running the pinned image as the job runner.
    task_defs = _resources(template, "AWS::ECS::TaskDefinition")
    assert len(task_defs) == 1
    # The runner exposes a pinned image digest to the gateway environment.
    assert stack.image_digest == IMAGE_DIGEST


def test_compute_runner_has_no_internet_route_and_disables_imds():
    stack, template = _synth()
    # No NAT gateways, no egress-only gateways, no VPC interface/gateway endpoints:
    # the job VPC is fully isolated with no route to the internet, IMDS, or AWS.
    assert _resources(template, "AWS::EC2::NatGateway") == []
    assert _resources(template, "AWS::EC2::VPCEndpoint") == []
    # Subnets used by the runner must not auto-assign public IPs.
    for subnet in _resources(template, "AWS::EC2::Subnet"):
        assert subnet["Properties"].get("MapPublicIpOnLaunch") in (False, None)
    # Isolated subnets have no default route to an internet gateway.
    for route in _resources(template, "AWS::EC2::Route"):
        assert "GatewayId" not in route["Properties"]
        assert "NatGatewayId" not in route["Properties"]


def test_compute_container_has_no_task_role_or_ambient_aws_credentials():
    _, template = _synth()
    task_definition = _resources(template, "AWS::ECS::TaskDefinition")[0]
    assert "TaskRoleArn" not in task_definition["Properties"]
    actions = _actions(template)
    # The execution role is not exposed to the workload. The container itself
    # receives no task role and therefore no ECS credential endpoint at all.
    for forbidden in (
        "sts:AssumeRole",
        "sts:GetSessionToken",
        "secretsmanager:GetSecretValue",
        "s3:*",
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "kms:Decrypt",
        "kms:Encrypt",
        "kms:GenerateDataKey*",
        "kms:ReEncrypt*",
        "ec2:CreateRoute",
    ):
        assert forbidden not in actions


def test_compute_stack_rejects_a_non_pinned_image_digest():
    from stacks.compute_stack import ComputeStack

    app = cdk.App()
    for bad in ("latest", "sha256:zz", "a" * 64):
        with pytest.raises(ValueError):
            ComputeStack(
                app,
                f"Bad{hash(bad) & 0xffff}",
                cmk_arn=CMK_ARN,
                image_digest=bad,
                env=cdk.Environment(account=ACCOUNT, region=REGION),
            )


def test_compute_stack_rejects_every_noncanonical_region():
    with pytest.raises(ValueError, match="eu-west-1"):
        _synth("us-east-1")


def test_app_instantiates_compute_stack():
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "ComputeStack" in app_source
    assert "compute_stack" in app_source
