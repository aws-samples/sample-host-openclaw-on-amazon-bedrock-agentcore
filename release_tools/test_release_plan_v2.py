from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path

import pytest

from release_tools.agentcore_hardening_v2 import AgentCoreHardeningOperationV1
from release_tools.asset_publication_v2 import AssetPublicationV2
from release_tools.baseline_observer_v2 import BaselineObservationRequestV1
from release_tools.cloudformation_v2 import (
    CloudFormationOperationV2,
    _observed_request_projection_digest,
    _template_parameter_digest,
    minimal_bootstrap_template_body,
)
from release_tools.image_publication import (
    CAPABILITY_TOOL_NAMES,
    OCI_CONFIG_MEDIA_TYPE,
    OCI_EMPTY_CONFIG_MEDIA_TYPE,
    OCI_LAYER_MEDIA_TYPES,
    OCI_MANIFEST_MEDIA_TYPE,
    PROVENANCE_ARTIFACT_TYPE,
    SBOM_ARTIFACT_TYPE,
    ImagePublicationEffectV1,
    ImagePublicationPlanV1,
)
from release_tools.release_plan_v2 import (
    AssembledReleasePlanV2,
    CloudAssemblyStageV2,
    PreclosedReleaseArtifactsV2,
    PreclosedRequestArtifactV2,
    PreclosedStaticRequestV2,
    ReleasePlanAssemblerV2,
    ReleasePlanAssemblyError,
    TrustedCloudAssemblyReaderV2,
)
from release_tools.stack_drift_v2 import StackDriftOperationV1


ACCOUNT = "123456789012"
REGION = "eu-west-1"
COMMIT = "a" * 40
TREE = "b" * 40
STACKS = (
    "OpenClawVpc",
    "OpenClawSecurity",
    "OpenClawGuardrails",
    "PersonalOperatorCapabilities",
    "OpenClawAgentCore",
    "OpenClawObservability",
    "OpenClawRouter",
    "OpenClawCron",
    "PersonalOperatorScheduler",
    "PersonalOperatorWeb",
)
DEPENDENCIES = {
    "OpenClawVpc": (),
    "OpenClawSecurity": (),
    "OpenClawGuardrails": ("OpenClawSecurity",),
    "PersonalOperatorCapabilities": ("OpenClawSecurity",),
    "OpenClawAgentCore": (
        "PersonalOperatorCapabilities",
        "OpenClawVpc",
        "OpenClawGuardrails",
        "OpenClawSecurity",
    ),
    "OpenClawObservability": ("OpenClawSecurity",),
    "OpenClawRouter": ("OpenClawSecurity", "OpenClawAgentCore"),
    "OpenClawCron": (),
    "PersonalOperatorScheduler": ("OpenClawRouter", "OpenClawSecurity"),
    "PersonalOperatorWeb": (
        "PersonalOperatorScheduler",
        "OpenClawSecurity",
        "PersonalOperatorCapabilities",
        "OpenClawRouter",
        "OpenClawAgentCore",
    ),
}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _template(stage: str, stack: str) -> bytes:
    marker_properties: dict[str, object] = {"Name": stack}
    resources: dict[str, dict[str, object]] = {
        "Marker": {
            "Type": "AWS::SSM::Parameter",
            "Properties": marker_properties,
        }
    }
    if stack == "OpenClawAgentCore" and stage in {"runtime", "endpoint", "consumer"}:
        resources["Runtime"] = {"Type": "AWS::BedrockAgentCore::Runtime"}
    if stack == "OpenClawAgentCore" and stage in {"endpoint", "consumer"}:
        resources["Endpoint"] = {
            "Type": "AWS::BedrockAgentCore::RuntimeEndpoint"
        }
    return _json_bytes(
        {
            "AWSTemplateFormatVersion": "2010-09-09",
            "Description": stack,
            "Resources": resources,
        }
    )


def _write_assembly(
    root: Path,
    *,
    stage: str,
    account: str = ACCOUNT,
    extra_assets: int = 0,
    zip_asset: bool = False,
) -> Path:
    root.mkdir(mode=0o700)
    artifacts: dict[str, object] = {}
    bucket = f"cdk-hnb659fds-assets-{account}-{REGION}"
    for stack in STACKS:
        template_file = f"{stack}.template.json"
        asset_file = f"{stack}.assets.json"
        template = _template(stage, stack)
        (root / template_file).write_bytes(template)
        asset_id = hashlib.sha256(template).hexdigest()
        destination_id = f"{account}-{REGION}-{asset_id[:8]}"
        role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-file-publishing-role-{account}-{REGION}"
        )
        asset_manifest = {
            "version": "54.0.0",
            "files": {
                asset_id: {
                    "displayName": f"{stack} Template",
                    "source": {"path": template_file, "packaging": "file"},
                    "destinations": {
                        destination_id: {
                            "bucketName": bucket,
                            "objectKey": f"{asset_id}.json",
                            "region": REGION,
                            "assumeRoleArn": role,
                        }
                    },
                }
            },
            "dockerImages": {},
        }
        if stack == "OpenClawVpc":
            for index in range(extra_assets):
                extra_payload = _json_bytes(
                    {"stage": stage, "extraAsset": index}
                )
                extra_id = hashlib.sha256(extra_payload).hexdigest()
                extra_file = f"extra-{index}.json"
                (root / extra_file).write_bytes(extra_payload)
                asset_manifest["files"][extra_id] = {
                    "displayName": f"Extra {index}",
                    "source": {"path": extra_file, "packaging": "file"},
                    "destinations": {
                        f"{account}-{REGION}-{extra_id[:8]}": {
                            "bucketName": bucket,
                            "objectKey": f"{extra_id}.json",
                            "region": REGION,
                            "assumeRoleArn": role,
                        }
                    },
                }
            if zip_asset:
                zip_source = root / "web-dist"
                zip_source.mkdir(mode=0o700)
                zip_payload = b"console.log('retained zip asset');\n"
                (zip_source / "index.js").write_bytes(zip_payload)
                zip_id = hashlib.sha256(
                    b"web-dist/index.js\x00" + zip_payload
                ).hexdigest()
                asset_manifest["files"][zip_id] = {
                    "displayName": "Trusted web distribution",
                    "source": {"path": "web-dist", "packaging": "zip"},
                    "destinations": {
                        f"{account}-{REGION}-{zip_id[:8]}": {
                            "bucketName": bucket,
                            "objectKey": f"{zip_id}.zip",
                            "region": REGION,
                            "assumeRoleArn": role,
                        }
                    },
                }
        (root / asset_file).write_bytes(_json_bytes(asset_manifest))
        asset_artifact = f"{stack}.assets"
        artifacts[asset_artifact] = {
            "type": "cdk:asset-manifest",
            "properties": {
                "file": asset_file,
                "requiresBootstrapStackVersion": 6,
                "bootstrapStackVersionSsmParameter": (
                    "/cdk-bootstrap/hnb659fds/version"
                ),
            },
        }
        stack_dependencies = [*DEPENDENCIES[stack], asset_artifact]
        deploy_role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-deploy-role-{account}-{REGION}"
        )
        cfn_role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-cfn-exec-role-{account}-{REGION}"
        )
        lookup_role = (
            "arn:${AWS::Partition}:iam::"
            f"{account}:role/cdk-hnb659fds-lookup-role-{account}-{REGION}"
        )
        artifacts[stack] = {
            "type": "aws:cloudformation:stack",
            "environment": f"aws://{account}/{REGION}",
            "properties": {
                "templateFile": template_file,
                "terminationProtection": False,
                "validateOnSynth": False,
                "assumeRoleArn": deploy_role,
                "cloudFormationExecutionRoleArn": cfn_role,
                "stackTemplateAssetObjectUrl": (
                    f"s3://{bucket}/{asset_id}.json"
                ),
                "requiresBootstrapStackVersion": 6,
                "bootstrapStackVersionSsmParameter": (
                    "/cdk-bootstrap/hnb659fds/version"
                ),
                "additionalDependencies": [asset_artifact],
                "lookupRole": {
                    "arn": lookup_role,
                    "requiresBootstrapStackVersion": 8,
                    "bootstrapStackVersionSsmParameter": (
                        "/cdk-bootstrap/hnb659fds/version"
                    ),
                },
            },
            "dependencies": stack_dependencies,
            "additionalMetadataFile": f"{stack}.metadata.json",
            "displayName": stack,
        }
    manifest = {
        "version": "54.0.0",
        "artifacts": artifacts,
        "missing": [],
        "minimumCliVersion": "2.1033.0",
    }
    (root / "manifest.json").write_bytes(_json_bytes(manifest))
    return root


def _replace_template(root: Path, stack: str, template: bytes) -> None:
    template_file = root / f"{stack}.template.json"
    asset_file = root / f"{stack}.assets.json"
    asset_manifest = json.loads(asset_file.read_bytes())
    old_id, old_asset = next(iter(asset_manifest["files"].items()))
    new_id = hashlib.sha256(template).hexdigest()
    destination_id, destination = next(
        iter(old_asset["destinations"].items())
    )
    destination["objectKey"] = f"{new_id}.json"
    old_asset["destinations"] = {
        f"{ACCOUNT}-{REGION}-{new_id[:8]}": destination
    }
    asset_manifest["files"] = {new_id: old_asset}
    asset_file.write_bytes(_json_bytes(asset_manifest))
    template_file.write_bytes(template)
    manifest_file = root / "manifest.json"
    manifest = json.loads(manifest_file.read_bytes())
    manifest["artifacts"][stack]["properties"][
        "stackTemplateAssetObjectUrl"
    ] = (
        f"s3://cdk-hnb659fds-assets-{ACCOUNT}-{REGION}/{new_id}.json"
    )
    manifest_file.write_bytes(_json_bytes(manifest))


def _descriptor(payload: bytes, media_type: str) -> dict[str, object]:
    return {
        "mediaType": media_type,
        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _image_artifacts(
    *,
    extra_layers: int = 0,
    crossed_referrer_subject_size: bool = False,
) -> tuple[ImagePublicationPlanV1, tuple[ImagePublicationEffectV1, ...]]:
    config_payload = b'{"architecture":"arm64","os":"linux"}'
    layer_payloads = [b"layer-primary"] + [
        f"layer-extra-{index}".encode() for index in range(extra_layers)
    ]
    sbom_payload = b'{"spdxVersion":"SPDX-2.3"}'
    provenance_payload = (
        b'{"_type":"https://in-toto.io/Statement/v1"}'
    )
    empty_config_payload = b"{}"
    config = _descriptor(config_payload, OCI_CONFIG_MEDIA_TYPE)
    layers = [
        _descriptor(payload, sorted(OCI_LAYER_MEDIA_TYPES)[0])
        for payload in layer_payloads
    ]
    sbom = _descriptor(sbom_payload, SBOM_ARTIFACT_TYPE)
    provenance = _descriptor(provenance_payload, PROVENANCE_ARTIFACT_TYPE)
    empty_config = _descriptor(empty_config_payload, OCI_EMPTY_CONFIG_MEDIA_TYPE)
    subject_manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "config": config,
            "layers": layers,
        }
    )
    subject = _descriptor(subject_manifest, OCI_MANIFEST_MEDIA_TYPE)
    sbom_subject = dict(subject)
    if crossed_referrer_subject_size:
        sbom_subject["size"] = int(sbom_subject["size"]) + 1
    sbom_manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": SBOM_ARTIFACT_TYPE,
            "config": empty_config,
            "layers": [sbom],
            "subject": sbom_subject,
        }
    )
    provenance_manifest = _json_bytes(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST_MEDIA_TYPE,
            "artifactType": PROVENANCE_ARTIFACT_TYPE,
            "config": empty_config,
            "layers": [provenance],
            "subject": subject,
        }
    )
    plan = ImagePublicationPlanV1.from_mapping(
        {
            "schema": ImagePublicationPlanV1.SCHEMA,
            "sourceCommit": COMMIT,
            "sourceTree": TREE,
            "account": ACCOUNT,
            "region": REGION,
            "repositoryName": "personal-operator/bridge",
            "commitTag": f"commit-{COMMIT}",
            "platform": "linux/arm64",
            "gitArchiveSha256": "1" * 64,
            "buildArchiveSha256": "2" * 64,
            "buildArchiveSize": 123,
            "catalogSourceSha256": "3" * 64,
            "capabilityCatalogDigest": "4" * 64,
            "modelCallableTools": list(CAPABILITY_TOOL_NAMES),
            "created": "2026-07-20T00:00:00Z",
            "builderId": "https://personal-operator.invalid/test-builder",
            "builderDependencies": [
                {"uri": "pkg:test/builder@1", "digest": "sha256:" + "5" * 64}
            ],
            "subject": subject,
            "config": config,
            "layers": layers,
            "sbom": {
                "payload": sbom,
                "manifest": _descriptor(
                    sbom_manifest, OCI_MANIFEST_MEDIA_TYPE
                ),
            },
            "provenance": {
                "payload": provenance,
                "manifest": _descriptor(
                    provenance_manifest, OCI_MANIFEST_MEDIA_TYPE
                ),
            },
            "probeEvidence": [
                {"buildId": "fresh-1", "sha256": "6" * 64, "size": 5},
                {"buildId": "fresh-2", "sha256": "6" * 64, "size": 5},
            ],
        }
    )
    plan_sha = plan.publication_plan_sha256

    def effect(
        kind: str,
        descriptor: dict[str, object],
        payload: bytes,
        *,
        tag: str | None = None,
        subject_digest: str | None = None,
        artifact_type: str | None = None,
    ) -> ImagePublicationEffectV1:
        prefix = {
            "ECR_BLOB_PUT": "ecr-blob-",
            "ECR_SUBJECT_MANIFEST_PUT": "ecr-subject-",
            "ECR_SBOM_REFERRER_PUT": "ecr-sbom-",
            "ECR_PROVENANCE_REFERRER_PUT": "ecr-provenance-",
        }[kind]
        return ImagePublicationEffectV1(
            plan_sha,
            prefix + str(descriptor["digest"]).removeprefix("sha256:"),
            kind,
            COMMIT,
            TREE,
            ACCOUNT,
            REGION,
            str(descriptor["digest"]),
            str(descriptor["mediaType"]),
            int(descriptor["size"]),
            tag,
            subject_digest,
            artifact_type,
            payload,
        )

    blob_pairs = [
        (config, config_payload),
        *zip(layers, layer_payloads, strict=True),
        (sbom, sbom_payload),
        (provenance, provenance_payload),
        (empty_config, empty_config_payload),
    ]
    effects = [effect("ECR_BLOB_PUT", item, payload) for item, payload in blob_pairs]
    effects.extend(
        (
            effect(
                "ECR_SUBJECT_MANIFEST_PUT",
                subject,
                subject_manifest,
                tag=f"commit-{COMMIT}",
            ),
            effect(
                "ECR_SBOM_REFERRER_PUT",
                _descriptor(sbom_manifest, OCI_MANIFEST_MEDIA_TYPE),
                sbom_manifest,
                subject_digest=str(subject["digest"]),
                artifact_type=SBOM_ARTIFACT_TYPE,
            ),
            effect(
                "ECR_PROVENANCE_REFERRER_PUT",
                _descriptor(provenance_manifest, OCI_MANIFEST_MEDIA_TYPE),
                provenance_manifest,
                subject_digest=str(subject["digest"]),
                artifact_type=PROVENANCE_ARTIFACT_TYPE,
            ),
        )
    )
    for item in effects:
        item.validate()
    return plan, tuple(effects)


def _cloudformation_request(
    kind: str,
    stack: str,
    *,
    template: bytes | None = None,
    template_asset_id: str = "",
) -> bytes:
    tags = (
        ()
        if kind == "CHANGESET_EXECUTE"
        else (
            ("SourceCommit", COMMIT),
            ("SourceTree", TREE),
            ("TransactionId", f"release_{COMMIT}"),
        )
    )
    capabilities = (
        ()
        if kind in {"BOOTSTRAP_STACK", "CHANGESET_EXECUTE"}
        else ("CAPABILITY_NAMED_IAM",)
    )
    if kind == "BOOTSTRAP_STACK":
        reviewed = minimal_bootstrap_template_body(
            account=ACCOUNT,
            region=REGION,
            source_commit=COMMIT,
            source_tree=TREE,
        )
        template_body = reviewed
        template_url = ""
        template_asset_id = ""
    elif kind == "CHANGESET_EXECUTE":
        reviewed = ""
        template_body = ""
        template_url = ""
        template_asset_id = ""
    else:
        assert template is not None
        reviewed = template.decode()
        template_body = ""
        template_url = (
            f"https://cdk-hnb659fds-assets-{ACCOUNT}-{REGION}.s3.{REGION}."
            f"amazonaws.com/{template_asset_id}.json"
        )
    parsed_template = json.loads(reviewed) if reviewed else None
    expected_parameters = (
        _template_parameter_digest(parsed_template or {}, ())
        if kind in {"BOOTSTRAP_STACK", "STACK_CREATE", "CHANGESET_CREATE"}
        else ""
    )
    expected_observed = _observed_request_projection_digest(
        kind=kind,
        stack_name=stack,
        change_set_name=(f"release-{COMMIT}" if "CHANGESET" in kind else ""),
        template=parsed_template,
        capabilities=capabilities,
        tags=tags,
    )
    value = {
        "schema": CloudFormationOperationV2.SCHEMA,
        "kind": kind,
        "account": ACCOUNT,
        "region": REGION,
        "sourceCommit": COMMIT,
        "sourceTree": TREE,
        "stackName": stack,
        "changeSetName": f"release-{COMMIT}" if "CHANGESET" in kind else "",
        "templateBody": template_body,
        "templateUrl": template_url,
        "reviewedTemplateBody": reviewed,
        "templateAssetId": template_asset_id,
        "templateContentSha256": (
            hashlib.sha256(reviewed.encode()).hexdigest() if reviewed else ""
        ),
        "expectedTemplateParameterSha256": expected_parameters,
        "expectedObservedRequestSha256": expected_observed,
        "parameters": [],
        "capabilities": list(capabilities),
        "tags": [{"Key": key, "Value": value} for key, value in tags],
    }
    return CloudFormationOperationV2.from_mapping(value).to_bytes()


def _drift_request(stack: str, *, phase: str, occurrence: str) -> bytes:
    return StackDriftOperationV1.from_mapping(
        {
            "schema": StackDriftOperationV1.SCHEMA,
            "account": ACCOUNT,
            "region": REGION,
            "sourceCommit": COMMIT,
            "sourceTree": TREE,
            "stackName": stack,
            "phase": phase,
            "occurrence": occurrence,
        }
    ).to_bytes()


def _preclosed_source(
    tmp_path: Path,
    *,
    extra_layers: int = 0,
    extra_assets: int = 0,
    crossed_referrer_subject_size: bool = False,
    zip_asset: bool = False,
) -> PreclosedReleaseArtifactsV2:
    tmp_path.mkdir(parents=True, exist_ok=True)
    roots = {
        stage: _write_assembly(
            tmp_path / stage,
            stage=stage,
            extra_assets=extra_assets if stage == "foundation" else 0,
            zip_asset=zip_asset,
        )
        for stage in ("foundation", "runtime", "endpoint", "consumer")
    }
    stages = {
        stage: TrustedCloudAssemblyReaderV2.read(
            root, stage=stage, account=ACCOUNT, region=REGION
        )
        for stage, root in roots.items()
    }
    requests: list[PreclosedRequestArtifactV2] = []

    def add(step_id: str, payload: bytes, *, private: bool = False) -> None:
        requests.append(
            PreclosedRequestArtifactV2(
                step_id,
                f"build/release-requests/{step_id}."
                + ("private" if private else "json"),
                payload,
            )
        )

    add(
        "foundation-baseline",
        BaselineObservationRequestV1(ACCOUNT, REGION, COMMIT).to_bytes(),
    )
    add(
        "foundation-bootstrap-cdktoolkit",
        _cloudformation_request("BOOTSTRAP_STACK", "CDKToolkit"),
    )
    add(
        "foundation-drift-cdktoolkit",
        _drift_request(
            "CDKToolkit",
            phase="foundation",
            occurrence="foundation-drift-cdktoolkit",
        ),
    )
    assets = ReleasePlanAssemblerV2._merged_assets(tuple(stages.values()))
    for item in assets:
        payload = item.source_bytes or b"untrusted-caller-selected-zip"
        add(
            f"foundation-asset-{item.asset_id}",
            AssetPublicationV2.build_artifact_bytes(
                account=ACCOUNT,
                region=REGION,
                source_commit=COMMIT,
                source_tree=TREE,
                bucket_name=item.bucket_name,
                asset_id=item.asset_id,
                object_key=item.object_key,
                content_type=(
                    "application/json"
                    if item.object_key.endswith(".json")
                    else "application/zip"
                ),
                payload=payload,
            ),
            private=True,
        )
    for stack in STACKS[:6]:
        slug = stack.lower()
        template = next(
            item for item in stages["foundation"].templates if item.stack_name == stack
        )
        add(
            f"foundation-create-{slug}",
            _cloudformation_request(
                "STACK_CREATE",
                stack,
                template=template.template_bytes,
                template_asset_id=template.template_asset_id,
            ),
        )
        add(
            f"foundation-drift-{slug}",
            _drift_request(
                stack,
                phase="foundation",
                occurrence=f"foundation-drift-{slug}",
            ),
        )

    image_plan, effects = _image_artifacts(
        extra_layers=extra_layers,
        crossed_referrer_subject_size=crossed_referrer_subject_size,
    )
    for effect in effects:
        add(
            f"image-{effect.effect_id}", effect.to_private_bytes(), private=True
        )
    requests.append(
        PreclosedRequestArtifactV2(
            "image-observe",
            "build/image-publication-plan.json",
            image_plan.to_bytes(),
        )
    )
    for stage in ("runtime", "endpoint"):
        template = next(
            item
            for item in stages[stage].templates
            if item.stack_name == "OpenClawAgentCore"
        )
        add(
            f"{stage}-update-agentcore",
            _cloudformation_request(
                "STACK_UPDATE",
                "OpenClawAgentCore",
                template=template.template_bytes,
                template_asset_id=template.template_asset_id,
            ),
        )
        add(
            f"{stage}-drift-agentcore",
            _drift_request(
                "OpenClawAgentCore",
                phase=stage,
                occurrence=f"{stage}-drift-agentcore",
            ),
        )
    add(
        "runtime-harden-agentcore",
        AgentCoreHardeningOperationV1.from_mapping(
            {
                "schema": AgentCoreHardeningOperationV1.SCHEMA,
                "sourceCommit": COMMIT,
                "sourceTree": TREE,
                "account": ACCOUNT,
                "region": REGION,
                "runtimeName": "personal_operator_bridge",
                "metadataConfiguration": {"requireMMDSV2": True},
            }
        ).to_bytes(),
    )
    context_subject = (
        f"release:{ACCOUNT}:{REGION}:{COMMIT}:"
        "artifact:build/runtime-context.json"
    )
    add(
        "context-write",
        PreclosedStaticRequestV2(
            "RUNTIME_CONTEXT_WRITE",
            COMMIT,
            TREE,
            ACCOUNT,
            REGION,
            context_subject,
        ).to_bytes(),
    )
    phases = {
        "OpenClawRouter": ("router-cron-cs", "router-cron"),
        "OpenClawCron": ("router-cron-cs", "router-cron"),
        "PersonalOperatorScheduler": ("scheduler-cs", "scheduler"),
        "PersonalOperatorWeb": ("web-cs", "web"),
    }
    for stack in STACKS[6:]:
        create_phase, execute_phase = phases[stack]
        slug = stack.lower()
        template = next(
            item for item in stages["consumer"].templates if item.stack_name == stack
        )
        add(
            f"{create_phase}-create-{slug}",
            _cloudformation_request(
                "CHANGESET_CREATE",
                stack,
                template=template.template_bytes,
                template_asset_id=template.template_asset_id,
            ),
        )
        add(
            f"{execute_phase}-execute-{slug}",
            _cloudformation_request("CHANGESET_EXECUTE", stack),
        )
        add(
            f"{execute_phase}-drift-{slug}",
            _drift_request(
                stack,
                phase=execute_phase,
                occurrence=f"{execute_phase}-drift-{slug}",
            ),
        )
    add(
        "verify",
        PreclosedStaticRequestV2(
            "VERIFY",
            COMMIT,
            TREE,
            ACCOUNT,
            REGION,
            f"release:{ACCOUNT}:{REGION}:{COMMIT}:verify",
        ).to_bytes(),
    )
    return PreclosedReleaseArtifactsV2(
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        driver_sha256="7" * 64,
        evidence_runtime_sha256="8" * 64,
        foundation_assembly=roots["foundation"],
        runtime_assembly=roots["runtime"],
        endpoint_assembly=roots["endpoint"],
        consumer_assembly=roots["consumer"],
        requests=tuple(requests),
    )


def test_reader_pins_exact_stage_inventory_templates_and_assets(tmp_path: Path) -> None:
    root = _write_assembly(tmp_path / "foundation", stage="foundation")

    result = TrustedCloudAssemblyReaderV2.read(
        root, stage="foundation", account=ACCOUNT, region=REGION
    )

    assert isinstance(result, CloudAssemblyStageV2)
    assert result.stage == "foundation"
    assert tuple(item.stack_name for item in result.templates) == STACKS
    assert len(result.assets) == len(STACKS)
    assert result.template("OpenClawVpc") == _template(
        "foundation", "OpenClawVpc"
    )


@pytest.mark.parametrize("stage", ("foundation", "runtime", "endpoint", "consumer"))
def test_reader_accepts_only_the_exact_stage_semantics(
    tmp_path: Path, stage: str
) -> None:
    root = _write_assembly(tmp_path / stage, stage=stage)
    assert TrustedCloudAssemblyReaderV2.read(
        root, stage=stage, account=ACCOUNT, region=REGION
    ).stage == stage


def test_reader_rejects_stage_substitution_and_cross_account(tmp_path: Path) -> None:
    runtime = _write_assembly(tmp_path / "runtime", stage="runtime")
    with pytest.raises(ReleasePlanAssemblyError, match="stage semantics"):
        TrustedCloudAssemblyReaderV2.read(
            runtime, stage="foundation", account=ACCOUNT, region=REGION
        )
    with pytest.raises(
        ReleasePlanAssemblyError, match="account|environment|destination"
    ):
        TrustedCloudAssemblyReaderV2.read(
            runtime, stage="runtime", account="999999999999", region=REGION
        )


def test_reader_rejects_agentcore_runtime_outside_agentcore_stack(
    tmp_path: Path,
) -> None:
    root = _write_assembly(tmp_path / "runtime", stage="runtime")
    template = json.loads((root / "OpenClawVpc.template.json").read_bytes())
    template["Resources"]["EarlyRuntime"] = {
        "Type": "AWS::BedrockAgentCore::Runtime"
    }
    _replace_template(root, "OpenClawVpc", _json_bytes(template))

    with pytest.raises(ReleasePlanAssemblyError, match="stage semantics"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="runtime", account=ACCOUNT, region=REGION
        )


def test_reader_rejects_missing_reordered_dependency_or_unknown_artifact(
    tmp_path: Path,
) -> None:
    root = _write_assembly(tmp_path / "assembly", stage="foundation")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"]["OpenClawAgentCore"]["dependencies"] = [
        "OpenClawAgentCore.assets"
    ]
    manifest_path.write_bytes(_json_bytes(manifest))
    with pytest.raises(ReleasePlanAssemblyError, match="dependency topology"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="foundation", account=ACCOUNT, region=REGION
        )

    root = _write_assembly(tmp_path / "unknown", stage="foundation")
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"]["Other"] = {"type": "unknown:type"}
    manifest_path.write_bytes(_json_bytes(manifest))
    with pytest.raises(ReleasePlanAssemblyError, match="artifact type"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="foundation", account=ACCOUNT, region=REGION
        )


@pytest.mark.parametrize("attack", ("symlink", "hardlink", "fifo", "traversal"))
def test_reader_rejects_unsafe_referenced_files(
    tmp_path: Path, attack: str
) -> None:
    root = _write_assembly(tmp_path / "assembly", stage="foundation")
    target = root / "OpenClawVpc.template.json"
    if attack == "symlink":
        payload = target.read_bytes()
        target.unlink()
        outside = tmp_path / "outside.json"
        outside.write_bytes(payload)
        target.symlink_to(outside)
    elif attack == "hardlink":
        os.link(target, tmp_path / "alias.json")
    elif attack == "fifo":
        target.unlink()
        os.mkfifo(target)
    else:
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["artifacts"]["OpenClawVpc"]["properties"]["templateFile"] = (
            "../outside.json"
        )
        manifest_path.write_bytes(_json_bytes(manifest))
    with pytest.raises(ReleasePlanAssemblyError, match="unsafe|regular|link|path"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="foundation", account=ACCOUNT, region=REGION
        )


def test_reader_rejects_duplicate_json_keys_non_utf8_and_unstable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duplicate = _write_assembly(tmp_path / "duplicate", stage="foundation")
    (duplicate / "manifest.json").write_bytes(
        b'{"version":"54.0.0","version":"54.0.0","artifacts":{},'
        b'"missing":[],"minimumCliVersion":"2.1033.0"}'
    )
    with pytest.raises(ReleasePlanAssemblyError, match="duplicate"):
        TrustedCloudAssemblyReaderV2.read(
            duplicate, stage="foundation", account=ACCOUNT, region=REGION
        )

    non_utf8 = _write_assembly(tmp_path / "non-utf8", stage="foundation")
    (non_utf8 / "manifest.json").write_bytes(b"\xff")
    with pytest.raises(ReleasePlanAssemblyError, match="UTF-8"):
        TrustedCloudAssemblyReaderV2.read(
            non_utf8, stage="foundation", account=ACCOUNT, region=REGION
        )

    unstable = _write_assembly(tmp_path / "unstable", stage="foundation")
    target = unstable / "OpenClawVpc.template.json"

    def mutate(label: str, _name: str) -> None:
        if label == "after-first-read" and _name == target.name:
            target.write_bytes(target.read_bytes() + b" ")

    import release_tools.release_plan_v2 as module

    monkeypatch.setattr(module, "_stability_hook", mutate)
    with pytest.raises(ReleasePlanAssemblyError, match="unstable"):
        TrustedCloudAssemblyReaderV2.read(
            unstable, stage="foundation", account=ACCOUNT, region=REGION
        )


def test_reader_rejects_nonfinite_and_excessively_nested_json(
    tmp_path: Path,
) -> None:
    for index, token in enumerate((b"NaN", b"Infinity", b"-Infinity")):
        nonfinite = _write_assembly(
            tmp_path / f"nonfinite-{index}", stage="foundation"
        )
        (nonfinite / "manifest.json").write_bytes(
            b'{"x":' + token + b"}"
        )
        with pytest.raises(ReleasePlanAssemblyError, match="non-finite"):
            TrustedCloudAssemblyReaderV2.read(
                nonfinite,
                stage="foundation",
                account=ACCOUNT,
                region=REGION,
            )

    overflow = _write_assembly(
        tmp_path / "overflow", stage="foundation"
    )
    (overflow / "manifest.json").write_bytes(b'{"x":1e309}')
    with pytest.raises(ReleasePlanAssemblyError, match="non-finite"):
        TrustedCloudAssemblyReaderV2.read(
            overflow, stage="foundation", account=ACCOUNT, region=REGION
        )

    nested = _write_assembly(tmp_path / "nested", stage="foundation")
    (nested / "manifest.json").write_bytes(
        b'{"nested":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}"
    )
    with pytest.raises(ReleasePlanAssemblyError, match="nesting|valid JSON"):
        TrustedCloudAssemblyReaderV2.read(
            nested, stage="foundation", account=ACCOUNT, region=REGION
        )


def test_reader_rejects_symlink_root_and_oversize_manifest(tmp_path: Path) -> None:
    real = _write_assembly(tmp_path / "real", stage="foundation")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ReleasePlanAssemblyError, match="safe directory"):
        TrustedCloudAssemblyReaderV2.read(
            alias, stage="foundation", account=ACCOUNT, region=REGION
        )

    oversized = _write_assembly(tmp_path / "oversized", stage="foundation")
    with (oversized / "manifest.json").open("ab") as target:
        target.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ReleasePlanAssemblyError, match="size|regular"):
        TrustedCloudAssemblyReaderV2.read(
            oversized, stage="foundation", account=ACCOUNT, region=REGION
        )


def test_reader_retains_deterministic_zip_asset_bytes(tmp_path: Path) -> None:
    root = _write_assembly(
        tmp_path / "assembly", stage="foundation", zip_asset=True
    )

    first = TrustedCloudAssemblyReaderV2.read(
        root, stage="foundation", account=ACCOUNT, region=REGION
    )
    second = TrustedCloudAssemblyReaderV2.read(
        root, stage="foundation", account=ACCOUNT, region=REGION
    )
    retained = next(item for item in first.assets if item.packaging == "zip")
    repeated = next(item for item in second.assets if item.packaging == "zip")

    assert retained.source_bytes is not None
    assert retained.source_bytes.startswith(b"PK")
    assert retained.source_bytes == repeated.source_bytes


def test_reader_rejects_zip_child_mutated_while_later_sibling_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_assembly(
        tmp_path / "assembly", stage="foundation", zip_asset=True
    )
    source = root / "web-dist"
    earlier = source / "index.js"
    later = source / "z-later.txt"
    later.write_bytes(b"later sibling\n")
    replacement = b"x" * len(earlier.read_bytes())
    mutated = False

    def mutate(label: str, name: str) -> None:
        nonlocal mutated
        if not mutated and label == "after-first-read" and name == later.name:
            earlier.write_bytes(replacement)
            mutated = True

    import release_tools.release_plan_v2 as module

    monkeypatch.setattr(module, "_stability_hook", mutate)
    with pytest.raises(ReleasePlanAssemblyError, match="unstable"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="foundation", account=ACCOUNT, region=REGION
        )


def test_reader_bounds_directory_only_zip_fanout_before_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write_assembly(
        tmp_path / "assembly", stage="foundation", zip_asset=True
    )
    source = root / "web-dist"
    (source / "index.js").unlink()
    for index in range(4):
        (source / f"directory-{index}").mkdir(mode=0o700)

    import release_tools.release_plan_v2 as module

    monkeypatch.setattr(module, "_MAX_ZIP_ASSET_ENTRIES", 4)
    with pytest.raises(ReleasePlanAssemblyError, match="retained limit"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="foundation", account=ACCOUNT, region=REGION
        )


@pytest.mark.parametrize("attack", ("missing", "symlink"))
def test_reader_rejects_unretained_zip_source(
    tmp_path: Path, attack: str
) -> None:
    root = _write_assembly(
        tmp_path / "assembly", stage="foundation", zip_asset=True
    )
    source = root / "web-dist"
    outside = tmp_path / "outside"
    source.rename(outside)
    if attack == "symlink":
        source.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ReleasePlanAssemblyError, match="ZIP|zip|directory|source"):
        TrustedCloudAssemblyReaderV2.read(
            root, stage="foundation", account=ACCOUNT, region=REGION
        )


def test_assembler_closes_exact_variable_recipe_and_is_deterministic(
    tmp_path: Path,
) -> None:
    source = _preclosed_source(tmp_path)

    first = ReleasePlanAssemblerV2.assemble(source)
    reversed_source = PreclosedReleaseArtifactsV2(
        source_commit=source.source_commit,
        source_tree=source.source_tree,
        account=source.account,
        region=source.region,
        driver_sha256=source.driver_sha256,
        evidence_runtime_sha256=source.evidence_runtime_sha256,
        foundation_assembly=source.foundation_assembly,
        runtime_assembly=source.runtime_assembly,
        endpoint_assembly=source.endpoint_assembly,
        consumer_assembly=source.consumer_assembly,
        requests=tuple(reversed(source.requests)),
    )
    second = ReleasePlanAssemblerV2.assemble(reversed_source)

    assert isinstance(first, AssembledReleasePlanV2)
    asset_count = sum(step.kind == "ASSET_PUBLISH" for step in first.plan.steps)
    blob_count = sum(
        step.kind == "IMAGE_PUBLISH" and ":blob:sha256:" in step.subject
        for step in first.plan.steps
    )
    assert len(first.plan.steps) == 38 + asset_count + blob_count
    assert first.plan.to_bytes() == second.plan.to_bytes()
    assert first.payloads == second.payloads
    assert [step.phase for step in first.plan.steps] == sorted(
        [step.phase for step in first.plan.steps],
        key=(
            "foundation",
            "image",
            "runtime",
            "endpoint",
            "context",
            "router-cron-cs",
            "router-cron",
            "scheduler-cs",
            "scheduler",
            "web-cs",
            "web",
            "verify",
        ).index,
    )
    assert all(
        len(first.payload(artifact.path)) == artifact.size
        and hashlib.sha256(first.payload(artifact.path)).hexdigest()
        == artifact.sha256
        for artifact in first.plan.artifacts
    )
    with pytest.raises(TypeError):
        first.payload_mapping()["new"] = b"forbidden"  # type: ignore[index]


def test_static_context_and_verify_requests_are_canonical_and_acyclic() -> None:
    for kind, suffix in (
        ("RUNTIME_CONTEXT_WRITE", "artifact:build/runtime-context.json"),
        ("VERIFY", "verify"),
    ):
        request = PreclosedStaticRequestV2(
            kind,
            COMMIT,
            TREE,
            ACCOUNT,
            REGION,
            f"release:{ACCOUNT}:{REGION}:{COMMIT}:{suffix}",
        )
        payload = request.to_bytes()
        assert b"planSha256" not in payload
        assert b"completedPrefixSha256" not in payload
        assert request.digest() == hashlib.sha256(payload).hexdigest()
        assert PreclosedStaticRequestV2.from_bytes(payload) == request


def test_assembler_uses_real_blob_count_instead_of_hardcoding_41(
    tmp_path: Path,
) -> None:
    normal = ReleasePlanAssemblerV2.assemble(_preclosed_source(tmp_path / "one"))
    expanded = ReleasePlanAssemblerV2.assemble(
        _preclosed_source(tmp_path / "many", extra_layers=2)
    )

    normal_blobs = sum(":blob:sha256:" in step.subject for step in normal.plan.steps)
    expanded_blobs = sum(
        ":blob:sha256:" in step.subject for step in expanded.plan.steps
    )
    assert expanded_blobs == normal_blobs + 2
    assert len(expanded.plan.steps) == len(normal.plan.steps) + 2
    assert len(normal.plan.steps) != 41


def test_assembler_uses_real_unique_asset_count(tmp_path: Path) -> None:
    normal = ReleasePlanAssemblerV2.assemble(_preclosed_source(tmp_path / "one"))
    expanded = ReleasePlanAssemblerV2.assemble(
        _preclosed_source(tmp_path / "many", extra_assets=2)
    )

    normal_assets = sum(step.kind == "ASSET_PUBLISH" for step in normal.plan.steps)
    expanded_assets = sum(
        step.kind == "ASSET_PUBLISH" for step in expanded.plan.steps
    )
    assert expanded_assets == normal_assets + 2
    assert len(expanded.plan.steps) == len(normal.plan.steps) + 2


def test_assembler_rejects_plan_over_canonical_contract_byte_limit(
    tmp_path: Path,
) -> None:
    source = _preclosed_source(tmp_path, extra_assets=5_000)

    with pytest.raises(ReleasePlanAssemblyError, match="byte limit"):
        ReleasePlanAssemblerV2.assemble(source)


def test_assembler_rejects_omitted_step_and_orphan_artifact(tmp_path: Path) -> None:
    source = _preclosed_source(tmp_path / "missing")
    missing = PreclosedReleaseArtifactsV2(
        source_commit=source.source_commit,
        source_tree=source.source_tree,
        account=source.account,
        region=source.region,
        driver_sha256=source.driver_sha256,
        evidence_runtime_sha256=source.evidence_runtime_sha256,
        foundation_assembly=source.foundation_assembly,
        runtime_assembly=source.runtime_assembly,
        endpoint_assembly=source.endpoint_assembly,
        consumer_assembly=source.consumer_assembly,
        requests=tuple(
            item for item in source.requests if item.step_id != "verify"
        ),
    )
    with pytest.raises(ReleasePlanAssemblyError, match="missing.*verify"):
        ReleasePlanAssemblerV2.assemble(missing)

    source = _preclosed_source(tmp_path / "orphan")
    orphan = PreclosedReleaseArtifactsV2(
        source_commit=source.source_commit,
        source_tree=source.source_tree,
        account=source.account,
        region=source.region,
        driver_sha256=source.driver_sha256,
        evidence_runtime_sha256=source.evidence_runtime_sha256,
        foundation_assembly=source.foundation_assembly,
        runtime_assembly=source.runtime_assembly,
        endpoint_assembly=source.endpoint_assembly,
        consumer_assembly=source.consumer_assembly,
        requests=(
            *source.requests,
            PreclosedRequestArtifactV2(
                "unowned-request",
                "build/release-requests/unowned-request.json",
                b"{}",
            ),
        ),
    )
    with pytest.raises(ReleasePlanAssemblyError, match="orphan"):
        ReleasePlanAssemblerV2.assemble(orphan)


def test_assembler_wraps_nested_typed_request_recursion(tmp_path: Path) -> None:
    source = _preclosed_source(tmp_path)
    nested = b'{"nested":' + b"[" * 2_000 + b"0" + b"]" * 2_000 + b"}\n"
    requests = tuple(
        PreclosedRequestArtifactV2(item.step_id, item.path, nested)
        if item.step_id == "runtime-update-agentcore"
        else item
        for item in source.requests
    )

    with pytest.raises(
        ReleasePlanAssemblyError, match="CloudFormation request artifact"
    ):
        ReleasePlanAssemblerV2.assemble(replace(source, requests=requests))


def test_assembler_rejects_stage_template_request_substitution(tmp_path: Path) -> None:
    source = _preclosed_source(tmp_path)
    foundation = TrustedCloudAssemblyReaderV2.read(
        source.foundation_assembly,
        stage="foundation",
        account=ACCOUNT,
        region=REGION,
    )
    wrong_template = next(
        item for item in foundation.templates if item.stack_name == "OpenClawSecurity"
    )
    crossed = _cloudformation_request(
        "STACK_CREATE",
        "OpenClawVpc",
        template=wrong_template.template_bytes,
        template_asset_id=wrong_template.template_asset_id,
    )
    requests = tuple(
        PreclosedRequestArtifactV2(item.step_id, item.path, crossed)
        if item.step_id == "foundation-create-openclawvpc"
        else item
        for item in source.requests
    )
    hostile = PreclosedReleaseArtifactsV2(
        source_commit=source.source_commit,
        source_tree=source.source_tree,
        account=source.account,
        region=source.region,
        driver_sha256=source.driver_sha256,
        evidence_runtime_sha256=source.evidence_runtime_sha256,
        foundation_assembly=source.foundation_assembly,
        runtime_assembly=source.runtime_assembly,
        endpoint_assembly=source.endpoint_assembly,
        consumer_assembly=source.consumer_assembly,
        requests=requests,
    )
    with pytest.raises(ReleasePlanAssemblyError, match="template differs"):
        ReleasePlanAssemblerV2.assemble(hostile)


def test_assembler_rejects_runtime_endpoint_drift_substitution(
    tmp_path: Path,
) -> None:
    source = _preclosed_source(tmp_path)
    endpoint = next(
        item
        for item in source.requests
        if item.step_id == "endpoint-drift-agentcore"
    )
    requests = tuple(
        PreclosedRequestArtifactV2(item.step_id, item.path, endpoint.payload)
        if item.step_id == "runtime-drift-agentcore"
        else item
        for item in source.requests
    )

    with pytest.raises(
        ReleasePlanAssemblyError, match="stack drift request crosses"
    ):
        ReleasePlanAssemblerV2.assemble(replace(source, requests=requests))


def test_assembler_rejects_consumer_template_drift(tmp_path: Path) -> None:
    source = _preclosed_source(tmp_path)
    template = json.loads(
        (source.consumer_assembly / "OpenClawRouter.template.json").read_bytes()
    )
    template["Description"] = "crossed consumer template"
    _replace_template(
        source.consumer_assembly,
        "OpenClawRouter",
        _json_bytes(template),
    )

    with pytest.raises(
        ReleasePlanAssemblyError, match="consumer stage semantics"
    ):
        ReleasePlanAssemblerV2.assemble(source)


def test_assembler_accepts_one_parameterized_endpoint_assembly_for_consumers(
    tmp_path: Path,
) -> None:
    """The real CDK app parameterizes live identity in one endpoint synth."""

    source = _preclosed_source(tmp_path)

    assembled = ReleasePlanAssemblerV2.assemble(
        replace(source, consumer_assembly=source.endpoint_assembly)
    )

    assert assembled.plan.steps[-1].kind == "VERIFY"


def test_assembler_rejects_duplicate_asset_id_with_cross_stage_bytes(
    tmp_path: Path,
) -> None:
    source = _preclosed_source(tmp_path)
    target = source.runtime_assembly / "OpenClawVpc.template.json"
    original = json.loads(target.read_bytes())
    original["Description"] = "crossed-but-same-synthesized-asset-id"
    target.write_bytes(_json_bytes(original))

    with pytest.raises(
        ReleasePlanAssemblyError, match="duplicate CDK asset ID|content"
    ):
        ReleasePlanAssemblerV2.assemble(source)


def test_assembler_rejects_arbitrary_zip_publication_bytes(tmp_path: Path) -> None:
    source = _preclosed_source(tmp_path, zip_asset=True)
    stage = TrustedCloudAssemblyReaderV2.read(
        source.foundation_assembly,
        stage="foundation",
        account=ACCOUNT,
        region=REGION,
    )
    asset = next(item for item in stage.assets if item.packaging == "zip")
    step_id = f"foundation-asset-{asset.asset_id}"
    crossed = AssetPublicationV2.build_artifact_bytes(
        account=ACCOUNT,
        region=REGION,
        source_commit=COMMIT,
        source_tree=TREE,
        bucket_name=asset.bucket_name,
        asset_id=asset.asset_id,
        object_key=asset.object_key,
        content_type="application/zip",
        payload=b"arbitrary-caller-selected-archive",
    )
    requests = tuple(
        PreclosedRequestArtifactV2(item.step_id, item.path, crossed)
        if item.step_id == step_id
        else item
        for item in source.requests
    )

    with pytest.raises(
        ReleasePlanAssemblyError, match="asset publication artifact differs"
    ):
        ReleasePlanAssemblerV2.assemble(replace(source, requests=requests))


def test_assembler_rejects_same_zip_asset_id_from_crossed_source_directory(
    tmp_path: Path,
) -> None:
    source = _preclosed_source(tmp_path, zip_asset=True)
    runtime = source.runtime_assembly
    crossed = runtime / "crossed-web-dist"
    crossed.mkdir(mode=0o700)
    crossed.joinpath("index.js").write_bytes(
        b"console.log('retained zip asset');\n"
    )
    manifest_path = runtime / "OpenClawVpc.assets.json"
    manifest = json.loads(manifest_path.read_bytes())
    zip_entry = next(
        item
        for item in manifest["files"].values()
        if item["source"]["packaging"] == "zip"
    )
    zip_entry["source"]["path"] = "crossed-web-dist"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(
        ReleasePlanAssemblyError, match="duplicate CDK asset ID"
    ):
        ReleasePlanAssemblerV2.assemble(source)


def test_assembler_rejects_referrer_crossing(tmp_path: Path) -> None:
    source = _preclosed_source(tmp_path)
    target = next(
        item
        for item in source.requests
        if item.step_id.startswith("image-ecr-sbom-")
    )
    plan = ImagePublicationPlanV1.from_bytes(
        next(item for item in source.requests if item.step_id == "image-observe").payload
    )
    original_id = target.step_id.removeprefix("image-")
    original = ImagePublicationEffectV1.from_private_bytes(
        target.payload,
        expected_private_file_sha256=hashlib.sha256(target.payload).hexdigest(),
        expected_effect_id=original_id,
        expected_publication_plan_sha256=plan.publication_plan_sha256,
    )
    crossed_manifest = json.loads(original.payload)
    crossed_manifest["subject"]["digest"] = "sha256:" + "f" * 64
    crossed_payload = _json_bytes(crossed_manifest)
    crossed_digest = "sha256:" + hashlib.sha256(crossed_payload).hexdigest()
    crossed = ImagePublicationEffectV1(
        original.publication_plan_sha256,
        "ecr-sbom-" + crossed_digest.removeprefix("sha256:"),
        original.effect_kind,
        original.source_commit,
        original.source_tree,
        original.account,
        original.region,
        crossed_digest,
        original.media_type,
        len(crossed_payload),
        original.tag,
        "sha256:" + "f" * 64,
        original.artifact_type,
        crossed_payload,
    )
    crossed_payload_file = crossed.to_private_bytes()
    crossed_artifact = PreclosedRequestArtifactV2(
        f"image-{crossed.effect_id}",
        f"build/release-requests/image-{crossed.effect_id}.private",
        crossed_payload_file,
    )
    requests = tuple(
        crossed_artifact if item.step_id == target.step_id else item
        for item in source.requests
    )
    hostile = PreclosedReleaseArtifactsV2(
        source_commit=source.source_commit,
        source_tree=source.source_tree,
        account=source.account,
        region=source.region,
        driver_sha256=source.driver_sha256,
        evidence_runtime_sha256=source.evidence_runtime_sha256,
        foundation_assembly=source.foundation_assembly,
        runtime_assembly=source.runtime_assembly,
        endpoint_assembly=source.endpoint_assembly,
        consumer_assembly=source.consumer_assembly,
        requests=requests,
    )
    with pytest.raises(ReleasePlanAssemblyError, match="referrer target|manifest"):
        ReleasePlanAssemblerV2.assemble(hostile)


def test_assembler_rejects_referrer_payload_subject_size_mismatch(
    tmp_path: Path,
) -> None:
    source = _preclosed_source(
        tmp_path, crossed_referrer_subject_size=True
    )

    with pytest.raises(
        ReleasePlanAssemblyError, match="referrer payload subject"
    ):
        ReleasePlanAssemblerV2.assemble(source)
