"""Typed production composition for immutable ECR and AgentCore evidence."""

from __future__ import annotations

import hashlib
import re
from typing import Sequence

from release_tools.agentcore import AgentCoreClient, AgentCoreEvidenceAdapter
from release_tools.contracts import RuntimeContextV3, RuntimeImageEvidence
from release_tools.ecr import (
    ArtifactBlobReader,
    EcrClient,
    EcrEvidenceAdapter,
)


_COMMIT = re.compile(r"[0-9a-f]{40}")
_ACCOUNT = re.compile(r"[0-9]{12}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,99}-[A-Za-z0-9]{10}")
_VERSION = re.compile(r"[1-9][0-9]{0,4}")
_ROLE_ARN = re.compile(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9_+=,.@/-]+")
_REGION = "eu-west-1"


class ProductionObservationError(RuntimeError):
    """Live release evidence cannot prove the exact staged subject."""


class ProductionEvidenceComposer:
    """Call strict injected adapters and compose driver observation payloads.

    Construction performs validation only. It does not create SDK sessions,
    discover credentials, or contact AWS. Calls cross the live-read boundary
    only through the two explicitly injected adapters.
    """

    def __init__(
        self,
        *,
        ecr: EcrEvidenceAdapter,
        agentcore: AgentCoreEvidenceAdapter,
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
        build_context: str,
        builder_id: str,
        builder_inputs: Sequence[str],
        expected_role_arn: str,
    ) -> None:
        role = _ROLE_ARN.fullmatch(expected_role_arn)
        if (
            _COMMIT.fullmatch(source_commit) is None
            or _COMMIT.fullmatch(source_tree) is None
            or _ACCOUNT.fullmatch(account) is None
            or account == "000000000000"
            or region != _REGION
            or role is None
            or role.group(1) != account
            or not isinstance(build_context, str)
            or not 1 <= len(build_context) <= 256
            or not isinstance(builder_id, str)
            or not 1 <= len(builder_id) <= 512
            or isinstance(builder_inputs, (str, bytes))
            or not builder_inputs
            or len(builder_inputs) > 64
            or any(
                not isinstance(value, str) or _DIGEST.fullmatch(value) is None
                for value in builder_inputs
            )
            or len(set(builder_inputs)) != len(builder_inputs)
        ):
            raise ProductionObservationError(
                "production observation identity or builder inputs are invalid"
            )
        self._ecr = ecr
        self._agentcore = agentcore
        self._source_commit = source_commit
        self._source_tree = source_tree
        self._account = account
        self._region = region
        self._build_context = build_context
        self._builder_id = builder_id
        self._builder_inputs = tuple(builder_inputs)
        self._expected_role_arn = expected_role_arn

    def image_evidence(self) -> dict[str, object]:
        """Collect full content- and subject-authenticated image evidence."""

        evidence = self._ecr.collect(
            source_commit=self._source_commit,
            source_tree=self._source_tree,
            account=self._account,
            region=self._region,
            build_context=self._build_context,
            builder_id=self._builder_id,
            builder_inputs=self._builder_inputs,
        )
        if (
            not isinstance(evidence, RuntimeImageEvidence)
            or evidence.source_commit != self._source_commit
            or evidence.source_tree != self._source_tree
            or evidence.account != self._account
            or evidence.region != self._region
        ):
            raise ProductionObservationError(
                "ECR evidence identity differs from the release"
            )
        return {"runtime_image_evidence": evidence.to_mapping()}

    def _runtime_context(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> RuntimeContextV3:
        if (
            _RUNTIME_ID.fullmatch(runtime_id) is None
            or _VERSION.fullmatch(runtime_version) is None
            or _DIGEST.fullmatch(runtime_image_digest) is None
        ):
            raise ProductionObservationError(
                "AgentCore observation identity is invalid"
            )
        image_uri = (
            f"{self._account}.dkr.ecr.{self._region}.amazonaws.com/"
            f"personal-operator/bridge@{runtime_image_digest}"
        )
        context = self._agentcore.collect_context(
            source_commit=self._source_commit,
            account=self._account,
            region=self._region,
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            expected_role_arn=self._expected_role_arn,
            runtime_image_uri=image_uri,
        )
        if (
            not isinstance(context, RuntimeContextV3)
            or context.source_commit != self._source_commit
            or context.account != self._account
            or context.region != self._region
            or context.runtime_id != runtime_id
            or context.runtime_version != runtime_version
            or context.runtime_endpoint_name != f"release_{self._source_commit}"
            or context.runtime_image_uri != image_uri
        ):
            raise ProductionObservationError(
                "AgentCore evidence identity differs from the release"
            )
        return context

    def endpoint_evidence(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> dict[str, object]:
        """Prove the never-retargeted endpoint through RuntimeContextV3."""

        context = self._runtime_context(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_digest=runtime_image_digest,
        )
        return {"runtime_context": context.to_mapping()}

    def context_evidence(
        self,
        *,
        runtime_id: str,
        runtime_version: str,
        runtime_image_digest: str,
    ) -> dict[str, object]:
        """Bind the canonical context artifact digest to fresh live evidence."""

        context = self._runtime_context(
            runtime_id=runtime_id,
            runtime_version=runtime_version,
            runtime_image_digest=runtime_image_digest,
        )
        return {
            "runtime_context": context.to_mapping(),
            "runtime_context_sha256": hashlib.sha256(context.to_bytes()).hexdigest(),
        }


def compose_production_evidence(
    *,
    ecr_client: EcrClient,
    artifact_blob_reader: ArtifactBlobReader,
    agentcore_client: AgentCoreClient,
    source_commit: str,
    source_tree: str,
    account: str,
    region: str,
    build_context: str,
    builder_id: str,
    builder_inputs: Sequence[str],
    expected_role_arn: str,
) -> ProductionEvidenceComposer:
    """Wire the exact adapters from injected, already-authorized clients."""

    return ProductionEvidenceComposer(
        ecr=EcrEvidenceAdapter(
            ecr_client,
            blob_reader=artifact_blob_reader,
        ),
        agentcore=AgentCoreEvidenceAdapter(agentcore_client),
        source_commit=source_commit,
        source_tree=source_tree,
        account=account,
        region=region,
        build_context=build_context,
        builder_id=builder_id,
        builder_inputs=builder_inputs,
        expected_role_arn=expected_role_arn,
    )
