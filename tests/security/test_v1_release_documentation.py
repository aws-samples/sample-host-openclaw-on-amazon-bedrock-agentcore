from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = {
    "po_file_list",
    "po_file_read",
    "po_file_write",
    "po_file_delete",
    "po_web_read",
    "po_schedule_list",
    "po_schedule_propose",
    "po_schedule_cancel_propose",
    "po_compute_run",
    "po_compute_status",
}


def test_readme_is_the_current_v1_capability_and_release_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized = " ".join(readme.split()).casefold()

    assert readme.startswith("# Personal Operator v1\n")
    assert set(re.findall(r"`(po_[a-z_]+)`", readme)) == EXPECTED_TOOLS
    for boundary in (
        "provider-credential-free AgentCore runtime",
        "short-lived, namespace-scoped workspace AWS session",
        "never enters model context",
        "trusted control plane",
        "scheduled turns are read-only",
        "Production compute remains disabled",
        "Connector and Browser Gateway planes remain disabled",
        "No AWS deployment evidence has been produced",
    ):
        assert boundary.casefold() in normalized
    for stale in (
        "# Personal Operator v0",
        "intended v0",
        "only four curated model tools",
        "v0 implementation is complete",
        (
            "Identity, provider credentials, approval authority, durable effect "
            "state, and every external effect stay in a trusted control plane "
            "outside the model runtime."
        ),
        "OpenClaw `2026.7.2` is built from",
    ):
        assert stale not in readme
    assert "The runtime-image recipe pins OpenClaw `2026.7.2`" in readme
    assert "bridge/                 Provider-credential-free" in readme


def test_active_agent_guidance_points_only_to_the_current_v1_boundary() -> None:
    root_guidance = {
        relative: (ROOT / relative).read_text(encoding="utf-8")
        for relative in ("AGENTS.md", "CLAUDE.md", "SECURITY.md")
    }
    for relative, document in root_guidance.items():
        assert "Personal Operator v1" in document, relative
        assert "V1-IMPLEMENTATION-EVIDENCE.md" in document, relative
        for stale in (
            "/home/ec2-user/projects/openclaw-on-agentcore",
            '|| echo "No tests yet"',
            "full profile",
            "5 ClawHub skills",
            "exec` allowed",
            "Container assumes its own role",
            "default to `True`",
        ):
            assert stale not in document, (relative, stale)

    bridge = (ROOT / "bridge/CLAUDE.md").read_text(encoding="utf-8")
    assert "Personal Operator v1 runtime boundary" in bridge
    assert set(re.findall(r"`(po_[a-z_]+)`", bridge)) == EXPECTED_TOOLS
    assert "exactly four" not in bridge
    assert "URL retrieval and search are deferred" not in bridge

    for relative in ("REVIEW.md", "IMPLEMENTATION_PLAN.md"):
        document = (ROOT / relative).read_text(encoding="utf-8")
        assert "Archived" in "\n".join(document.splitlines()[:8]), relative
        assert "not authoritative" in "\n".join(
            document.splitlines()[:12]
        ).casefold(), relative


def test_legacy_architecture_documents_are_explicitly_archived() -> None:
    for relative in ("docs/architecture.md", "docs/architecture-detailed.md"):
        document = (ROOT / relative).read_text(encoding="utf-8")
        assert document.startswith("# ")
        assert (
            "> **Archived upstream architecture — not the Personal Operator v1 "
            "source of truth.**" in "\n".join(document.splitlines()[:10])
        )
        assert "V1-IMPLEMENTATION-EVIDENCE.md" in document
        assert "README.md" in document


def test_v1_implementation_evidence_records_local_proof_and_every_open_gate() -> None:
    evidence = (ROOT / "docs/V1-IMPLEMENTATION-EVIDENCE.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(evidence.split())

    assert evidence.startswith("# Personal Operator v1 Implementation Evidence\n")
    assert "2026-07-19" in evidence
    assert "local and synthetic evidence only" in evidence
    assert "exact commit and tree are recorded after the terminal commit" in normalized
    assert "All local checks passed." in evidence
    assert set(re.findall(r"`(po_[a-z_]+)`", evidence)) == EXPECTED_TOOLS
    assert (
        "Every connector/browser adapter path enters the Task-3 admission"
        not in evidence
    )
    assert "Active connector and browser composition is disabled" in evidence
    assert "are added only from fresh candidate runs" not in evidence
    assert re.search(r"Focused security/integration: `[1-9][0-9]* passed", evidence)
    assert re.search(r"Aggregate Python: `[1-9][0-9]* passed", evidence)
    assert re.search(r"Aggregate log SHA-256: `[0-9a-f]{64}`", evidence)
    assert "Independent specification review: `ACCEPT`" in evidence
    assert "Independent security review: `ACCEPT`" in evidence
    assert "rg -l --hidden" in evidence
    assert "shasum -a 256 specs/capabilities/catalog-v1.json" in evidence
    for category in (
        "credential",
        "forbidden runtime capability",
        "dynamic MCP",
        "browser IAM",
        "networkless compute",
        "catalog parity",
        "cross-tenant",
        "target grant",
        "schedule effect",
        "import replay",
        "log content",
    ):
        assert category in evidence
    for gate in (
        "OPEN — runtime image push",
        "OPEN — managed signing",
        "OPEN — authoritative image scan",
        "OPEN — CloudFormation change-set execution",
        "OPEN — AgentCore runtime readiness",
        "OPEN — consumer application",
        "OPEN — connector/provider effects",
        "OPEN — Browser Gateway",
        "OPEN — networkless compute",
        "OPEN — moderated pilot",
    ):
        assert gate in evidence


def test_release_evidence_no_longer_describes_the_v0_four_tool_surface() -> None:
    evidence = (ROOT / "docs/RELEASE-EVIDENCE.md").read_text(encoding="utf-8")

    assert "only four curated model tools" not in evidence
    assert "Personal Operator v1" in evidence
    assert "V1-IMPLEMENTATION-EVIDENCE.md" in evidence


def test_current_boundary_docs_match_the_ten_tool_runtime_catalog() -> None:
    capability = (ROOT / "docs/CAPABILITY-BOUNDARY.md").read_text(encoding="utf-8")
    privacy = (ROOT / "docs/PRIVACY-BOUNDARY.md").read_text(encoding="utf-8")
    combined = capability + privacy

    assert "exactly ten model-visible `po_*` tools" in capability
    assert "exactly ten curated tools" in privacy
    assert set(re.findall(r"`(po_[a-z_]+)`", capability)) == EXPECTED_TOOLS
    assert set(re.findall(r"`(po_[a-z_]+)`", privacy)) == EXPECTED_TOOLS
    for stale in (
        "six v1 tools are installed",
        "At this task boundary those six entries are contracts",
        "and four curated tools",
    ):
        assert stale not in combined
