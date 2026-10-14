"""Synth-time tests for DynamoDB table protection.

Automated cleanup jobs in some accounts delete DynamoDB tables that lack an
opt-out tag, and CloudFormation does not copy its own ``aws:cloudformation:*``
tags onto DynamoDB tables. Every table the CDK app creates must therefore have
deletion protection on by default, and must carry any extra tags set through
the ``table_tags`` context key (for example ``{"auto-delete": "no"}``).

The whole ``app.py`` is synthesized (in a child process, because the jsii
runtime reads ``CDK_OUTDIR``/``CDK_CONTEXT_JSON`` only when it starts), so a
table added to any stack later is covered too. No AWS calls: account/region
and availability zones are fixed via context.

    pytest tests/test_table_protection_synth.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import aws_cdk as cdk

_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "123456789012"
_REGION = "us-west-2"


def _synth_app(tmp_path, **context_overrides):
    """Run app.py with cdk.json context plus overrides; return {stack: template}."""
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    context.update(
        {
            "account": _ACCOUNT,
            "region": _REGION,
            "availability_zones": [f"{_REGION}a", f"{_REGION}b"],
            # Include the opt-in gateway stack so its resources are scanned too.
            "enable_gateway": True,
        }
    )
    context.update(context_overrides)
    outdir = tmp_path / "cdk.out"
    env = dict(os.environ, CDK_CONTEXT_JSON=json.dumps(context), CDK_OUTDIR=str(outdir))
    result = subprocess.run(
        [sys.executable, "app.py"],
        cwd=_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    templates = {}
    for path in outdir.glob("*.template.json"):
        templates[path.name.removesuffix(".template.json")] = json.loads(path.read_text())
    assert templates, "app.py synthesized no stacks"
    return templates


def _tables(templates):
    found = {}
    for stack, template in templates.items():
        for logical_id, res in template.get("Resources", {}).items():
            if res["Type"] == "AWS::DynamoDB::Table":
                found[f"{stack}/{logical_id}"] = res
    return found


def _tag_map(res):
    return {t["Key"]: t["Value"] for t in res["Properties"].get("Tags", [])}


@pytest.fixture(scope="module")
def default_tables(tmp_path_factory):
    return _tables(_synth_app(tmp_path_factory.mktemp("default")))


@pytest.fixture(scope="module")
def tagged_templates(tmp_path_factory):
    return _synth_app(
        tmp_path_factory.mktemp("tagged"),
        table_tags={"auto-delete": "no", "owner": "team"},
    )


def test_cdk_json_table_tags_default_is_empty():
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    assert context["table_tags"] == {}


def test_identity_and_token_usage_tables_are_found(default_tables):
    stacks = sorted(key.split("/")[0] for key in default_tables)
    assert "OpenClawRouter" in stacks
    assert "OpenClawTokenMonitoring" in stacks


def test_every_table_has_deletion_protection(default_tables):
    for key, res in default_tables.items():
        assert res["Properties"].get("DeletionProtectionEnabled") is True, key


def test_every_table_is_retained(default_tables):
    for key, res in default_tables.items():
        assert res.get("DeletionPolicy") == "Retain", key
        assert res.get("UpdateReplacePolicy") == "Retain", key


def test_no_tags_when_context_is_empty(default_tables):
    for key, res in default_tables.items():
        assert "Tags" not in res["Properties"], key


def test_context_tags_on_every_table(tagged_templates):
    tables = _tables(tagged_templates)
    assert len(tables) >= 2
    for key, res in tables.items():
        tags = _tag_map(res)
        assert tags.get("auto-delete") == "no", key
        assert tags.get("owner") == "team", key


def test_context_tags_do_not_spread_to_other_resources(tagged_templates):
    for stack, template in tagged_templates.items():
        for logical_id, res in template.get("Resources", {}).items():
            if res["Type"] == "AWS::DynamoDB::Table":
                continue
            tags = res.get("Properties", {}).get("Tags")
            if isinstance(tags, list):
                keys = [t.get("Key") for t in tags if isinstance(t, dict)]
                assert "auto-delete" not in keys, f"{stack}/{logical_id}"


def test_context_tags_from_cli_json_string(tmp_path):
    # `cdk deploy -c table_tags='{"auto-delete":"no"}'` passes a string.
    tables = _tables(_synth_app(tmp_path, table_tags='{"auto-delete": "no"}'))
    assert len(tables) >= 2
    for key, res in tables.items():
        assert _tag_map(res).get("auto-delete") == "no", key


@pytest.mark.parametrize(
    "value",
    [["auto-delete", "no"], {"auto-delete": 1}, "not json", '["a"]'],
)
def test_invalid_table_tags_are_rejected(value):
    from stacks import table_tags_from_context

    app = cdk.App(context={"table_tags": value})
    with pytest.raises(ValueError):
        table_tags_from_context(app.node)


@pytest.mark.parametrize("value", [None, "", {}])
def test_unset_table_tags_mean_no_tags(value):
    from stacks import table_tags_from_context

    context = {} if value is None else {"table_tags": value}
    app = cdk.App(context=context)
    assert table_tags_from_context(app.node) == {}
