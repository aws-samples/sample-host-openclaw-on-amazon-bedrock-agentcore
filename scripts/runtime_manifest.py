"""Shared offline validation for AgentCore Runtime V2 enrollment evidence.

Checks immutable endpoint metadata, image/configuration hashes, and successful
chat warmup evidence bound to an isolated deployment actor and session.
Deployment tooling and CDK use the same contract before exposing a target for
new users. These checks validate supplied data; live service readback belongs
to the deployment controller.
"""

import re


def validate_v2_binding_evidence(body, expected):
    if not isinstance(body, dict):
        raise ValueError("V2 warmup evidence must be an object")
    execution = body.get("execution")
    if (body.get("runtimePlatform") != "V2" or body.get("phase") != "USER_READY"
            or not isinstance(execution, dict) or execution.get("engine") != "openclaw"
            or execution.get("phase") != "completed"):
        raise ValueError("V2 warmup lacks USER_READY and completed gateway evidence")
    for key in ("userId", "actorId", "sessionId", "runtimeSessionId"):
        if body.get(key) != expected[key]:
            raise ValueError(f"V2 warmup owner or session evidence does not match the request: {key}")


def validate_v2_warmup_evidence(warmup):
    if not isinstance(warmup, dict):
        raise ValueError("warmup must be an object")
    if type(warmup.get("contractVersion")) is not int or warmup["contractVersion"] != 1:
        raise ValueError("warmup.contractVersion must be integer 1")
    session_id = warmup.get("runtimeSessionId")
    prefix = "deployment-warmup-"
    if not isinstance(session_id, str) or not re.fullmatch(prefix + "[0-9a-f]{32}", session_id):
        raise ValueError("warmup.runtimeSessionId must identify an isolated deployment warmup")
    expected = {"userId": session_id, "sessionId": session_id, "runtimeSessionId": session_id,
                "actorId": "deployment-warmup:" + session_id[len(prefix):]}
    validate_v2_binding_evidence(warmup, expected)


def has_v2_warmup_evidence(warmup):
    try:
        validate_v2_warmup_evidence(warmup)
        return True
    except ValueError:
        return False


def validate_v2_enrollment(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1:
        raise ValueError("schemaVersion must be integer 1")
    if manifest.get("platformVersion") != "V2":
        raise ValueError("platformVersion must be V2")
    if manifest.get("endpointPolicy") != "IMMUTABLE":
        raise ValueError("endpointPolicy must be IMMUTABLE")
    endpoint = manifest.get("endpointName")
    if (not isinstance(endpoint, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,47}", endpoint)
            or endpoint == "DEFAULT"):
        raise ValueError("endpointName must be a custom endpoint name")
    runtime_version = manifest.get("agentRuntimeVersion")
    if not isinstance(runtime_version, str) or not re.fullmatch(r"[1-9][0-9]*", runtime_version):
        raise ValueError("agentRuntimeVersion must be a positive numeric version string")
    digest = manifest.get("imageDigest")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("imageDigest must be a SHA-256 digest")
    image_uri = manifest.get("imageUri")
    if not isinstance(image_uri, str) or not image_uri.endswith("@" + digest):
        raise ValueError("imageUri must be pinned to imageDigest")
    config_hash = manifest.get("configurationHash")
    if not isinstance(config_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", config_hash):
        raise ValueError("configurationHash must be a SHA-256 hash")
    warmup = manifest.get("warmup")
    if not isinstance(warmup, dict) or warmup.get("mode") != "chat" or warmup.get("status") != "succeeded":
        raise ValueError("warmup must record a successful chat")
    validate_v2_warmup_evidence(warmup)
    return manifest
