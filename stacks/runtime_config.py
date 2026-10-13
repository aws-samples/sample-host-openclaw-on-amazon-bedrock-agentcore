"""Offline validation of the AgentCore Runtime V2 deployment manifest for CDK.

Loads a local manifest, checks its runtime ARN against the deployment account
and region, and applies the shared enrollment evidence contract. Converts the
validated release into the target fields used by dependent stacks. An omitted
path returns no target so foundation stacks can be deployed before a release
exists; this module does not perform live service readback.
"""

import json
from pathlib import Path
import re

from scripts.runtime_manifest import validate_v2_enrollment


def load_runtime_manifest(path, region, account):
    if not path:
        return None
    manifest = json.loads(Path(path).read_text())
    if not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
        raise ValueError("Unsupported runtime manifest schemaVersion")
    if manifest.get("platformVersion") != "V2":
        raise ValueError("AgentCore Runtime manifest must specify platformVersion V2")
    runtime_id = manifest.get("agentRuntimeId", "")
    if not isinstance(runtime_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", runtime_id):
        raise ValueError("Invalid runtime ID")
    arn = manifest.get("agentRuntimeArn")
    if arn != f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}":
        raise ValueError("AgentCore Runtime ARN must match its ID, account and region")
    endpoint = manifest.get("endpointName")
    if not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", endpoint):
        raise ValueError("AgentCore Runtime endpointName must be a name, not an endpoint ID or ARN")
    validate_v2_enrollment(manifest)
    return manifest


def runtime_target_from_manifest(manifest):
    if manifest is None:
        return None
    validate_v2_enrollment(manifest)
    return {
        "runtimeArn": manifest["agentRuntimeArn"], "endpointName": manifest["endpointName"],
        "runtimeVersion": manifest["agentRuntimeVersion"], "platformVersion": "V2",
        "imageDigest": manifest["imageDigest"], "configurationHash": manifest["configurationHash"],
    }
