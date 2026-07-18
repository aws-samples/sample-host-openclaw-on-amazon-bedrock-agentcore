#!/usr/bin/env python3
"""Fail closed unless deployed AgentCore storage matches the frozen contract."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from typing import Any


REQUIRED_REGION = "eu-west-1"
REQUIRED_MOUNT = "/mnt/workspace"
RUNTIME_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}$")
RELEASE_ENDPOINT_PATTERN = re.compile(r"^release_[0-9a-f]{40}$")
VERSION_PATTERN = re.compile(r"^[1-9][0-9]{0,4}$")
RUNTIME_ARN_PATTERN = re.compile(
    r"^arn:aws:bedrock-agentcore:eu-west-1:[0-9]{12}:"
    r"agent/[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}:([1-9][0-9]{0,4})$"
)
ROLE_ARN_PATTERN = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$")
KMS_ARN_PATTERN = re.compile(
    r"^arn:aws:kms:eu-west-1:[0-9]{12}:key/[0-9A-Za-z-]+$"
)
TERMINAL_RUNTIME_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}
TERMINAL_ENDPOINT_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING"}
KNOWN_RUNTIME_STATES = TERMINAL_RUNTIME_FAILURES | {"CREATING", "UPDATING", "READY"}
KNOWN_ENDPOINT_STATES = TERMINAL_ENDPOINT_FAILURES | {"CREATING", "UPDATING", "READY"}


class StorageVerificationError(RuntimeError):
    """A deployed resource differs from the frozen storage contract."""


def _fail(message: str) -> None:
    raise StorageVerificationError(message)


def _require_client_region(client: Any, label: str) -> None:
    region = getattr(getattr(client, "meta", None), "region_name", None)
    if region != REQUIRED_REGION:
        _fail(f"{label} client region must be exactly {REQUIRED_REGION}; got {region!r}")


def _validate_inputs(
    *,
    runtime_id: str,
    expected_endpoint_id: str,
    endpoint_name: str,
    expected_runtime_arn: str,
    expected_role_arn: str,
    bucket: str,
    expected_kms_key_arn: str,
) -> None:
    if not RUNTIME_ID_PATTERN.fullmatch(runtime_id or ""):
        _fail("runtime ID is missing or noncanonical")
    if not RUNTIME_ID_PATTERN.fullmatch(expected_endpoint_id or ""):
        _fail("endpoint ID is missing or noncanonical")
    if not RELEASE_ENDPOINT_PATTERN.fullmatch(endpoint_name or ""):
        _fail("endpoint name is not an exact release endpoint")
    if not RUNTIME_ARN_PATTERN.fullmatch(expected_runtime_arn or ""):
        _fail("expected runtime ARN is missing, noncanonical, or outside eu-west-1")
    if not ROLE_ARN_PATTERN.fullmatch(expected_role_arn or ""):
        _fail("expected execution role ARN is missing or noncanonical")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket or ""):
        _fail("workspace bucket name is missing or noncanonical")
    if ".." in bucket or ".-" in bucket or "-." in bucket:
        _fail("workspace bucket name is unsafe")
    if not KMS_ARN_PATTERN.fullmatch(expected_kms_key_arn or ""):
        _fail("expected KMS key ARN is missing, noncanonical, or outside eu-west-1")


def _validate_runtime(
    response: dict[str, Any],
    *,
    runtime_id: str,
    expected_runtime_arn: str,
    expected_role_arn: str,
    expected_version: str | None = None,
) -> str:
    if response.get("agentRuntimeId") != runtime_id:
        _fail("AgentCore returned a different runtime ID")
    version = response.get("agentRuntimeVersion")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        _fail("AgentCore runtime version is missing or noncanonical")
    if expected_version is not None and version != expected_version:
        _fail("refetched AgentCore runtime version differs from endpoint live version")
    arn = response.get("agentRuntimeArn")
    match = RUNTIME_ARN_PATTERN.fullmatch(arn or "")
    if not match or match.group(1) != version:
        _fail("AgentCore runtime ARN is malformed or not bound to its version")
    if arn != expected_runtime_arn:
        _fail("AgentCore runtime ARN differs from the exact deployed ARN")
    if response.get("roleArn") != expected_role_arn:
        _fail("AgentCore runtime execution role differs from the expected role")
    filesystems = response.get("filesystemConfigurations")
    expected_filesystems = [{"sessionStorage": {"mountPath": REQUIRED_MOUNT}}]
    if filesystems != expected_filesystems:
        _fail("AgentCore filesystem configuration must be one exact session-storage mount")
    return version


def _validate_endpoint(
    response: dict[str, Any],
    *,
    expected_endpoint_id: str,
    endpoint_name: str,
    expected_runtime_arn: str,
    expected_version: str,
) -> None:
    if response.get("id") != expected_endpoint_id:
        _fail("AgentCore endpoint ID differs from the expected endpoint")
    if response.get("name") != endpoint_name:
        _fail("AgentCore endpoint name differs from the requested endpoint")
    live = response.get("liveVersion")
    target = response.get("targetVersion")
    if (
        not isinstance(live, str)
        or not VERSION_PATTERN.fullmatch(live)
        or not isinstance(target, str)
        or not VERSION_PATTERN.fullmatch(target)
        or live != target
        or live != expected_version
    ):
        _fail("AgentCore endpoint live and target version must equal the exact runtime version")
    if response.get("agentRuntimeArn") != expected_runtime_arn:
        _fail("AgentCore endpoint runtime ARN differs from the exact runtime ARN")


def _verify_bucket(
    s3_client: Any,
    *,
    bucket: str,
    expected_kms_key_arn: str,
) -> None:
    location = s3_client.get_bucket_location(Bucket=bucket).get(
        "LocationConstraint"
    )
    if location != REQUIRED_REGION:
        _fail(f"workspace bucket region must be exactly {REQUIRED_REGION}")

    versioning = s3_client.get_bucket_versioning(Bucket=bucket)
    if versioning.get("Status") != "Enabled":
        _fail("workspace bucket versioning must be enabled")

    encryption = s3_client.get_bucket_encryption(Bucket=bucket).get(
        "ServerSideEncryptionConfiguration", {}
    )
    rules = encryption.get("Rules")
    expected_encryption = {
        "SSEAlgorithm": "aws:kms",
        "KMSMasterKeyID": expected_kms_key_arn,
    }
    if (
        not isinstance(rules, list)
        or len(rules) != 1
        or rules[0].get("ApplyServerSideEncryptionByDefault")
        != expected_encryption
        or rules[0].get("BucketKeyEnabled") is not True
    ):
        _fail("workspace bucket must use the exact KMS key with bucket keys enabled")

    public = s3_client.get_public_access_block(Bucket=bucket).get(
        "PublicAccessBlockConfiguration", {}
    )
    required_public_block = {
        "BlockPublicAcls": True,
        "IgnorePublicAcls": True,
        "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True,
    }
    if public != required_public_block:
        _fail("workspace bucket public access must be fully blocked")


def verify_agentcore_storage(
    *,
    control_client: Any,
    s3_client: Any,
    runtime_id: str,
    expected_endpoint_id: str,
    endpoint_name: str,
    expected_runtime_arn: str,
    expected_role_arn: str,
    bucket: str,
    expected_kms_key_arn: str,
    timeout_seconds: float = 600,
    poll_seconds: float = 5,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, str]:
    """Poll, refetch, and verify the exact deployed storage boundary."""

    if timeout_seconds <= 0 or poll_seconds < 0:
        _fail("verification timeout and polling interval are invalid")
    _require_client_region(control_client, "AgentCore control")
    _require_client_region(s3_client, "S3")
    _validate_inputs(
        runtime_id=runtime_id,
        expected_endpoint_id=expected_endpoint_id,
        endpoint_name=endpoint_name,
        expected_runtime_arn=expected_runtime_arn,
        expected_role_arn=expected_role_arn,
        bucket=bucket,
        expected_kms_key_arn=expected_kms_key_arn,
    )
    expected_runtime_match = RUNTIME_ARN_PATTERN.fullmatch(expected_runtime_arn)
    assert expected_runtime_match is not None
    expected_version = expected_runtime_match.group(1)

    deadline = monotonic() + timeout_seconds
    while True:
        exact = control_client.get_agent_runtime(
            agentRuntimeId=runtime_id,
            agentRuntimeVersion=expected_version,
        )
        runtime_status = exact.get("status")
        if runtime_status not in KNOWN_RUNTIME_STATES:
            _fail(f"AgentCore returned unknown runtime status {runtime_status!r}")
        if runtime_status in TERMINAL_RUNTIME_FAILURES:
            _fail(f"AgentCore runtime entered failed terminal status {runtime_status}")

        endpoint = control_client.get_agent_runtime_endpoint(
            agentRuntimeId=runtime_id,
            endpointName=endpoint_name,
        )
        endpoint_status = endpoint.get("status")
        if endpoint_status not in KNOWN_ENDPOINT_STATES:
            _fail(f"AgentCore returned unknown endpoint status {endpoint_status!r}")
        if endpoint_status in TERMINAL_ENDPOINT_FAILURES:
            _fail(f"AgentCore endpoint entered failed terminal status {endpoint_status}")

        if runtime_status == "READY" and endpoint_status == "READY":
            exact_version = _validate_runtime(
                exact,
                runtime_id=runtime_id,
                expected_runtime_arn=expected_runtime_arn,
                expected_role_arn=expected_role_arn,
                expected_version=expected_version,
            )
            _validate_endpoint(
                endpoint,
                expected_endpoint_id=expected_endpoint_id,
                endpoint_name=endpoint_name,
                expected_runtime_arn=expected_runtime_arn,
                expected_version=exact_version,
            )
            _verify_bucket(
                s3_client,
                bucket=bucket,
                expected_kms_key_arn=expected_kms_key_arn,
            )
            return {
                "bucket": bucket,
                "endpointId": expected_endpoint_id,
                "endpointName": endpoint_name,
                "mountPath": REQUIRED_MOUNT,
                "region": REQUIRED_REGION,
                "runtimeArn": expected_runtime_arn,
                "runtimeId": runtime_id,
                "runtimeVersion": exact_version,
            }

        if monotonic() >= deadline:
            _fail("timed out before AgentCore runtime and endpoint became READY")
        sleep(poll_seconds)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--endpoint-id", required=True)
    parser.add_argument("--endpoint-name", required=True)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--execution-role-arn", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--kms-key-arn", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=600)
    parser.add_argument("--poll-seconds", type=float, default=5)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    import boto3

    control = boto3.client("bedrock-agentcore-control", region_name=REQUIRED_REGION)
    s3 = boto3.client("s3", region_name=REQUIRED_REGION)
    try:
        evidence = verify_agentcore_storage(
            control_client=control,
            s3_client=s3,
            runtime_id=args.runtime_id,
            expected_endpoint_id=args.endpoint_id,
            endpoint_name=args.endpoint_name,
            expected_runtime_arn=args.runtime_arn,
            expected_role_arn=args.execution_role_arn,
            bucket=args.bucket,
            expected_kms_key_arn=args.kms_key_arn,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except StorageVerificationError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
