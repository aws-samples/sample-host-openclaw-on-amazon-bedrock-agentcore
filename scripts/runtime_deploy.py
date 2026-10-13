#!/usr/bin/env python3
"""SDK deployment controller for AgentCore Runtime V2 and immutable releases.

Merges partial overrides with service readback to preserve existing settings,
pins the ECR image by digest, and verifies the deployed configuration and
endpoint version. Release endpoints start at release_1 and cannot be retargeted;
changed releases require a new endpoint name.

Records deployment identity and full-chat warmup evidence in
.runtime/runtime-v2.json by default. Enrollment requires successful warmup,
even when an artifact was prepared with --skip-warmup. The CLI requires
--region and the SDK versions in requirements-deploy.lock; deploy and verify
use AWS credentials, while preflight checks the local SDK without AWS calls.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters

from postdeploy_warmup import WarmupError, run_warmup
from runtime_manifest import has_v2_warmup_evidence, validate_v2_enrollment


SDK_VERSIONS = {"boto3": "1.43.95", "botocore": "1.43.95"}
MUTABLE_FIELDS = {
    "agentRuntimeArtifact", "roleArn", "networkConfiguration", "description",
    "authorizerConfiguration", "requestHeaderConfiguration", "protocolConfiguration",
    "lifecycleConfiguration", "metadataConfiguration", "environmentVariables",
    "filesystemConfigurations", "capacityProviderConfiguration", "platformVersion",
}
V2_ENV_LIMIT = 2500
V2_REGIONS = {"us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-northeast-1"}
NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
IMAGE_PATTERN = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com"
    r"(?P<china>\.cn)?/(?P<repository>[a-z0-9][a-z0-9._/-]*)"
    r"(?:(?:@(?P<digest>sha256:[0-9a-f]{64}))|(?::(?P<tag>[\w.-]+)))$"
)
ECR_PREFIXES = ("openclaw-bridge", "openclaw_agent", "bedrock-agentcore-")


class DeploymentError(RuntimeError):
    pass


def check_sdk_versions():
    for package, expected in SDK_VERSIONS.items():
        if version(package) != expected:
            raise DeploymentError(f"Use {package}=={expected} from requirements-deploy.lock in the deployment venv")


def check_service_model(client):
    model = client.meta.service_model
    update = model.operation_model("UpdateAgentRuntime").input_shape
    actual = set(update.members) - {"agentRuntimeId", "clientToken"}
    if actual != MUTABLE_FIELDS:
        raise DeploymentError("SDK UpdateAgentRuntime fields differ from the reviewed preservation contract")
    required = {
        "CreateAgentRuntime": ({"platformVersion", "filesystemConfigurations"}, set()),
        "UpdateAgentRuntime": ({"platformVersion", "filesystemConfigurations"}, set()),
        "GetAgentRuntime": ({"agentRuntimeId", "agentRuntimeVersion"}, MUTABLE_FIELDS),
        "CreateAgentRuntimeEndpoint": ({"name", "agentRuntimeVersion"}, {"targetVersion"}),
        "GetAgentRuntimeEndpoint": ({"endpointName"}, {"name", "liveVersion", "targetVersion", "status"}),
    }
    for operation, (inputs, outputs) in required.items():
        op = model.operation_model(operation)
        if not inputs <= set(op.input_shape.members) or not outputs <= set(op.output_shape.members):
            raise DeploymentError(f"SDK service model lacks required fields: {operation}")
    for operation in ("CreateAgentRuntime", "UpdateAgentRuntime"):
        shape = model.operation_model(operation).input_shape
        platform_enum = shape.members["platformVersion"].enum
        # This verified release models platformVersion as an unconstrained string.
        if platform_enum and "V2" not in platform_enum:
            raise DeploymentError(f"SDK does not support V2: {operation}")
        filesystem = shape.members["filesystemConfigurations"].member
        if "mountPath" not in filesystem.members["sessionStorage"].members:
            raise DeploymentError("SDK does not support sessionStorage.mountPath")


def mutable_configuration(value):
    return {key: copy.deepcopy(value[key]) for key in MUTABLE_FIELDS if key in value}


def merge_configuration(base, overrides):
    result = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_configuration(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def environment_size(environment):
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()
    ):
        raise DeploymentError("environmentVariables must contain string keys and values")
    return sum(len(key.encode("utf-8")) + len(value.encode("utf-8"))
               for key, value in environment.items())


def build_configuration(client, current, overrides, image_uri, platform="V2", unset_env=()):
    if platform != "V2":
        raise DeploymentError("platform must be V2")
    for configuration in (current, overrides):
        if configuration and configuration.get("platformVersion", "V2") != "V2":
            raise DeploymentError("configuration platformVersion must be V2")
        if configuration.get("environmentVariables", {}).get("AGENTCORE_RUNTIME_PLATFORM", "V2") != "V2":
            raise DeploymentError("AGENTCORE_RUNTIME_PLATFORM must be V2")
    overrides = copy.deepcopy(overrides)
    configured_unsets = overrides.pop("unsetEnvironmentVariables", [])
    if not isinstance(configured_unsets, list) or any(not isinstance(key, str) for key in configured_unsets):
        raise DeploymentError("unsetEnvironmentVariables must be a list of environment keys")
    unknown = set(overrides) - MUTABLE_FIELDS
    if unknown:
        raise DeploymentError(f"unknown configuration fields: {', '.join(sorted(unknown))}")
    desired = merge_configuration(mutable_configuration(current), overrides)
    desired["platformVersion"] = "V2"
    desired["agentRuntimeArtifact"] = {"containerConfiguration": {"containerUri": image_uri}}
    env = desired.setdefault("environmentVariables", {})
    for key in [*configured_unsets, *unset_env]:
        env.pop(key, None)
    env["AGENTCORE_RUNTIME_PLATFORM"] = "V2"
    size = environment_size(env)
    limit = V2_ENV_LIMIT
    if size > limit:
        raise DeploymentError(
            f"environmentVariables total key/value UTF-8 bytes {size} exceeds {limit}; "
            "remove redundant overrides explicitly with --unset-env, or move static defaults "
            "into the image before deployment. Existing settings are not silently discarded."
        )
    if desired.get("networkConfiguration", {}).get("networkMode") != "VPC":
        raise DeploymentError("This image requires VPC networkConfiguration")
    fs = desired.setdefault("filesystemConfigurations", [])
    storage = [item["sessionStorage"] for item in fs if "sessionStorage" in item]
    if not storage:
        fs.append({"sessionStorage": {"mountPath": "/mnt/workspace"}})
    elif len(storage) != 1 or storage[0].get("mountPath") != "/mnt/workspace":
        raise DeploymentError("sessionStorage must mount at /mnt/workspace")
    desired.setdefault("protocolConfiguration", {"serverProtocol": "HTTP"})
    if desired["protocolConfiguration"]["serverProtocol"] != "HTTP":
        raise DeploymentError("Deployment warmup requires the HTTP protocol")
    validate_parameters(dict(desired, agentRuntimeId="validate-1234567890"),
                        client.meta.service_model.operation_model("UpdateAgentRuntime").input_shape)
    return desired


def configuration_hash(configuration):
    raw = json.dumps(mutable_configuration(configuration), sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_request(desired, name):
    if desired.get("platformVersion") != "V2":
        raise DeploymentError("platformVersion must be V2")
    if not NAME_PATTERN.fullmatch(name):
        raise DeploymentError("runtime name must match [a-zA-Z][a-zA-Z0-9_]{0,47}")
    request = copy.deepcopy(desired)
    request.pop("metadataConfiguration", None)
    request.get("networkConfiguration", {}).get("networkModeConfig", {}).pop("requireServiceS3Endpoint", None)
    request["agentRuntimeName"] = name
    request["clientToken"] = hashlib.sha256((name + configuration_hash(request)).encode()).hexdigest()
    return request


def update_request(desired, current, runtime_id):
    if desired.get("platformVersion") != "V2" or current.get("platformVersion") != "V2":
        raise DeploymentError("platformVersion must be V2")
    request = copy.deepcopy(desired)
    network = request.get("networkConfiguration", {}).get("networkModeConfig", {})
    current_network = current.get("networkConfiguration", {}).get("networkModeConfig", {})
    # New runtimes reject this field even unchanged; keep it in desired for readback.
    if ("requireServiceS3Endpoint" in network
            and network["requireServiceS3Endpoint"] == current_network.get("requireServiceS3Endpoint")):
        network.pop("requireServiceS3Endpoint")
    request["agentRuntimeId"] = runtime_id
    request["clientToken"] = hashlib.sha256(
        f"{runtime_id}:{current['agentRuntimeVersion']}:{configuration_hash(request)}".encode()
    ).hexdigest()
    return request


def verify_configuration(actual, desired):
    mismatched = [key for key, value in desired.items() if key in MUTABLE_FIELDS and actual.get(key) != value]
    if mismatched:
        # Never include environment/authorizer values in errors or manifests.
        raise DeploymentError("runtime readback differs for: " + ", ".join(sorted(mismatched)))


def wait_runtime(client, runtime_id, expected_version=None, *, timeout=1800, interval=5,
                 sleep=time.sleep, clock=time.monotonic):
    deadline = clock() + timeout
    while True:
        current = client.get_agent_runtime(agentRuntimeId=runtime_id)
        status = current.get("status")
        if status == "READY" and (expected_version is None or current.get("agentRuntimeVersion") == expected_version):
            return current
        if status not in {"CREATING", "UPDATING", "READY"}:
            raise DeploymentError(f"runtime did not become READY: {status}")
        if clock() >= deadline:
            raise DeploymentError("runtime READY/version readback timed out")
        sleep(interval)


def wait_endpoint(client, runtime_id, name, expected_version, *, timeout=1800, interval=5,
                  sleep=time.sleep, clock=time.monotonic, initial_endpoint=None, deadline=None):
    deadline = clock() + timeout if deadline is None else deadline
    endpoint = initial_endpoint
    while True:
        if endpoint is None:
            try:
                endpoint = client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
            except ClientError as error:
                if error.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
                endpoint = {"status": "CREATING"}
        status = endpoint.get("status")
        if status not in {"CREATING", "UPDATING", "READY"}:
            raise DeploymentError(f"endpoint did not become READY: {status}")
        # Missing versions can appear during creation; a different version is never this release.
        if (endpoint.get("name", name) != name
                or any(endpoint.get(key) not in {None, expected_version}
                       for key in ("liveVersion", "targetVersion"))):
            raise DeploymentError("V2 endpoints are immutable; choose a new --endpoint-name for this release")
        remaining = deadline - clock()
        # A zero timeout intentionally verifies one read; positive waits reject late READY responses.
        if timeout > 0 and remaining <= 0:
            raise DeploymentError("endpoint live/target version readback timed out")
        if (status == "READY" and endpoint.get("name") == name
                and endpoint.get("liveVersion") == expected_version
                and endpoint.get("targetVersion", expected_version) == expected_version):
            return endpoint
        if remaining <= 0:
            raise DeploymentError("endpoint live/target version readback timed out")
        sleep(min(interval, remaining))
        endpoint = None


def ensure_endpoint(client, runtime_id, name, runtime_version, *, timeout=1800, interval=5,
                    deadline=None, previously_observed=False):
    if not NAME_PATTERN.fullmatch(name) or name == "DEFAULT":
        raise DeploymentError("V2 requires a custom immutable endpoint name")
    deadline = time.monotonic() + timeout if deadline is None else deadline
    try:
        endpoint = client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        if previously_observed:
            raise DeploymentError("previously observed endpoint is missing; refusing to recreate") from error
        endpoint = None
    token = hashlib.sha256(f"{runtime_id}:{name}:{runtime_version}".encode()).hexdigest()
    if endpoint is None:
        if timeout > 0 and time.monotonic() >= deadline:
            raise DeploymentError("endpoint live/target version readback timed out")
        client.create_agent_runtime_endpoint(agentRuntimeId=runtime_id, name=name,
                                             agentRuntimeVersion=runtime_version, clientToken=token)
    return wait_endpoint(client, runtime_id, name, runtime_version, timeout=timeout, interval=interval,
                         initial_endpoint=endpoint, deadline=deadline)


def check_release_endpoint(client, runtime_id, name, current, desired, *, timeout=1800, interval=5,
                           deadline=None):
    deadline = time.monotonic() + timeout if deadline is None else deadline
    try:
        endpoint = client.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName=name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "ResourceNotFoundException":
            return
        raise
    changed = any(current.get(key) != value for key, value in desired.items())
    if changed:
        raise DeploymentError("V2 endpoints are immutable; choose a new --endpoint-name for this release")
    return wait_endpoint(client, runtime_id, name, current["agentRuntimeVersion"],
                         timeout=timeout, interval=interval, initial_endpoint=endpoint, deadline=deadline)


def validate_repository(name):
    if not name.startswith(ECR_PREFIXES):
        raise DeploymentError("ECR repository must use an execution-role prefix: " + ", ".join(ECR_PREFIXES))


def resolve_image(ecr, image_uri, region, account):
    match = IMAGE_PATTERN.fullmatch(image_uri)
    if not match or match["region"] != region or match["account"] != account or match["china"]:
        raise DeploymentError("image must be a tagged or digest ECR URI in the deployment account and region")
    validate_repository(match["repository"])
    image_id = {"imageDigest": match["digest"]} if match["digest"] else {"imageTag": match["tag"]}
    response = ecr.describe_images(registryId=match["account"], repositoryName=match["repository"], imageIds=[image_id])
    images = response.get("imageDetails", [])
    if len(images) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", images[0].get("imageDigest", "")):
        raise DeploymentError("ECR did not resolve exactly one image digest")
    digest = images[0]["imageDigest"]
    if match["digest"] and digest != match["digest"]:
        raise DeploymentError("ECR digest readback mismatch")
    repository_uri = image_uri.split("@")[0] if match["digest"] else image_uri.rsplit(":", 1)[0]
    return f"{repository_uri}@{digest}", digest


def verify_existing_callers(client, manifest):
    # Refuse cutover before CDK can remove permissions for saved user routes.
    for name in ("openclaw-router", "openclaw-cron-executor"):
        try:
            current = client.get_function_configuration(FunctionName=name)
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceNotFoundException":
                continue
            raise
        environment = current.get("Environment", {})
        if environment.get("Error"):
            raise DeploymentError(f"Cannot read {name} environment; existing routes require reconciliation")
        variables = environment.get("Variables", {})
        arn = variables.get("AGENTCORE_RUNTIME_ARN")
        if not arn:
            raise DeploymentError(f"{name} has no verified AgentCore Runtime binding; reconcile it before Phase 3")
        if arn != manifest["agentRuntimeArn"]:
            raise DeploymentError(f"{name} uses a different AgentCore Runtime; automatic cutover is not supported")
        if name == "openclaw-router":
            try:
                target = json.loads(variables.get("RUNTIME_TARGET", "null"))
            except (TypeError, ValueError):
                target = None
            if (not isinstance(target, dict) or target.get("platformVersion") != "V2"
                    or target.get("runtimeArn") != arn):
                raise DeploymentError("Existing Router is not a verified V2 deployment; user migration is not supported")


def write_manifest(path, manifest):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(manifest, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_manifest(path):
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or type(value.get("schemaVersion")) is not int
            or value["schemaVersion"] != 1):
        raise DeploymentError("unsupported deployment manifest")
    if value.get("platformVersion") != "V2":
        raise DeploymentError("manifest platformVersion must be V2")
    if not isinstance(value.get("agentRuntimeId"), str) or not value["agentRuntimeId"]:
        raise DeploymentError("manifest must identify the existing V2 runtime")
    return value


def verify_manifest(client, manifest, region, *, require_warmup=True, expected_platform="V2",
                    require_enrollment_ready=False):
    if expected_platform != "V2" or manifest.get("platformVersion") != "V2":
        raise DeploymentError("manifest platformVersion must be V2")
    if (manifest.get("endpointPolicy") != "IMMUTABLE"
            or not isinstance(manifest.get("endpointName"), str)
            or not NAME_PATTERN.fullmatch(manifest["endpointName"])
            or manifest["endpointName"] == "DEFAULT"):
        raise DeploymentError("manifest requires an immutable custom V2 endpoint")
    if manifest.get("region") != region:
        raise DeploymentError("manifest region differs from deployment region")
    if require_enrollment_ready:
        if not require_warmup:
            raise DeploymentError("enrollment requires successful chat warmup")
        try:
            validate_v2_enrollment(manifest)
        except ValueError as error:
            raise DeploymentError(f"enrollment requires a verified V2 manifest: {error}") from error
    allowed = {"succeeded"} if require_warmup else {"succeeded", "skipped"}
    if manifest.get("warmup", {}).get("status") not in allowed:
        raise DeploymentError("manifest lacks successful chat warmup")
    if (manifest["warmup"]["status"] == "succeeded"
            and not has_v2_warmup_evidence(manifest["warmup"])):
        raise DeploymentError("manifest lacks verified V2 gateway warmup evidence")
    current = client.get_agent_runtime(agentRuntimeId=manifest["agentRuntimeId"],
                                       agentRuntimeVersion=manifest["agentRuntimeVersion"])
    expected = {
        "agentRuntimeArn": manifest["agentRuntimeArn"],
        "agentRuntimeVersion": manifest["agentRuntimeVersion"],
        "platformVersion": manifest["platformVersion"], "status": "READY",
    }
    if any(current.get(key) != value for key, value in expected.items()):
        raise DeploymentError("manifest runtime identity/version/platform readback mismatch")
    if configuration_hash(current) != manifest["configurationHash"]:
        raise DeploymentError("manifest configurationHash readback mismatch")
    uri = current["agentRuntimeArtifact"]["containerConfiguration"]["containerUri"]
    if uri != manifest["imageUri"] or uri.rsplit("@", 1)[-1] != manifest["imageDigest"]:
        raise DeploymentError("manifest image digest readback mismatch")
    return wait_endpoint(client, current["agentRuntimeId"], manifest["endpointName"],
                         manifest["agentRuntimeVersion"], timeout=0)


def deploy_runtime(client, invoke_client, *, manifest_path, region, platform="V2", name="openclaw_agent_v2",
                   endpoint_name=None, image_uri, image_digest, overrides, runtime_id=None,
                   skip_warmup=False, unset_env=(), timeout=1800):
    if platform != "V2":
        raise DeploymentError("platform must be V2")
    check_service_model(client)
    manifest = load_manifest(manifest_path)
    if endpoint_name is None:
        endpoint_name = manifest.get("endpointName", "release_1")
    if (not NAME_PATTERN.fullmatch(name) or not isinstance(endpoint_name, str)
            or not NAME_PATTERN.fullmatch(endpoint_name)):
        raise DeploymentError("runtime and endpoint names must match [a-zA-Z][a-zA-Z0-9_]{0,47}")
    if endpoint_name == "DEFAULT":
        raise DeploymentError("V2 requires a custom immutable endpoint, not DEFAULT")
    if not IMAGE_PATTERN.fullmatch(image_uri) or not image_uri.endswith("@" + image_digest):
        raise DeploymentError("deployment requires a resolved image digest URI")
    if region not in V2_REGIONS:
        raise DeploymentError("V2 region is outside the reviewed supported region set")
    if manifest:
        if manifest.get("region") != region:
            raise DeploymentError("manifest region/platform does not match deployment")
        if runtime_id and runtime_id != manifest.get("agentRuntimeId"):
            raise DeploymentError("runtime ID conflicts with manifest")
        runtime_id = manifest.get("agentRuntimeId")
    current = {}
    if runtime_id:
        current = wait_runtime(client, runtime_id, timeout=timeout)
        if current.get("platformVersion") != "V2":
            raise DeploymentError("existing runtime platformVersion must be V2; in-place conversion is not supported")
    desired = build_configuration(client, current, overrides, image_uri, unset_env=unset_env)
    if desired.get("authorizerConfiguration", {}).get("customJWTAuthorizer"):
        raise DeploymentError("customJWTAuthorizer requires token-based invocation; this controller supports IAM warmup only")
    if not runtime_id or "requireServiceS3Endpoint" not in current.get("networkConfiguration", {}).get("networkModeConfig", {}):
        # Only round-trip this legacy flag for an existing runtime that returns it.
        desired.get("networkConfiguration", {}).get("networkModeConfig", {}).pop("requireServiceS3Endpoint", None)
    endpoint_deadline = None
    endpoint_observed = False
    if runtime_id:
        endpoint_deadline = time.monotonic() + timeout
        endpoint_observed = check_release_endpoint(
            client, runtime_id, endpoint_name, current, desired, timeout=timeout,
            deadline=endpoint_deadline,
        ) is not None
    previous = copy.deepcopy(manifest)
    manifest = {
        "schemaVersion": 1, "region": region, "platformVersion": "V2",
        "endpointName": endpoint_name, "imageUri": image_uri, "imageDigest": image_digest,
        "sdkVersions": SDK_VERSIONS, "updatedAt": datetime.now(timezone.utc).isoformat(),
        "warmup": {"mode": "chat", "status": "pending"},
    }
    manifest["endpointPolicy"] = "IMMUTABLE"
    warmup_in_progress = False
    preserve_previous = False
    warmup_attempted = False

    def persist_pending_warmup(attempt):
        nonlocal warmup_in_progress
        manifest["warmup"] = attempt
        write_manifest(manifest_path, manifest)
        warmup_in_progress = True

    try:
        if not runtime_id:
            created = client.create_agent_runtime(**create_request(desired, name))
            runtime_id = created["agentRuntimeId"]
            manifest.update(agentRuntimeId=runtime_id, agentRuntimeArn=created["agentRuntimeArn"],
                            agentRuntimeVersion=created["agentRuntimeVersion"])
            write_manifest(manifest_path, manifest)
            current = wait_runtime(client, runtime_id, created["agentRuntimeVersion"], timeout=timeout)
            network = current.get("networkConfiguration", {}).get("networkModeConfig", {})
            if "requireServiceS3Endpoint" in network:
                # Create omits this update-only flag; retain the service's chosen value.
                desired["networkConfiguration"].setdefault("networkModeConfig", {})[
                    "requireServiceS3Endpoint"
                ] = network["requireServiceS3Endpoint"]
            verify_configuration(current, {k: v for k, v in desired.items() if k != "metadataConfiguration"})
        if mutable_configuration(current) != desired:
            # Preserve service-populated defaults as well as inherited settings.
            desired = {**mutable_configuration(current), **desired}
            if any(current.get(key) != value for key, value in desired.items()):
                updated = client.update_agent_runtime(**update_request(desired, current, runtime_id))
                current = wait_runtime(client, runtime_id, updated["agentRuntimeVersion"], timeout=timeout)
        verify_configuration(current, desired)
        manifest.update(agentRuntimeId=runtime_id, agentRuntimeArn=current["agentRuntimeArn"],
                        agentRuntimeVersion=current["agentRuntimeVersion"],
                        configurationHash=configuration_hash(current))
        # Keep read-only retries intact, including failures during final verification.
        preserve_previous = all(previous.get(key) == manifest[key]
                                for key in ("endpointName", "agentRuntimeVersion", "configurationHash"))
        if not preserve_previous:
            write_manifest(manifest_path, manifest)
        ensure_endpoint(client, runtime_id, endpoint_name, current["agentRuntimeVersion"], timeout=timeout,
                        deadline=endpoint_deadline, previously_observed=endpoint_observed)
        if skip_warmup:
            manifest["warmup"] = {"mode": "chat", "status": "skipped", "reason": "explicit --skip-warmup"}
        elif (previous.get("configurationHash") == manifest["configurationHash"]
              and previous.get("agentRuntimeVersion") == manifest["agentRuntimeVersion"]
              and previous.get("endpointName") == endpoint_name
              and previous.get("warmup", {}).get("status") == "succeeded"
              and has_v2_warmup_evidence(previous["warmup"])):
            manifest["warmup"] = previous["warmup"]
        else:
            warmup_attempted = True
            manifest["warmup"] = run_warmup(invoke_client, current["agentRuntimeArn"], endpoint_name,
                                          runtime_version=current["agentRuntimeVersion"],
                                          before_invoke=persist_pending_warmup)
            warmup_in_progress = False
        verify_manifest(client, manifest, region, require_warmup=not skip_warmup)
        write_manifest(manifest_path, manifest)
        return manifest
    except Exception as error:
        if runtime_id and (not preserve_previous or warmup_attempted):
            manifest.setdefault("agentRuntimeId", runtime_id)
            status = "unknown" if warmup_in_progress and not isinstance(error, WarmupError) else "failed"
            manifest["warmup"].update(status=status, errorType=type(error).__name__)
            if isinstance(error, WarmupError):
                manifest["warmup"]["failureReason"] = str(error)
            write_manifest(manifest_path, manifest)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="AgentCore Runtime V2 deployment with immutable release endpoints")
    parser.add_argument("operation", choices=["preflight", "deploy", "verify"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--runtime-name")
    parser.add_argument("--runtime-id")
    parser.add_argument("--endpoint-name")
    parser.add_argument("--image-uri")
    parser.add_argument("--config", help="JSON partial overrides; nested objects are merged with Get readback")
    parser.add_argument("--unset-env", action="append", default=[])
    parser.add_argument("--skip-warmup", action="store_true", help="prepare or inspect an artifact; not eligible for enrollment")
    parser.add_argument("--require-enrollment-ready", action="store_true",
                        help="verify an immutable V2 endpoint with successful gateway warmup before enrollment")
    parser.add_argument("--check-existing-callers", action="store_true",
                        help="refuse replacing legacy callers or switching their AgentCore Runtime ARN")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)
    if args.check_existing_callers and (args.operation != "verify" or not args.require_enrollment_ready):
        parser.error("--check-existing-callers requires verify --require-enrollment-ready")
    check_sdk_versions()
    import boto3
    # Preflight must not resolve credentials or call AWS.
    if args.operation == "preflight":
        from botocore.session import Session
        from types import SimpleNamespace
        check_service_model(SimpleNamespace(meta=SimpleNamespace(
            service_model=Session().get_service_model("bedrock-agentcore-control"))))
        if args.region not in V2_REGIONS:
            raise DeploymentError("V2 region is outside the reviewed supported region set")
        print(json.dumps({"sdkVersions": SDK_VERSIONS, "serviceModel": "verified"}))
        return
    path = args.manifest or ".runtime/runtime-v2.json"
    manifest = load_manifest(path)
    client = boto3.client("bedrock-agentcore-control", region_name=args.region,
                          config=Config(retries={"mode": "standard", "total_max_attempts": 3}))
    check_service_model(client)
    if args.operation == "verify":
        verify_manifest(client, manifest, args.region, require_warmup=not args.skip_warmup,
                        require_enrollment_ready=args.require_enrollment_ready)
        if args.check_existing_callers:
            verify_existing_callers(boto3.client("lambda", region_name=args.region), manifest)
        print(json.dumps({"manifest": path, "verified": True, "warmup": manifest["warmup"]["status"]}))
        return
    if not args.image_uri:
        parser.error("deploy requires --image-uri")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    ecr = boto3.client("ecr", region_name=args.region)
    account = boto3.client("sts", region_name=args.region).get_caller_identity()["Account"]
    image_uri, digest = resolve_image(ecr, args.image_uri, args.region, account)
    invoke_client = boto3.client("bedrock-agentcore", region_name=args.region, config=Config(
        connect_timeout=10, read_timeout=900, retries={"total_max_attempts": 1},
    ))
    result = deploy_runtime(
        client, invoke_client, manifest_path=path, region=args.region,
        name=args.runtime_name or "openclaw_agent_v2",
        endpoint_name=args.endpoint_name,
        image_uri=image_uri, image_digest=digest,
        overrides=json.loads(Path(args.config).read_text()) if args.config else {},
        runtime_id=args.runtime_id,
        skip_warmup=args.skip_warmup, unset_env=args.unset_env, timeout=args.timeout,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # AWS errors may echo environment values; report their type without the request.
        print(f"ERROR: {error if isinstance(error, (DeploymentError, WarmupError)) else type(error).__name__}", file=sys.stderr)
        sys.exit(1)
