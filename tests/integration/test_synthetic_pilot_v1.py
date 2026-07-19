from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]

from tests.integration.synthetic_pilot_v1 import (
    REPORT_CANARIES,
    _hermetic_boundaries,
    run_synthetic_pilot,
)
from web.gmail_workspace import GmailWorkspaceService
from web.measurements import DynamoScanMeasurements
from workflows.gmail.oauth import GoogleReadonlyOAuthFlow


EXPECTED_EVENTS = {
    ("control", "invite", "succeeded"): 3,
    ("oauth", "connect", "succeeded"): 3,
    ("connector", "connect", "succeeded"): 3,
    ("scan", "scan", "succeeded"): 3,
    ("cards", "card", "succeeded"): 3,
    ("feedback", "feedback", "succeeded"): 3,
    ("workspace", "draft", "succeeded"): 3,
    ("workspace", "workspace", "succeeded"): 3,
    ("capability_gateway", "capability", "succeeded"): 3,
    ("scheduler", "schedule", "pending"): 3,
    ("compute", "compute", "disabled"): 3,
    ("portable", "export", "succeeded"): 3,
    ("portable", "import", "succeeded"): 3,
    ("portable", "import", "inert"): 3,
    ("portable", "import", "replay_denied"): 3,
    ("control", "deletion", "pending"): 3,
    ("control", "deletion", "succeeded"): 3,
}


def _event_counts(report: dict) -> dict[tuple[str, str, str], int]:
    return {
        (event["component"], event["operation"], event["outcome"]): event["count"]
        for event in report["events"]
    }


def test_three_participant_v1_journey_is_asserted_before_private_report() -> None:
    run = run_synthetic_pilot()

    assert run.participants_completed == 3
    assert run.external_call_ledger == ()
    report = json.loads(run.report_bytes)
    assert report["schema"] == "personal-operator.cohort-report.v1"
    assert report["participantCount"] == 3
    assert _event_counts(report) == EXPECTED_EVENTS
    assert run.report_bytes.endswith(b"\n")
    assert run.report_bytes == (
        json.dumps(
            report,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    assert all(canary.encode() not in run.report_bytes for canary in REPORT_CANARIES)


def test_journey_and_runner_are_byte_deterministic_and_stdout_only() -> None:
    first = run_synthetic_pilot().report_bytes
    second = run_synthetic_pilot().report_bytes
    assert first == second

    command = [sys.executable, str(ROOT / "scripts/run-synthetic-pilot.py")]
    one = subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    two = subprocess.run(command, cwd=ROOT, check=True, capture_output=True)
    assert one.stderr == two.stderr == b""
    assert one.stdout == two.stdout == first


REAL_JOURNEY_METHODS = (
    (GoogleReadonlyOAuthFlow, "start"),
    (GoogleReadonlyOAuthFlow, "complete"),
    (DynamoScanMeasurements, "feedback"),
    (GmailWorkspaceService, "get"),
    (GmailWorkspaceService, "edit_draft"),
)


@pytest.mark.parametrize(("owner", "method_name"), REAL_JOURNEY_METHODS)
def test_success_report_causally_requires_each_real_journey_method(
    monkeypatch: pytest.MonkeyPatch,
    owner: type,
    method_name: str,
) -> None:
    label = f"{owner.__name__}.{method_name}"

    def blocked(*_args, **_kwargs):
        raise AssertionError(f"causal journey method blocked: {label}")

    monkeypatch.setattr(owner, method_name, blocked)

    with pytest.raises(AssertionError, match=f"causal journey method blocked: {label}"):
        run_synthetic_pilot()


def test_each_real_journey_method_executes_once_per_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts: dict[str, int] = {}

    for owner, method_name in REAL_JOURNEY_METHODS:
        original = getattr(owner, method_name)
        label = f"{owner.__name__}.{method_name}"

        def counted(*args, _original=original, _label=label, **kwargs):
            counts[_label] = counts.get(_label, 0) + 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, method_name, counted)

    run_synthetic_pilot()

    assert counts == {
        f"{owner.__name__}.{method_name}": 3
        for owner, method_name in REAL_JOURNEY_METHODS
    }


def test_hermetic_boundary_actively_probes_and_denies_raw_socket_methods() -> None:
    methods = ["connect", "connect_ex", "sendto"]
    methods.extend(
        name
        for name in ("send", "sendall", "sendmsg", "sendfile")
        if hasattr(socket.socket, name)
    )
    labels = [f"socket.socket.{name}" for name in methods]

    with _hermetic_boundaries() as ledger:
        assert set(labels).issubset(ledger.installed)
        for method_name, label in zip(methods, labels, strict=True):
            with pytest.raises(
                AssertionError,
                match=f"external boundary reached: {label}",
            ):
                getattr(socket.socket, method_name)()

        assert ledger.calls == labels
