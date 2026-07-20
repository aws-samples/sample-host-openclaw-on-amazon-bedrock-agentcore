"""Trusted workspace credential broker Lambda.

Only this Lambda may assume the bucket-wide workspace base role.  Every call
must carry a router-minted user/session capability, and the claimed session
must still be the strongly consistent live runtime mapping before STS is called.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

try:
    from router.workspace_capability import (
        WorkspaceCapabilityError,
        verify_workspace_capability,
    )
except ImportError:  # direct trusted Lambda asset
    from workspace_capability import WorkspaceCapabilityError, verify_workspace_capability


REQUIRED_REGION = "eu-west-1"
ACTIVE_RUNTIME_STATES = frozenset({"BUSY", "IDLE"})
STS_CREDENTIAL_TTL_SECONDS = 900
STS_EXPIRATION_TOLERANCE_SECONDS = 30
BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
ROLE_ARN_PATTERN = re.compile(
    r"arn:aws:iam::(?P<account>[0-9]{12}):role/"
    r"openclaw-workspace-session-role-eu-west-1"
)
CMK_ARN_PATTERN = re.compile(
    r"arn:aws:kms:eu-west-1:(?P<account>[0-9]{12}):key/[A-Za-z0-9-]+"
)
RUNTIME_ARN_PATTERN = re.compile(
    r"arn:aws:bedrock-agentcore:eu-west-1:(?P<account>[0-9]{12}):agent/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12}:[1-9][0-9]{0,4}"
)
RUNTIME_QUALIFIER_PATTERN = re.compile(r"release_[0-9a-f]{40}")


class WorkspaceBrokerAuthorizationError(PermissionError):
    """No credentials were issued because caller authority was invalid."""


class WorkspaceBrokerConfigurationError(RuntimeError):
    """Deployment configuration cannot uphold the broker boundary."""


def build_workspace_session_policy(
    *, bucket: str, namespace: str, cmk_arn: str, account: str
) -> str:
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": f"arn:aws:s3:::{bucket}/{namespace}/*",
            },
            {
                "Effect": "Allow",
                "Action": "s3:ListBucket",
                "Resource": f"arn:aws:s3:::{bucket}",
                "Condition": {"StringLike": {"s3:prefix": f"{namespace}/*"}},
            },
            {
                "Effect": "Allow",
                "Action": ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"],
                "Resource": cmk_arn,
                "Condition": {
                    "StringEquals": {
                        "kms:ViaService": "s3.eu-west-1.amazonaws.com",
                        "kms:CallerAccount": account,
                    }
                },
            },
        ],
    }
    return json.dumps(policy, sort_keys=True, separators=(",", ":"))


class WorkspaceCredentialBroker:
    def __init__(
        self,
        *,
        runtime_state_table,
        sts_client,
        key_provider: Callable[[], bytes | str],
        audience: str,
        workspace_role_arn: str,
        workspace_bucket: str,
        workspace_cmk_arn: str,
        runtime_arn: str,
        runtime_qualifier: str,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        if not callable(key_provider) or not callable(clock):
            raise TypeError("broker key provider and clock must be callable")
        role_match = ROLE_ARN_PATTERN.fullmatch(str(workspace_role_arn or ""))
        cmk_match = CMK_ARN_PATTERN.fullmatch(str(workspace_cmk_arn or ""))
        if role_match is None or cmk_match is None:
            raise WorkspaceBrokerConfigurationError("broker role or CMK ARN is invalid")
        if role_match.group("account") != cmk_match.group("account"):
            raise WorkspaceBrokerConfigurationError("broker role and CMK accounts differ")
        runtime_match = RUNTIME_ARN_PATTERN.fullmatch(str(runtime_arn or ""))
        if (
            runtime_match is None
            or runtime_match.group("account") != role_match.group("account")
            or RUNTIME_QUALIFIER_PATTERN.fullmatch(str(runtime_qualifier or ""))
            is None
        ):
            raise WorkspaceBrokerConfigurationError(
                "broker runtime binding is invalid"
            )
        if (
            BUCKET_PATTERN.fullmatch(str(workspace_bucket or "")) is None
            or ".." in workspace_bucket
            or ".-" in workspace_bucket
            or "-." in workspace_bucket
        ):
            raise WorkspaceBrokerConfigurationError("workspace bucket is invalid")
        self._table = runtime_state_table
        self._sts = sts_client
        self._key_provider = key_provider
        self._audience = audience
        self._role_arn = workspace_role_arn
        self._bucket = workspace_bucket
        self._cmk_arn = workspace_cmk_arn
        self._account = role_match.group("account")
        self._runtime_arn = runtime_arn
        self._runtime_qualifier = runtime_qualifier
        self._clock = clock

    def issue(self, event) -> dict:
        if not isinstance(event, Mapping) or set(event) != {"capability"}:
            raise WorkspaceBrokerAuthorizationError(
                "broker accepts only one signed capability"
            )
        try:
            claims = verify_workspace_capability(
                event["capability"],
                key=self._key_provider(),
                audience=self._audience,
                now=self._clock(),
            )
        except (TypeError, ValueError, WorkspaceCapabilityError) as error:
            raise WorkspaceBrokerAuthorizationError(
                "workspace capability is invalid"
            ) from error

        response = self._table.get_item(
            Key={"userId": claims["sub"]}, ConsistentRead=True
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        if (
            not isinstance(item, Mapping)
            or item.get("userId") != claims["sub"]
            or item.get("sessionId") != claims["sessionId"]
            or item.get("state") not in ACTIVE_RUNTIME_STATES
            or item.get("tombstonedAt") is not None
            or item.get("runtimeArn") != self._runtime_arn
            or item.get("runtimeQualifier") != self._runtime_qualifier
        ):
            raise WorkspaceBrokerAuthorizationError(
                "workspace capability session is no longer active"
            )

        suffix = hashlib.sha256(
            (
                "personal-operator-workspace-session-v1\0"
                f"{claims['sub']}\0{claims['sessionId']}"
            ).encode("utf-8")
        ).hexdigest()[:24]
        response = self._sts.assume_role(
            RoleArn=self._role_arn,
            RoleSessionName=f"workspace-{suffix}",
            DurationSeconds=STS_CREDENTIAL_TTL_SECONDS,
            Policy=build_workspace_session_policy(
                bucket=self._bucket,
                namespace=claims["namespace"],
                cmk_arn=self._cmk_arn,
                account=self._account,
            ),
        )
        credentials = response.get("Credentials") if isinstance(response, Mapping) else None
        if not isinstance(credentials, Mapping):
            raise RuntimeError("STS returned no workspace credentials")
        access_key = credentials.get("AccessKeyId")
        secret_key = credentials.get("SecretAccessKey")
        session_token = credentials.get("SessionToken")
        expiration = credentials.get("Expiration")
        if (
            not isinstance(access_key, str)
            or not access_key
            or not isinstance(secret_key, str)
            or not secret_key
            or not isinstance(session_token, str)
            or not session_token
            or not isinstance(expiration, datetime)
        ):
            raise RuntimeError("STS returned malformed workspace credentials")
        if expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
        now = float(self._clock())
        if expiration.timestamp() <= now:
            raise RuntimeError("STS returned expired workspace credentials")
        if expiration.timestamp() > (
            now
            + STS_CREDENTIAL_TTL_SECONDS
            + STS_EXPIRATION_TOLERANCE_SECONDS
        ):
            raise RuntimeError(
                "STS workspace credential lifetime exceeded its fixed maximum"
            )
        return {
            "Version": 1,
            "AccessKeyId": access_key,
            "SecretAccessKey": secret_key,
            "SessionToken": session_token,
            "Expiration": expiration.astimezone(timezone.utc).isoformat(),
        }


_broker: WorkspaceCredentialBroker | None = None


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise WorkspaceBrokerConfigurationError(f"broker requires {name}")
    return value


def _production_broker() -> WorkspaceCredentialBroker:
    global _broker
    if _broker is not None:
        return _broker
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if region != REQUIRED_REGION:
        raise WorkspaceBrokerConfigurationError(
            f"broker requires exact {REQUIRED_REGION} region"
        )

    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=3,
        read_timeout=5,
        retries={"max_attempts": 0},
    )
    dynamodb = boto3.resource("dynamodb", region_name=region, config=config)
    sts = boto3.client("sts", region_name=region, config=config)
    secrets = boto3.client("secretsmanager", region_name=region, config=config)
    secret_id = _required("WORKSPACE_CAPABILITY_SECRET_ID")
    key_cache: list[str] = []

    def key_provider() -> str:
        if not key_cache:
            response = secrets.get_secret_value(SecretId=secret_id)
            value = response.get("SecretString")
            if not isinstance(value, str) or len(value.encode("utf-8")) < 32:
                raise WorkspaceBrokerConfigurationError(
                    "workspace capability secret is invalid"
                )
            key_cache.append(value)
        return key_cache[0]

    _broker = WorkspaceCredentialBroker(
        runtime_state_table=dynamodb.Table(_required("RUNTIME_STATE_TABLE_NAME")),
        sts_client=sts,
        key_provider=key_provider,
        audience=_required("WORKSPACE_CREDENTIAL_BROKER_FUNCTION_NAME"),
        workspace_role_arn=_required("WORKSPACE_SESSION_ROLE_ARN"),
        workspace_bucket=_required("S3_USER_FILES_BUCKET"),
        workspace_cmk_arn=_required("CMK_ARN"),
        runtime_arn=_required("AGENTCORE_RUNTIME_ARN"),
        runtime_qualifier=_required("AGENTCORE_QUALIFIER"),
    )
    return _broker


def lambda_handler(event, _context):
    return _production_broker().issue(event)
