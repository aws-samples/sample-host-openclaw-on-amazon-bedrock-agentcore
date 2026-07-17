"""Static contract tests for the frozen Personal Operator runtime defaults."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_cdk_product_defaults_are_frozen() -> None:
    context = _json("cdk.json")["context"]

    assert context["region"] == "eu-west-1"
    assert context["default_model_id"] == "eu.anthropic.claude-sonnet-4-6"
    assert context["session_idle_timeout"] == 1800
    assert context["session_max_lifetime"] == 28800
    assert context["workspace_sync_interval_seconds"] == 300
    assert context["user_files_ttl_days"] == 30
    assert context["registration_open"] is False
    assert context["enable_browser"] is False
    assert context["runtime_id"] == ""
    assert context["runtime_endpoint_id"] == ""
    assert context["image_version"] == "71"


def test_bridge_runtime_versions_are_frozen() -> None:
    package = _json("bridge/package.json")
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert package["engines"]["node"] == ">=24.15.0"
    assert re.findall(r"^FROM .*node:(\S+)", dockerfile, flags=re.MULTILINE) == [
        "24.15.0-slim",
        "24.15.0-slim",
    ]
    assert re.findall(
        r"^ARG OPENCLAW_VERSION=([^\s]+)", dockerfile, flags=re.MULTILINE
    ) == ["2026.7.2"]
    assert re.findall(
        r"^ARG OPENCLAW_SOURCE_COMMIT=([0-9a-f]+)", dockerfile, flags=re.MULTILINE
    ) == ["4bfaccafd62ac2ff2e70ca1decc40fb1297ab438"]
    assert "npm install -g openclaw@" not in dockerfile


def test_bridge_image_does_not_install_clawhub() -> None:
    dockerfile = (ROOT / "bridge/Dockerfile").read_text(encoding="utf-8")

    assert "clawhub" not in dockerfile.casefold()
