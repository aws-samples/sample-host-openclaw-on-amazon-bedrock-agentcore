"""Exact synthesized boundary for the AgentCore runtime execution role."""

from aws_cdk import App, Environment, Stack, aws_ec2 as ec2
from aws_cdk.assertions import Template

from stacks.agentcore_stack import AgentCoreStack


ACCOUNT = "123456789012"
REGION = "eu-west-1"
ROLE_NAME = "openclaw-agentcore-execution-role-eu-west-1"


def _template() -> dict[str, object]:
    app = App()
    env = Environment(account=ACCOUNT, region=REGION)
    network = Stack(app, "RuntimeIamNetwork", env=env)
    vpc = ec2.Vpc(network, "Vpc", max_azs=2, nat_gateways=0)
    endpoint_group = ec2.SecurityGroup(
        network,
        "EndpointGroup",
        vpc=vpc,
        allow_all_outbound=False,
    )
    stack = AgentCoreStack(
        app,
        "OpenClawAgentCore",
        cmk_arn=f"arn:aws:kms:{REGION}:{ACCOUNT}:key/test-key",
        vpc=vpc,
        private_subnet_ids=["subnet-00000000000000001"],
        trusted_endpoint_security_group=endpoint_group,
        s3_prefix_list_id="pl-6da54004",
        workspace_capability_secret_name=(
            "personal-operator/workspace-capability"
        ),
        capability_gateway_function_arn=(
            f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:"
            "personal-operator-capability-gateway"
        ),
        env=env,
    )
    return Template.from_stack(stack).to_json()


def test_runtime_execution_role_explicitly_freezes_iam_defaults() -> None:
    template = _template()
    roles = [
        resource
        for resource in template["Resources"].values()
        if resource.get("Type") == "AWS::IAM::Role"
        and resource.get("Properties", {}).get("RoleName") == ROLE_NAME
    ]

    assert len(roles) == 1
    properties = roles[0]["Properties"]
    assert properties["Path"] == "/"
    assert properties["MaxSessionDuration"] == 3600
    assert "Description" not in properties
    assert "PermissionsBoundary" not in properties
