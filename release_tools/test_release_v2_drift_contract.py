"""RED-first contract tests for the mandatory release-v2 drift recipe."""

from __future__ import annotations

from copy import deepcopy
import hashlib

import pytest

from release_tools.contracts import ContractError, ReleasePlanV2
from release_tools.test_contracts import _release_plan_v2


_STACK_MUTATIONS = frozenset(
    {
        "BOOTSTRAP_STACK",
        "STACK_CREATE",
        "STACK_UPDATE",
        "CHANGESET_EXECUTE",
    }
)


def _plan_with_mandatory_drift_checks() -> dict[str, object]:
    value = deepcopy(_release_plan_v2())
    original_steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(original_steps, list)
    assert isinstance(artifacts, list)

    if any(step["kind"] == "STACK_DRIFT_CHECK" for step in original_steps):
        return value

    expanded: list[dict[str, object]] = []
    for raw_step in original_steps:
        assert isinstance(raw_step, dict)
        expanded.append(raw_step)
        if raw_step["kind"] not in _STACK_MUTATIONS:
            continue

        drift_id = f"{raw_step['id']}-drift"
        artifact_path = f"requests/{drift_id}.json"
        request_sha256 = hashlib.sha256(artifact_path.encode("utf-8")).hexdigest()
        expanded.append(
            {
                "id": drift_id,
                "phase": raw_step["phase"],
                "ordinal": -1,
                "kind": "STACK_DRIFT_CHECK",
                "subject": f"{raw_step['subject']}:drift",
                "mutation": True,
                "requestArtifact": artifact_path,
                "requestSha256": request_sha256,
                "expectedTemplateSha256": "",
                "expectedTemplateParameterSha256": "",
                "expectedRequestSha256": request_sha256,
                "expectedObservedRequestSha256": "",
                "expectedContentSha256": "",
            }
        )
        artifacts.append(
            {
                "path": artifact_path,
                "size": len(artifact_path.encode("utf-8")),
                "sha256": request_sha256,
            }
        )

    for ordinal, step in enumerate(expanded):
        step["ordinal"] = ordinal
    value["steps"] = expanded
    artifacts.sort(key=lambda artifact: artifact["path"])
    return value


def _reindex(value: dict[str, object]) -> None:
    steps = value["steps"]
    assert isinstance(steps, list)
    for ordinal, step in enumerate(steps):
        step["ordinal"] = ordinal


def _remove_step_and_artifact(
    value: dict[str, object], step: dict[str, object]
) -> None:
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    steps.remove(step)
    artifacts[:] = [
        artifact
        for artifact in artifacts
        if artifact["path"] != step["requestArtifact"]
    ]
    _reindex(value)


def _insert_distinct_drift(
    value: dict[str, object], *, index: int, source: dict[str, object], suffix: str
) -> dict[str, object]:
    steps = value["steps"]
    artifacts = value["artifacts"]
    assert isinstance(steps, list)
    assert isinstance(artifacts, list)
    candidate = deepcopy(source)
    candidate["id"] = f"{source['id']}-{suffix}"
    path = f"requests/{candidate['id']}.json"
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
    candidate["requestArtifact"] = path
    candidate["requestSha256"] = digest
    candidate["expectedRequestSha256"] = digest
    steps.insert(index, candidate)
    artifacts.append(
        {"path": path, "size": len(path.encode("utf-8")), "sha256": digest}
    )
    artifacts.sort(key=lambda artifact: artifact["path"])
    _reindex(value)
    return candidate


def test_release_plan_v2_accepts_a_drift_check_immediately_after_every_stack_write() -> None:
    plan = ReleasePlanV2.from_mapping(_plan_with_mandatory_drift_checks())

    for index, step in enumerate(plan.steps):
        if step.kind not in _STACK_MUTATIONS:
            continue
        drift = plan.steps[index + 1]
        assert drift.kind == "STACK_DRIFT_CHECK"
        assert drift.phase == step.phase
        assert drift.subject == f"{step.subject}:drift"
        assert drift.mutation is True
        assert drift.expected_request_sha256 == drift.request_sha256
        assert drift.expected_template_sha256 == ""
        assert drift.expected_template_parameter_sha256 == ""
        assert drift.expected_observed_request_sha256 == ""
        assert drift.expected_content_sha256 == ""


@pytest.mark.parametrize(
    "damage", ("missing", "orphan", "duplicate", "reordered", "cross-subject")
)
def test_release_plan_v2_rejects_nonexact_stack_drift_pairing(
    damage: str,
) -> None:
    value = _plan_with_mandatory_drift_checks()
    steps = value["steps"]
    assert isinstance(steps, list)
    first_write_index = next(
        index for index, step in enumerate(steps) if step["kind"] in _STACK_MUTATIONS
    )
    first_drift = steps[first_write_index + 1]
    assert first_drift["kind"] == "STACK_DRIFT_CHECK"

    if damage == "missing":
        _remove_step_and_artifact(value, first_drift)
    elif damage == "orphan":
        asset_index = next(
            index for index, step in enumerate(steps) if step["kind"] == "ASSET_PUBLISH"
        )
        orphan = _insert_distinct_drift(
            value,
            index=asset_index + 1,
            source=first_drift,
            suffix="orphan",
        )
        orphan["subject"] = f"{steps[asset_index]['subject']}:drift"
    elif damage == "duplicate":
        _insert_distinct_drift(
            value,
            index=first_write_index + 2,
            source=first_drift,
            suffix="duplicate",
        )
    elif damage == "reordered":
        steps[first_write_index + 1], steps[first_write_index + 2] = (
            steps[first_write_index + 2],
            steps[first_write_index + 1],
        )
        _reindex(value)
    else:
        other_write = next(
            step
            for step in steps[first_write_index + 2 :]
            if step["kind"] in _STACK_MUTATIONS
        )
        first_drift["subject"] = f"{other_write['subject']}:drift"

    with pytest.raises(ContractError, match="drift"):
        ReleasePlanV2.from_mapping(value)


@pytest.mark.parametrize(
    "field",
    (
        "expectedTemplateSha256",
        "expectedTemplateParameterSha256",
        "expectedObservedRequestSha256",
        "expectedContentSha256",
    ),
)
def test_release_plan_v2_drift_step_rejects_every_nonrequest_binding(
    field: str,
) -> None:
    value = _plan_with_mandatory_drift_checks()
    steps = value["steps"]
    assert isinstance(steps, list)
    drift = next(step for step in steps if step["kind"] == "STACK_DRIFT_CHECK")
    drift[field] = "f" * 64

    with pytest.raises(ContractError, match="binding"):
        ReleasePlanV2.from_mapping(value)


def test_release_plan_v2_drift_step_is_closed_to_stack_owning_phases() -> None:
    value = _plan_with_mandatory_drift_checks()
    steps = value["steps"]
    assert isinstance(steps, list)
    drift = next(step for step in steps if step["kind"] == "STACK_DRIFT_CHECK")
    drift["phase"] = "image"

    with pytest.raises(ContractError, match="invalid for its phase"):
        ReleasePlanV2.from_mapping(value)
