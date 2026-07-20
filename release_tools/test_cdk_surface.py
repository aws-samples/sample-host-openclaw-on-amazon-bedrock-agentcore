from __future__ import annotations

import importlib.metadata
import inspect
from pathlib import Path

from aws_cdk import aws_bedrockagentcore as agentcore


ROOT = Path(__file__).resolve().parents[1]


def test_release_cdk_surface_is_pinned_to_the_verified_library() -> None:
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()

    assert "aws-cdk-lib==2.261.0" in requirements
    assert importlib.metadata.version("aws-cdk-lib") == "2.261.0"


def test_agentcore_l1_constructor_surface_matches_the_release_contract() -> None:
    runtime_parameters = inspect.signature(agentcore.CfnRuntime.__init__).parameters
    endpoint_parameters = inspect.signature(
        agentcore.CfnRuntimeEndpoint.__init__
    ).parameters

    assert {
        "agent_runtime_artifact",
        "agent_runtime_name",
        "network_configuration",
        "role_arn",
        "filesystem_configurations",
        "lifecycle_configuration",
        "protocol_configuration",
    }.issubset(runtime_parameters)
    assert {
        "agent_runtime_id",
        "agent_runtime_version",
        "name",
    }.issubset(endpoint_parameters)

    artifact = agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
        container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
            container_uri="123456789012.dkr.ecr.eu-west-1.amazonaws.com/"
            "personal-operator/bridge@sha256:" + "a" * 64
        )
    )
    network = agentcore.CfnRuntime.NetworkConfigurationProperty(
        network_mode="VPC",
        network_mode_config=agentcore.CfnRuntime.VpcConfigProperty(
            subnets=["subnet-00000000000000000"],
            security_groups=["sg-00000000000000000"],
        ),
    )
    filesystem = agentcore.CfnRuntime.FilesystemConfigurationProperty(
        session_storage=agentcore.CfnRuntime.SessionStorageConfigurationProperty(
            mount_path="/mnt/workspace"
        )
    )
    lifecycle = agentcore.CfnRuntime.LifecycleConfigurationProperty(
        idle_runtime_session_timeout=1800,
        max_lifetime=28800,
    )

    assert artifact.container_configuration.container_uri.endswith("a" * 64)
    assert network.network_mode == "VPC"
    assert filesystem.session_storage.mount_path == "/mnt/workspace"
    assert lifecycle.idle_runtime_session_timeout == 1800
