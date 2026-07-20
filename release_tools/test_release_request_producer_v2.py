from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from release_tools.cloudformation_v2 import CloudFormationOperationV2
from release_tools.image_publication import ImagePublicationBundle
from release_tools.release_plan_v2 import AssembledReleasePlanV2
from release_tools.release_runner_v2 import RELEASE_KIND_ROUTES_V2
from release_tools.test_image_publication import _prepare
from release_tools.test_release_plan_v2 import (
    ACCOUNT,
    COMMIT,
    REGION,
    TREE,
    _write_assembly,
)


DRIVER_SHA256 = hashlib.sha256(b"accepted in-process driver").hexdigest()
EVIDENCE_RUNTIME_SHA256 = hashlib.sha256(
    b"accepted evidence runtime"
).hexdigest()


@pytest.fixture(scope="module")
def image_bundle() -> ImagePublicationBundle:
    bundle = _prepare()
    assert type(bundle) is ImagePublicationBundle
    return bundle


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return (
        _write_assembly(tmp_path / "foundation", stage="foundation"),
        _write_assembly(tmp_path / "runtime", stage="runtime"),
        _write_assembly(tmp_path / "endpoint", stage="endpoint"),
    )


def _produce(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
    **changes: object,
) -> AssembledReleasePlanV2:
    from release_tools.release_request_producer_v2 import (
        produce_release_plan_v2,
    )

    foundation, runtime, endpoint = _roots(tmp_path)
    values: dict[str, object] = {
        "source_commit": COMMIT,
        "source_tree": TREE,
        "account": ACCOUNT,
        "region": REGION,
        "driver_sha256": DRIVER_SHA256,
        "evidence_runtime_sha256": EVIDENCE_RUNTIME_SHA256,
        "foundation_assembly": foundation,
        "runtime_assembly": runtime,
        "endpoint_assembly": endpoint,
        "image_bundle": image_bundle,
    }
    values.update(changes)
    return produce_release_plan_v2(**values)  # type: ignore[arg-type]


def test_producer_builds_deterministic_full_route_plan_from_typed_inputs(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
) -> None:
    first = _produce(tmp_path / "one", image_bundle)
    second = _produce(tmp_path / "two", image_bundle)

    assert type(first) is AssembledReleasePlanV2
    assert first.plan.to_bytes() == second.plan.to_bytes()
    assert first.payloads == second.payloads
    assert (
        first.plan.source_commit,
        first.plan.source_tree,
        first.plan.account,
        first.plan.region,
        first.plan.driver_sha256,
        first.plan.evidence_runtime_sha256,
    ) == (
        COMMIT,
        TREE,
        ACCOUNT,
        REGION,
        DRIVER_SHA256,
        EVIDENCE_RUNTIME_SHA256,
    )
    assert tuple(stage.stage for stage in first.stages) == (
        "foundation",
        "runtime",
        "endpoint",
        "consumer",
    )
    assert first.stages[2].manifest_bytes == first.stages[3].manifest_bytes
    assert first.stages[2].templates == first.stages[3].templates
    assert first.stages[2].assets == first.stages[3].assets
    assert {step.kind for step in first.plan.steps} == set(
        RELEASE_KIND_ROUTES_V2
    )

    merged_assets = {
        asset.asset_id: asset
        for stage in first.stages
        for asset in stage.assets
    }
    blob_count = sum(
        effect.effect_kind == "ECR_BLOB_PUT"
        for effect in image_bundle.publication_effects(
            expected_plan_sha256=image_bundle.plan_sha256
        )
    )
    assert len(first.plan.steps) == 38 + len(merged_assets) + blob_count


def test_every_request_is_derived_and_consumed_exactly_once(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
) -> None:
    assembled = _produce(tmp_path, image_bundle)
    payloads = assembled.payload_mapping()

    assert set(payloads) == {
        step.request_artifact for step in assembled.plan.steps
    }
    assert len(payloads) == len(assembled.plan.steps)
    assert all(
        hashlib.sha256(payloads[step.request_artifact]).hexdigest()
        == step.request_sha256
        == step.expected_request_sha256
        for step in assembled.plan.steps
    )

    foundation = assembled.stages[0]
    for stack_name in (
        "OpenClawVpc",
        "OpenClawSecurity",
        "OpenClawGuardrails",
        "PersonalOperatorCapabilities",
        "OpenClawAgentCore",
        "OpenClawObservability",
    ):
        step = next(
            candidate
            for candidate in assembled.plan.steps
            if candidate.step_id == f"foundation-create-{stack_name.lower()}"
        )
        operation = CloudFormationOperationV2.from_bytes(
            payloads[step.request_artifact]
        )
        template = next(
            item for item in foundation.templates if item.stack_name == stack_name
        )
        assert operation.reviewed_template_body.encode() == template.template_bytes
        assert operation.template_asset_id == template.template_asset_id


def test_producer_rejects_a_divergent_fourth_consumer_assembly(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
) -> None:
    from release_tools.release_request_producer_v2 import (
        ReleaseRequestProducerV2Error,
    )

    divergent = _write_assembly(
        tmp_path / "divergent-consumer", stage="consumer"
    )
    with pytest.raises(
        ReleaseRequestProducerV2Error,
        match="consumer assembly must reuse the exact endpoint assembly",
    ):
        _produce(
            tmp_path / "release",
            image_bundle,
            consumer_assembly=divergent,
        )


def test_producer_accepts_only_exact_endpoint_reuse_for_compatibility_argument(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
) -> None:
    from release_tools.release_request_producer_v2 import produce_release_plan_v2

    foundation, runtime, endpoint = _roots(tmp_path)
    assembled = produce_release_plan_v2(
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        driver_sha256=DRIVER_SHA256,
        evidence_runtime_sha256=EVIDENCE_RUNTIME_SHA256,
        foundation_assembly=foundation,
        runtime_assembly=runtime,
        endpoint_assembly=endpoint,
        consumer_assembly=endpoint,
        image_bundle=image_bundle,
    )
    assert assembled.stages[2].manifest_bytes == assembled.stages[3].manifest_bytes
    assert assembled.stages[2].templates == assembled.stages[3].templates
    assert assembled.stages[2].assets == assembled.stages[3].assets


def test_producer_retains_endpoint_once_against_crossed_consumer_read(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from release_tools.release_plan_v2 import TrustedCloudAssemblyReaderV2
    from release_tools.release_request_producer_v2 import produce_release_plan_v2

    foundation, runtime, endpoint = _roots(tmp_path / "release")
    divergent = _write_assembly(
        tmp_path / "crossed-consumer",
        stage="endpoint",
        zip_asset=True,
    )
    original_read = TrustedCloudAssemblyReaderV2.read
    crossed_consumer_reads: list[Path] = []

    def crossed_read(
        root: str | Path,
        *,
        stage: str,
        account: str,
        region: str,
    ):
        selected = root
        if Path(root) == endpoint and stage == "consumer":
            crossed_consumer_reads.append(Path(root))
            selected = divergent
        return original_read(
            selected,
            stage=stage,
            account=account,
            region=region,
        )

    monkeypatch.setattr(
        TrustedCloudAssemblyReaderV2,
        "read",
        staticmethod(crossed_read),
    )
    assembled = produce_release_plan_v2(
        source_commit=COMMIT,
        source_tree=TREE,
        account=ACCOUNT,
        region=REGION,
        driver_sha256=DRIVER_SHA256,
        evidence_runtime_sha256=EVIDENCE_RUNTIME_SHA256,
        foundation_assembly=foundation,
        runtime_assembly=runtime,
        endpoint_assembly=endpoint,
        consumer_assembly=endpoint,
        image_bundle=image_bundle,
    )

    assert crossed_consumer_reads == []
    assert assembled.stages[2].manifest_bytes == assembled.stages[3].manifest_bytes
    assert assembled.stages[2].templates == assembled.stages[3].templates
    assert assembled.stages[2].assets == assembled.stages[3].assets


def test_crossed_image_identity_and_tampered_blob_fail_closed(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
) -> None:
    from release_tools.release_request_producer_v2 import (
        ReleaseRequestProducerV2Error,
    )

    crossed = image_bundle.replace(
        plan=replace(image_bundle.plan, source_commit="c" * 40)
    )
    with pytest.raises(
        ReleaseRequestProducerV2Error, match="image publication identity"
    ):
        _produce(tmp_path / "crossed", crossed)

    digest = next(iter(image_bundle.blobs))
    tampered_blobs = dict(image_bundle.blobs)
    tampered_blobs[digest] = tampered_blobs[digest] + b"tamper"
    tampered = image_bundle.replace(blobs=tampered_blobs)
    with pytest.raises(
        ReleaseRequestProducerV2Error, match="image publication bundle"
    ):
        _produce(tmp_path / "tampered", tampered)


def test_producer_has_no_caller_selected_operation_catalog(
    tmp_path: Path,
    image_bundle: ImagePublicationBundle,
) -> None:
    with pytest.raises(TypeError, match="operation_kinds"):
        _produce(
            tmp_path,
            image_bundle,
            operation_kinds=("CALLER_SELECTED",),
        )
