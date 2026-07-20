"""Deterministic production request producer for clean-account release v2.

This module has no process, SDK, network, credential, mutation, or filesystem
write authority.  It retains three reviewed CloudAssembly roots through the
strict reader, derives every request byte from those retained values and one
validated in-process image publication bundle, and delegates final closure to
the public release-plan assembler.

The Endpoint assembly is deliberately reused as the Consumer assembly.  That
is the only safe pre-cloud representation of CDK's parameterized live Runtime
and Endpoint identity: accepting a separately synthesized fourth root would
permit consumer IAM and invocation subjects to drift from the exact Endpoint
stage that created them.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from release_tools.agentcore_hardening_v2 import (
    AgentCoreHardeningOperationV1,
    RUNTIME_NAME,
)
from release_tools.asset_publication_v2 import AssetPublicationV2
from release_tools.baseline_observer_v2 import BaselineObservationRequestV1
from release_tools.cloudformation_v2 import (
    BOOTSTRAP_STACK,
    CONSUMER_STACKS,
    FOUNDATION_STACKS,
    CloudFormationMutationError,
    CloudFormationOperationV2,
    minimal_bootstrap_template_body,
)
from release_tools.contracts import canonical_json_bytes
from release_tools.image_publication import (
    ArtifactSubstitutionError,
    ImagePublicationBundle,
    ImagePublicationError,
    ImagePublicationPlanV1,
)
from release_tools.release_plan_v2 import (
    AssembledReleasePlanV2,
    CloudAssemblyAssetV2,
    CloudAssemblyStageV2,
    CloudAssemblyTemplateV2,
    PreclosedReleaseArtifactsV2,
    PreclosedRequestArtifactV2,
    PreclosedStaticRequestV2,
    ReleasePlanAssemblerV2,
    TrustedCloudAssemblyReaderV2,
)
from release_tools.stack_drift_v2 import StackDriftOperationV1


class ReleaseRequestProducerV2Error(RuntimeError):
    """The typed pre-cloud inputs do not close one exact release plan."""


_CONSUMER_PHASES: Mapping[str, tuple[str, str]] = {
    "OpenClawRouter": ("router-cron-cs", "router-cron"),
    "OpenClawCron": ("router-cron-cs", "router-cron"),
    "PersonalOperatorScheduler": ("scheduler-cs", "scheduler"),
    "PersonalOperatorWeb": ("web-cs", "web"),
}


def _planned_observed_parameters(
    template: Mapping[str, Any],
) -> list[dict[str, str]]:
    definitions = template.get("Parameters", {})
    if not isinstance(definitions, Mapping):
        raise ReleaseRequestProducerV2Error(
            "reviewed template parameter definitions are malformed"
        )
    result: list[dict[str, str]] = []
    for key in sorted(definitions):
        definition = definitions[key]
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(definition, Mapping)
        ):
            raise ReleaseRequestProducerV2Error(
                "reviewed template parameter definition is invalid"
            )
        value = definition.get("Default")
        if not isinstance(value, str):
            raise ReleaseRequestProducerV2Error(
                "reviewed template parameter lacks an exact default"
            )
        item = {"ParameterKey": key, "ParameterValue": value}
        parameter_type = definition.get("Type")
        if (
            isinstance(parameter_type, str)
            and parameter_type.startswith("AWS::SSM::Parameter::Value<")
        ):
            if (
                key != "BootstrapVersion"
                or parameter_type != "AWS::SSM::Parameter::Value<String>"
                or value != "/cdk-bootstrap/hnb659fds/version"
            ):
                raise ReleaseRequestProducerV2Error(
                    "reviewed template has an unbound SSM parameter"
                )
            item["ResolvedValue"] = "6"
        result.append(item)
    return result


def _template_parameter_sha256(template: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "parameters": _planned_observed_parameters(template),
                "template": dict(template),
            }
        )
    ).hexdigest()


def _observed_request_sha256(
    *,
    kind: str,
    stack_name: str,
    change_set_name: str,
    template: Mapping[str, Any] | None,
    capabilities: tuple[str, ...],
    tags: tuple[tuple[str, str], ...],
) -> str:
    if kind in {"BOOTSTRAP_STACK", "STACK_CREATE", "STACK_UPDATE"}:
        if template is None:
            raise ReleaseRequestProducerV2Error(
                "stack operation lacks its reviewed template"
            )
        description = template.get("Description", "")
        if not isinstance(description, str):
            raise ReleaseRequestProducerV2Error(
                "reviewed template description is invalid"
            )
        projection: Mapping[str, Any] = {
            "stackName": stack_name,
            "description": description,
            "roleArn": "",
            "timeoutInMinutes": 0,
            "capabilities": sorted(capabilities),
            "notificationArns": [],
            "tags": [
                {"Key": key, "Value": value}
                for key, value in sorted(tags)
            ],
            "rollbackConfiguration": {},
            "deploymentConfig": {},
            "disableRollback": True,
            "enableTerminationProtection": True,
            "retainExceptOnCreate": False,
        }
    elif kind == "CHANGESET_CREATE":
        projection = {
            "stackName": stack_name,
            "changeSetName": change_set_name,
            "changeSetType": "CREATE",
            "description": (
                "Personal Operator release "
                + tags[-1][1].removeprefix("release_")
                if tags
                else ""
            ),
            "roleArn": "",
            "capabilities": sorted(capabilities),
            "notificationArns": [],
            "tags": [
                {"Key": key, "Value": value}
                for key, value in sorted(tags)
            ],
            "rollbackConfiguration": {},
            "deploymentConfig": {},
            "deploymentMode": "",
            "includeNestedStacks": False,
            "onStackFailure": "DO_NOTHING",
            "importExistingResources": False,
        }
    elif kind == "CHANGESET_EXECUTE":
        projection = {
            "stackName": stack_name,
            "changeSetName": change_set_name,
            "changeSetType": "CREATE",
            "executionOnly": True,
            "roleArn": "",
        }
    else:  # pragma: no cover - all callers are fixed below
        raise ReleaseRequestProducerV2Error(
            "CloudFormation operation kind is not fixed"
        )
    return hashlib.sha256(canonical_json_bytes(projection)).hexdigest()


def _reviewed_template(
    template: CloudAssemblyTemplateV2,
) -> tuple[str, dict[str, Any]]:
    try:
        text = template.template_bytes.decode("utf-8", errors="strict")
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("template is not an object")
        canonical_json_bytes(parsed)
    except (UnicodeError, TypeError, ValueError) as error:
        raise ReleaseRequestProducerV2Error(
            "retained CloudFormation template is not strict JSON"
        ) from error
    return text, parsed


def _cloudformation_request(
    *,
    kind: str,
    stack_name: str,
    source_commit: str,
    source_tree: str,
    account: str,
    region: str,
    template: CloudAssemblyTemplateV2 | None = None,
) -> bytes:
    """Construct one internally selected operation and validate its closure."""

    tags = (
        ()
        if kind == "CHANGESET_EXECUTE"
        else (
            ("SourceCommit", source_commit),
            ("SourceTree", source_tree),
            ("TransactionId", f"release_{source_commit}"),
        )
    )
    capabilities = (
        ()
        if kind in {"BOOTSTRAP_STACK", "CHANGESET_EXECUTE"}
        else ("CAPABILITY_NAMED_IAM",)
    )
    if kind == "BOOTSTRAP_STACK":
        reviewed = minimal_bootstrap_template_body(
            account=account,
            region=region,
            source_commit=source_commit,
            source_tree=source_tree,
        )
        parsed_template = json.loads(reviewed)
        template_body = reviewed
        template_url = ""
        template_asset_id = ""
    elif kind == "CHANGESET_EXECUTE":
        reviewed = ""
        parsed_template = None
        template_body = ""
        template_url = ""
        template_asset_id = ""
    else:
        if not isinstance(template, CloudAssemblyTemplateV2):
            raise ReleaseRequestProducerV2Error(
                "fixed CloudFormation operation lacks its retained template"
            )
        reviewed, parsed_template = _reviewed_template(template)
        template_body = ""
        template_asset_id = template.template_asset_id
        template_url = (
            f"https://cdk-hnb659fds-assets-{account}-{region}.s3.{region}."
            f"amazonaws.com/{template_asset_id}.json"
        )
    change_set_name = (
        f"release-{source_commit}" if kind.startswith("CHANGESET_") else ""
    )
    expected_parameters = (
        _template_parameter_sha256(parsed_template or {})
        if kind in {"BOOTSTRAP_STACK", "STACK_CREATE", "CHANGESET_CREATE"}
        else ""
    )
    expected_observed = _observed_request_sha256(
        kind=kind,
        stack_name=stack_name,
        change_set_name=change_set_name,
        template=parsed_template,
        capabilities=capabilities,
        tags=tags,
    )
    try:
        return CloudFormationOperationV2.from_mapping(
            {
                "schema": CloudFormationOperationV2.SCHEMA,
                "kind": kind,
                "account": account,
                "region": region,
                "sourceCommit": source_commit,
                "sourceTree": source_tree,
                "stackName": stack_name,
                "changeSetName": change_set_name,
                "templateBody": template_body,
                "templateUrl": template_url,
                "reviewedTemplateBody": reviewed,
                "templateAssetId": template_asset_id,
                "templateContentSha256": (
                    hashlib.sha256(reviewed.encode("utf-8")).hexdigest()
                    if reviewed
                    else ""
                ),
                "expectedTemplateParameterSha256": expected_parameters,
                "expectedObservedRequestSha256": expected_observed,
                "parameters": [],
                "capabilities": list(capabilities),
                "tags": [
                    {"Key": key, "Value": value} for key, value in tags
                ],
            }
        ).to_bytes()
    except CloudFormationMutationError as error:
        raise ReleaseRequestProducerV2Error(
            "derived CloudFormation request is not closed"
        ) from error


def _drift_request(
    *,
    stack_name: str,
    phase: str,
    occurrence: str,
    source_commit: str,
    source_tree: str,
    account: str,
    region: str,
) -> bytes:
    return StackDriftOperationV1.from_mapping(
        {
            "schema": StackDriftOperationV1.SCHEMA,
            "account": account,
            "region": region,
            "sourceCommit": source_commit,
            "sourceTree": source_tree,
            "stackName": stack_name,
            "phase": phase,
            "occurrence": occurrence,
        }
    ).to_bytes()


def _template(
    stage: CloudAssemblyStageV2, stack_name: str
) -> CloudAssemblyTemplateV2:
    matches = tuple(
        item for item in stage.templates if item.stack_name == stack_name
    )
    if len(matches) != 1:
        raise ReleaseRequestProducerV2Error(
            "retained CloudAssembly template inventory is not exact"
        )
    return matches[0]


def _merged_assets(
    stages: Sequence[CloudAssemblyStageV2],
) -> tuple[CloudAssemblyAssetV2, ...]:
    merged: dict[str, CloudAssemblyAssetV2] = {}
    for stage in stages:
        for asset in stage.assets:
            previous = merged.get(asset.asset_id)
            if previous is not None and previous != asset:
                raise ReleaseRequestProducerV2Error(
                    "retained CDK asset identity crosses stage content"
                )
            merged[asset.asset_id] = asset
    return tuple(merged[key] for key in sorted(merged))


class ReleaseRequestProducerV2:
    """Closed in-package composition of every pre-cloud request artifact."""

    @classmethod
    def produce(
        cls,
        *,
        source_commit: str,
        source_tree: str,
        account: str,
        region: str,
        driver_sha256: str,
        evidence_runtime_sha256: str,
        foundation_assembly: str | Path,
        runtime_assembly: str | Path,
        endpoint_assembly: str | Path,
        image_bundle: ImagePublicationBundle,
        consumer_assembly: str | Path | None = None,
    ) -> AssembledReleasePlanV2:
        try:
            endpoint_root = Path(endpoint_assembly)
            if (
                consumer_assembly is not None
                and Path(consumer_assembly) != endpoint_root
            ):
                raise ReleaseRequestProducerV2Error(
                    "consumer assembly must reuse the exact endpoint assembly"
                )
        except (TypeError, ValueError) as error:
            raise ReleaseRequestProducerV2Error(
                "trusted CloudAssembly root is invalid"
            ) from error

        foundation = TrustedCloudAssemblyReaderV2.read(
            foundation_assembly,
            stage="foundation",
            account=account,
            region=region,
        )
        runtime = TrustedCloudAssemblyReaderV2.read(
            runtime_assembly,
            stage="runtime",
            account=account,
            region=region,
        )
        endpoint = TrustedCloudAssemblyReaderV2.read(
            endpoint_root,
            stage="endpoint",
            account=account,
            region=region,
        )
        consumer = ReleasePlanAssemblerV2._consumer_view(endpoint)

        if type(image_bundle) is not ImagePublicationBundle:
            raise ReleaseRequestProducerV2Error(
                "image publication bundle is not the exact typed input"
            )
        raw_image_plan = image_bundle.plan
        if (
            raw_image_plan.source_commit,
            raw_image_plan.source_tree,
            raw_image_plan.account,
            raw_image_plan.region,
        ) != (source_commit, source_tree, account, region):
            raise ReleaseRequestProducerV2Error(
                "image publication identity crosses the exact release"
            )
        try:
            image_plan = ImagePublicationPlanV1.from_bytes(
                raw_image_plan.to_bytes()
            )
            publication_sha256 = image_plan.publication_plan_sha256
            image_bundle.validate(
                expected_plan_sha256=publication_sha256
            )
            image_effects = image_bundle.publication_effects(
                expected_plan_sha256=publication_sha256
            )
        except (ArtifactSubstitutionError, ImagePublicationError) as error:
            raise ReleaseRequestProducerV2Error(
                "image publication bundle is not an exact immutable closure"
            ) from error
        if any(
            (
                effect.source_commit,
                effect.source_tree,
                effect.account,
                effect.region,
            )
            != (source_commit, source_tree, account, region)
            for effect in image_effects
        ):
            raise ReleaseRequestProducerV2Error(
                "image publication effect crosses the exact release"
            )

        stages = (foundation, runtime, endpoint, consumer)
        assets = _merged_assets(stages)
        requests: list[PreclosedRequestArtifactV2] = []
        request_ids: set[str] = set()
        request_paths: set[str] = set()

        def add(
            step_id: str,
            payload: bytes,
            *,
            private: bool = False,
            path: str | None = None,
        ) -> None:
            artifact_path = path or (
                f"build/release-requests/{step_id}."
                + ("private" if private else "json")
            )
            if step_id in request_ids or artifact_path in request_paths:
                raise ReleaseRequestProducerV2Error(
                    "derived request inventory contains a duplicate"
                )
            request_ids.add(step_id)
            request_paths.add(artifact_path)
            requests.append(
                PreclosedRequestArtifactV2(
                    step_id=step_id,
                    path=artifact_path,
                    payload=payload,
                )
            )

        add(
            "foundation-baseline",
            BaselineObservationRequestV1(
                account, region, source_commit
            ).to_bytes(),
        )
        add(
            "foundation-bootstrap-cdktoolkit",
            _cloudformation_request(
                kind="BOOTSTRAP_STACK",
                stack_name=BOOTSTRAP_STACK,
                source_commit=source_commit,
                source_tree=source_tree,
                account=account,
                region=region,
            ),
        )
        add(
            "foundation-drift-cdktoolkit",
            _drift_request(
                stack_name=BOOTSTRAP_STACK,
                phase="foundation",
                occurrence="foundation-drift-cdktoolkit",
                source_commit=source_commit,
                source_tree=source_tree,
                account=account,
                region=region,
            ),
        )

        for asset in assets:
            add(
                f"foundation-asset-{asset.asset_id}",
                AssetPublicationV2.build_artifact_bytes(
                    account=account,
                    region=region,
                    source_commit=source_commit,
                    source_tree=source_tree,
                    bucket_name=asset.bucket_name,
                    asset_id=asset.asset_id,
                    object_key=asset.object_key,
                    content_type=(
                        "application/json"
                        if asset.object_key.endswith(".json")
                        else "application/zip"
                    ),
                    payload=asset.source_bytes,
                ),
                private=True,
            )

        for stack_name in FOUNDATION_STACKS:
            slug = stack_name.lower()
            add(
                f"foundation-create-{slug}",
                _cloudformation_request(
                    kind="STACK_CREATE",
                    stack_name=stack_name,
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                    template=_template(foundation, stack_name),
                ),
            )
            add(
                f"foundation-drift-{slug}",
                _drift_request(
                    stack_name=stack_name,
                    phase="foundation",
                    occurrence=f"foundation-drift-{slug}",
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                ),
            )

        for effect in image_effects:
            add(
                f"image-{effect.effect_id}",
                effect.to_private_bytes(),
                private=True,
            )
        add(
            "image-observe",
            image_plan.to_bytes(),
            path="build/image-publication-plan.json",
        )

        for stage_name, stage in (("runtime", runtime), ("endpoint", endpoint)):
            add(
                f"{stage_name}-update-agentcore",
                _cloudformation_request(
                    kind="STACK_UPDATE",
                    stack_name="OpenClawAgentCore",
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                    template=_template(stage, "OpenClawAgentCore"),
                ),
            )
            add(
                f"{stage_name}-drift-agentcore",
                _drift_request(
                    stack_name="OpenClawAgentCore",
                    phase=stage_name,
                    occurrence=f"{stage_name}-drift-agentcore",
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                ),
            )

        add(
            "runtime-harden-agentcore",
            AgentCoreHardeningOperationV1.from_mapping(
                {
                    "schema": AgentCoreHardeningOperationV1.SCHEMA,
                    "sourceCommit": source_commit,
                    "sourceTree": source_tree,
                    "account": account,
                    "region": region,
                    "runtimeName": RUNTIME_NAME,
                    "metadataConfiguration": {"requireMMDSV2": True},
                }
            ).to_bytes(),
        )
        add(
            "context-write",
            PreclosedStaticRequestV2(
                "RUNTIME_CONTEXT_WRITE",
                source_commit,
                source_tree,
                account,
                region,
                (
                    f"release:{account}:{region}:{source_commit}:"
                    "artifact:build/runtime-context.json"
                ),
            ).to_bytes(),
        )

        for stack_name in CONSUMER_STACKS:
            create_phase, execute_phase = _CONSUMER_PHASES[stack_name]
            slug = stack_name.lower()
            add(
                f"{create_phase}-create-{slug}",
                _cloudformation_request(
                    kind="CHANGESET_CREATE",
                    stack_name=stack_name,
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                    template=_template(consumer, stack_name),
                ),
            )
            add(
                f"{execute_phase}-execute-{slug}",
                _cloudformation_request(
                    kind="CHANGESET_EXECUTE",
                    stack_name=stack_name,
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                ),
            )
            add(
                f"{execute_phase}-drift-{slug}",
                _drift_request(
                    stack_name=stack_name,
                    phase=execute_phase,
                    occurrence=f"{execute_phase}-drift-{slug}",
                    source_commit=source_commit,
                    source_tree=source_tree,
                    account=account,
                    region=region,
                ),
            )

        add(
            "verify",
            PreclosedStaticRequestV2(
                "VERIFY",
                source_commit,
                source_tree,
                account,
                region,
                f"release:{account}:{region}:{source_commit}:verify",
            ).to_bytes(),
        )

        source = PreclosedReleaseArtifactsV2(
            source_commit=source_commit,
            source_tree=source_tree,
            account=account,
            region=region,
            driver_sha256=driver_sha256,
            evidence_runtime_sha256=evidence_runtime_sha256,
            foundation_assembly=Path(foundation_assembly),
            runtime_assembly=Path(runtime_assembly),
            endpoint_assembly=endpoint_root,
            consumer_assembly=endpoint_root,
            requests=tuple(requests),
        )
        return ReleasePlanAssemblerV2.assemble(
            source,
            _retained_stages=(foundation, runtime, endpoint, consumer),
        )


def produce_release_plan_v2(
    *,
    source_commit: str,
    source_tree: str,
    account: str,
    region: str,
    driver_sha256: str,
    evidence_runtime_sha256: str,
    foundation_assembly: str | Path,
    runtime_assembly: str | Path,
    endpoint_assembly: str | Path,
    image_bundle: ImagePublicationBundle,
    consumer_assembly: str | Path | None = None,
) -> AssembledReleasePlanV2:
    """Functional entry point for the closed deterministic producer."""

    return ReleaseRequestProducerV2.produce(
        source_commit=source_commit,
        source_tree=source_tree,
        account=account,
        region=region,
        driver_sha256=driver_sha256,
        evidence_runtime_sha256=evidence_runtime_sha256,
        foundation_assembly=foundation_assembly,
        runtime_assembly=runtime_assembly,
        endpoint_assembly=endpoint_assembly,
        image_bundle=image_bundle,
        consumer_assembly=consumer_assembly,
    )
