"""Cold-start verified production composition for the capability gateway."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping

import boto3

from .catalog import compile_catalog
from .contracts import CapabilityCallV1, CapabilityCatalogV1, CapabilityResultV1
from .durable import DynamoAdmissionRepository, DynamoCapabilityLedger
from .gateway import CapabilityGateway

REQUIRED_REGION = "eu-west-1"
DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parent / "artifacts"
_RELEASE = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TABLE = re.compile(r"[A-Za-z0-9_.-]{3,255}")
_CALLER = re.compile(
    r"arn:aws:iam::[0-9]{12}:role/" r"openclaw-agentcore-execution-role-eu-west-1"
)


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{name} is required")
    return value


def load_packaged_catalog(
    env: Mapping[str, str],
    *,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> CapabilityCatalogV1:
    if not isinstance(env, Mapping):
        raise TypeError("capability composition environment must be a mapping")
    if _required_env(env, "AWS_REGION") != REQUIRED_REGION:
        raise RuntimeError("capability gateway region drift")
    release_commit = _required_env(env, "CAPABILITY_RELEASE_COMMIT")
    expected_digest = _required_env(env, "CAPABILITY_CATALOG_DIGEST")
    if _RELEASE.fullmatch(release_commit) is None:
        raise RuntimeError("capability release commit is invalid")
    if _DIGEST.fullmatch(expected_digest) is None:
        raise RuntimeError("capability catalog digest is invalid")
    configured_root = Path(artifact_root)
    _, catalog = compile_catalog(release_commit, configured_root / "schemas")
    if catalog.catalog_digest != expected_digest:
        raise RuntimeError("packaged capability catalog digest drift")
    return catalog


@dataclass(frozen=True, slots=True)
class ProductionCapabilityComposition:
    catalog: CapabilityCatalogV1
    repository: DynamoAdmissionRepository
    ledger: DynamoCapabilityLedger
    gateway: CapabilityGateway
    allowed_caller_arn: str

    def invoke(self, event: Any) -> CapabilityResultV1:
        if (
            not isinstance(event, Mapping)
            or set(event) != {"schema", "grant", "call"}
            or event.get("schema") != "personal-operator.capability-relay-envelope.v1"
        ):
            raise ValueError("capability relay envelope is invalid")
        call = CapabilityCallV1.from_mapping(event["call"])
        return self.gateway.invoke(
            call,
            {
                "callerArn": self.allowed_caller_arn,
                "turnGrant": event["grant"],
            },
        )


def build_production_composition(
    *,
    env: Mapping[str, str] = os.environ,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
    dynamodb_client: Any | None = None,
    clock: Callable[[], int] | None = None,
    compute_adapters: Mapping[str, Any] | None = None,
) -> ProductionCapabilityComposition:
    # The credential-free gateway Lambda holds no compute execution authority.
    # Compute wiring is an explicit, injected seam restricted to the two frozen
    # compute operations; reject any other operation before touching state.
    adapters: dict[str, Any] = {}
    if compute_adapters is not None:
        if set(compute_adapters) - {"compute.run", "compute.status"}:
            raise RuntimeError("only compute operations may be injected")
        adapters.update(compute_adapters)
    catalog = load_packaged_catalog(env, artifact_root=artifact_root)
    table_name = _required_env(env, "CAPABILITY_STATE_TABLE_NAME")
    caller_arn = _required_env(env, "CAPABILITY_ALLOWED_CALLER_ARN")
    if _TABLE.fullmatch(table_name) is None:
        raise RuntimeError("capability state table name is invalid")
    if _CALLER.fullmatch(caller_arn) is None:
        raise RuntimeError("capability caller ARN is invalid")
    trusted_clock = clock or (lambda: int(time.time()))
    if not callable(trusted_clock):
        raise TypeError("capability composition clock must be callable")
    client = dynamodb_client or boto3.client(
        "dynamodb",
        region_name=REQUIRED_REGION,
    )
    repository = DynamoAdmissionRepository(client=client, table_name=table_name)
    ledger = DynamoCapabilityLedger(client=client, table_name=table_name)
    gateway = CapabilityGateway(
        catalog=catalog,
        repository=repository,
        ledger=ledger,
        adapters=adapters,
        allowed_caller_arn=caller_arn,
        clock=trusted_clock,
    )
    return ProductionCapabilityComposition(
        catalog=catalog,
        repository=repository,
        ledger=ledger,
        gateway=gateway,
        allowed_caller_arn=caller_arn,
    )


@lru_cache(maxsize=1)
def get_production_composition() -> ProductionCapabilityComposition:
    return build_production_composition()


__all__ = [
    "DEFAULT_ARTIFACT_ROOT",
    "ProductionCapabilityComposition",
    "build_production_composition",
    "get_production_composition",
    "load_packaged_catalog",
]
