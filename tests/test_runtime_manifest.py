import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.runtime_manifest import has_v2_warmup_evidence, validate_v2_enrollment


@pytest.fixture
def manifest():
    nonce = "a" * 32
    session = "deployment-warmup-" + nonce
    digest = "sha256:" + "b" * 64
    return {
        "schemaVersion": 1, "platformVersion": "V2",
        "agentRuntimeId": "openclaw_agent_v2-1234567890",
        "agentRuntimeArn": "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/openclaw_agent_v2-1234567890",
        "agentRuntimeVersion": "2", "endpointName": "release_2", "endpointPolicy": "IMMUTABLE",
        "imageUri": "123456789012.dkr.ecr.us-west-2.amazonaws.com/openclaw@" + digest,
        "imageDigest": digest, "configurationHash": "c" * 64,
        "warmup": {
            "mode": "chat", "status": "succeeded", "contractVersion": 1,
            "runtimePlatform": "V2", "phase": "USER_READY",
            "execution": {"engine": "openclaw", "phase": "completed"},
            "userId": session, "actorId": "deployment-warmup:" + nonce,
            "sessionId": session, "runtimeSessionId": session,
        },
    }


def test_valid_manifest_is_returned_without_mutation(manifest):
    before = copy.deepcopy(manifest)
    assert validate_v2_enrollment(manifest) is manifest
    assert manifest == before
    assert has_v2_warmup_evidence(manifest["warmup"])


@pytest.mark.parametrize("key", [
    "schemaVersion", "platformVersion", "endpointName", "endpointPolicy", "agentRuntimeVersion",
    "imageUri", "imageDigest", "configurationHash", "warmup",
])
def test_manifest_rejects_missing_metadata(manifest, key):
    del manifest[key]
    with pytest.raises(ValueError, match=key):
        validate_v2_enrollment(manifest)


@pytest.mark.parametrize("key,value", [
    ("schemaVersion", True), ("platformVersion", "V1"), ("endpointPolicy", "MUTABLE"),
    ("endpointName", "DEFAULT"), ("endpointName", "release-1234567890"),
    ("endpointName", []), ("agentRuntimeVersion", 2), ("agentRuntimeVersion", "0"),
    ("agentRuntimeVersion", "latest"), ("imageDigest", "sha256:short"),
    ("configurationHash", []), ("imageUri", "repo:latest"),
    ("imageUri", "repo@sha256:" + "f" * 64), ("warmup", []),
])
def test_manifest_rejects_invalid_metadata(manifest, key, value):
    manifest[key] = value
    with pytest.raises(ValueError, match=key):
        validate_v2_enrollment(manifest)


@pytest.mark.parametrize("key", [
    "contractVersion", "runtimePlatform", "phase", "execution",
    "userId", "actorId", "sessionId", "runtimeSessionId",
])
def test_warmup_requires_all_completion_and_binding_fields(manifest, key):
    del manifest["warmup"][key]
    with pytest.raises(ValueError):
        validate_v2_enrollment(manifest)
    assert not has_v2_warmup_evidence(manifest["warmup"])


@pytest.mark.parametrize("key,value", [
    ("contractVersion", True), ("contractVersion", "1"), ("mode", "ping"), ("status", "skipped"),
    ("runtimePlatform", "V1"), ("phase", "INITIALIZING"),
    ("execution", {"engine": "lightweight", "phase": "completed"}),
    ("execution", {"engine": "openclaw", "phase": "unknown_outcome"}),
    ("execution", None), ("userId", "another-user"), ("actorId", "deployment-warmup:" + "d" * 32),
    ("sessionId", "another-session"), ("runtimeSessionId", "production-session"),
    ("runtimeSessionId", "deployment-warmup-" + "d" * 32),
])
def test_warmup_rejects_shim_incomplete_or_mismatched_evidence(manifest, key, value):
    manifest["warmup"][key] = value
    with pytest.raises(ValueError):
        validate_v2_enrollment(manifest)


def test_plain_shim_acknowledgement_is_not_enrollment_evidence(manifest):
    manifest["warmup"] = {"mode": "chat", "status": "succeeded", "response": "ok"}
    with pytest.raises(ValueError, match="contractVersion"):
        validate_v2_enrollment(manifest)


@pytest.mark.parametrize("value", [None, [], "manifest"])
def test_malformed_manifest_raises_value_error(value):
    with pytest.raises(ValueError, match="manifest"):
        validate_v2_enrollment(value)


def test_validator_imports_without_site_packages_or_aws_sdk(manifest):
    root = Path(__file__).resolve().parents[1]
    code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from scripts.runtime_manifest import validate_v2_enrollment
manifest = json.load(sys.stdin)
assert validate_v2_enrollment(manifest) is manifest
assert not any(name.startswith(('boto3', 'botocore', 'aws_cdk')) for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(root)],
                            input=json.dumps(manifest), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
