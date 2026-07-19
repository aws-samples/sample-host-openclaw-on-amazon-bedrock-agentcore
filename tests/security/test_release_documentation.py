from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_release_docs_are_explicitly_preproduction_and_effect_free() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    privacy = (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")

    assert "not authorization to deploy" in operations
    assert "prepare, do not deploy" in operations
    boundary = "staging deployment path implemented and locally verified; not deployed"
    assert boundary in operations
    assert boundary in evidence
    assert "never replay a write merely because a request timed out" in operations
    assert "Raw message bodies are transient" in privacy
    assert "terminal and no-resend `UNCERTAIN` records expire after 90 days" in privacy
    assert "No deployed completion SLA is claimed" in " ".join(privacy.split())
    assert "**NOT RELEASED / NOT DEPLOYABLE YET.**" in evidence
    assert "Runtime provisioning is intentionally absent" not in evidence
    assert "Runtime deployment is intentionally **not implemented**" not in operations
    assert "pre-production local\nprototype" in evidence


def test_release_docs_keep_every_external_staging_gate_explicitly_open() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")
    combined = operations + evidence

    for gate in (
        "runtime image push",
        "managed signing",
        "authoritative image scan",
        "CloudFormation change-set execution",
        "AgentCore runtime readiness",
        "consumer application",
        "moderated pilot",
    ):
        assert f"OPEN — {gate}" in combined
    assert "No cloud resource was created or changed" in evidence
    assert "No image was pushed" in evidence


def test_release_docs_count_the_shared_lambda_asset_surface_correctly() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")

    expected = "five unique handler modules across six Lambda functions"
    assert expected in operations
    assert expected in evidence
    assert "all four Lambda handlers" not in operations


def test_pause_runbook_names_missing_controls_and_only_supported_containment() -> None:
    operations = (ROOT / "docs/OPERATIONS.md").read_text(encoding="utf-8")

    assert "retained raw `poi1_...` bearer" in operations
    assert "A stored invitation digest cannot revoke an invitation" in operations
    assert "empty production connector registry" in operations
    assert "no mutable connector kill switch or OAuth pause control" in operations
    assert "reviewed redeployment" in operations
    assert "no mutable operator schedule kill switch" in operations
    assert "exact per-user `PURGE_USER`" in operations
    assert "not a cohort-wide pause" in operations

    assert "revoke every unused invitation digest" not in operations
    assert "Set the connector control-plane kill switch" not in operations
    assert "block new OAuth starts" not in operations
    assert "Apply the schedule kill switch" not in operations
    assert "disable live EventBridge schedules" not in operations
