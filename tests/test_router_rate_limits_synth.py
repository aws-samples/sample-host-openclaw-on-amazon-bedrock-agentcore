"""Synth-time tests for webhook API throttling and Lambda reserved concurrency.

The stage keeps its stage-wide throttle and adds a lower per-route throttle on
the two GET routes that answer without a channel signature (``GET /health``
and WhatsApp's ``GET /webhook/whatsapp`` subscription check). Route settings
must name route keys that exist when the stage is created, so the stage must
depend on every route.

Reserved concurrency on the Router, Cron executor and token-metrics Lambdas is
opt-in: unset or 0 keeps the shared account pool (the previous behaviour).

The whole ``app.py`` is synthesized in a child process (the jsii runtime reads
``CDK_OUTDIR``/``CDK_CONTEXT_JSON`` only at start). No AWS calls.

    pytest tests/test_router_rate_limits_synth.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ACCOUNT = "123456789012"
_REGION = "us-west-2"

_FUNCTIONS = {
    "router": ("openclaw-router", "router_reserved_concurrency"),
    "cron": ("openclaw-cron-executor", "cron_reserved_concurrency"),
    "token": ("openclaw-token-metrics", "token_metrics_reserved_concurrency"),
}
_UNSIGNED_GET_ROUTES = {"GET /health", "GET /webhook/whatsapp"}


def _run_synth(tmp_path, **context_overrides):
    context = json.loads((_ROOT / "cdk.json").read_text())["context"]
    context.update(
        {
            "account": _ACCOUNT,
            "region": _REGION,
            "availability_zones": [f"{_REGION}a", f"{_REGION}b"],
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
    templates = {}
    for path in outdir.glob("*.template.json"):
        templates[path.name.removesuffix(".template.json")] = json.loads(path.read_text())
    return result, templates


def _synth(tmp_path, **context_overrides):
    result, templates = _run_synth(tmp_path, **context_overrides)
    assert result.returncode == 0, result.stderr[-4000:]
    assert templates, "app.py synthesized no stacks"
    return templates


def _resources(templates, res_type):
    for stack, template in templates.items():
        for logical_id, res in template.get("Resources", {}).items():
            if res["Type"] == res_type:
                yield stack, logical_id, res


def _function(templates, name):
    matches = [
        res for _, _, res in _resources(templates, "AWS::Lambda::Function")
        if res["Properties"].get("FunctionName") == name
    ]
    assert len(matches) == 1, f"expected one Lambda named {name}, found {len(matches)}"
    return matches[0]


def _router_template(templates):
    for template in templates.values():
        types = {r["Type"] for r in template.get("Resources", {}).values()}
        if "AWS::ApiGatewayV2::Api" in types:
            return template
    raise AssertionError("no stack defines the HTTP API")


@pytest.fixture(scope="module")
def default_templates(tmp_path_factory):
    return _synth(tmp_path_factory.mktemp("default"))


def test_default_stage_throttle_unchanged(default_templates):
    template = _router_template(default_templates)
    [stage] = [r for r in template["Resources"].values() if r["Type"] == "AWS::ApiGatewayV2::Stage"]
    defaults = stage["Properties"]["DefaultRouteSettings"]
    assert defaults["ThrottlingRateLimit"] == 100
    assert defaults["ThrottlingBurstLimit"] == 50


def test_unsigned_get_routes_get_lower_per_route_throttle(default_templates):
    template = _router_template(default_templates)
    [stage] = [r for r in template["Resources"].values() if r["Type"] == "AWS::ApiGatewayV2::Stage"]
    route_settings = stage["Properties"]["RouteSettings"]
    assert set(route_settings) == _UNSIGNED_GET_ROUTES
    for key in _UNSIGNED_GET_ROUTES:
        assert route_settings[key]["ThrottlingRateLimit"] == 10
        assert route_settings[key]["ThrottlingBurstLimit"] == 20
    route_keys = {
        r["Properties"]["RouteKey"]
        for r in template["Resources"].values()
        if r["Type"] == "AWS::ApiGatewayV2::Route"
    }
    # Every per-route key must name a real route, or the stage deploy fails.
    assert _UNSIGNED_GET_ROUTES <= route_keys
    # Signed POST event routes stay on the stage-wide bucket.
    assert not any(k.startswith("POST ") for k in route_settings)


def test_stage_waits_for_every_route(default_templates):
    template = _router_template(default_templates)
    resources = template["Resources"]
    [stage] = [r for r in resources.values() if r["Type"] == "AWS::ApiGatewayV2::Stage"]
    routes = {lid for lid, r in resources.items() if r["Type"] == "AWS::ApiGatewayV2::Route"}
    assert routes
    assert routes <= set(stage.get("DependsOn", []))


def test_no_waf_association_on_http_api(default_templates):
    # AWS WAF cannot be associated with an API Gateway HTTP API stage.
    assert not list(_resources(default_templates, "AWS::WAFv2::WebACLAssociation"))
    assert not list(_resources(default_templates, "AWS::WAFv2::WebACL"))


@pytest.mark.parametrize("fn", sorted(_FUNCTIONS))
def test_reserved_concurrency_off_by_default(default_templates, fn):
    name, _ = _FUNCTIONS[fn]
    assert "ReservedConcurrentExecutions" not in _function(default_templates, name)["Properties"]


def test_throttles_and_reserved_concurrency_are_configurable(tmp_path):
    templates = _synth(
        tmp_path,
        router_stage_rate_limit=250,
        router_stage_burst_limit=125,
        router_get_route_rate_limit=3,
        router_get_route_burst_limit=6,
        router_reserved_concurrency=40,
        cron_reserved_concurrency="5",
        token_metrics_reserved_concurrency=2,
    )
    template = _router_template(templates)
    [stage] = [r for r in template["Resources"].values() if r["Type"] == "AWS::ApiGatewayV2::Stage"]
    assert stage["Properties"]["DefaultRouteSettings"]["ThrottlingRateLimit"] == 250
    assert stage["Properties"]["DefaultRouteSettings"]["ThrottlingBurstLimit"] == 125
    for key in _UNSIGNED_GET_ROUTES:
        assert stage["Properties"]["RouteSettings"][key]["ThrottlingRateLimit"] == 3
        assert stage["Properties"]["RouteSettings"][key]["ThrottlingBurstLimit"] == 6
    expected = {"router": 40, "cron": 5, "token": 2}
    for fn, (name, _) in _FUNCTIONS.items():
        props = _function(templates, name)["Properties"]
        assert props["ReservedConcurrentExecutions"] == expected[fn]


def test_explicit_zero_means_unreserved(tmp_path):
    templates = _synth(tmp_path, router_reserved_concurrency=0, cron_reserved_concurrency="0")
    for name in ("openclaw-router", "openclaw-cron-executor"):
        assert "ReservedConcurrentExecutions" not in _function(templates, name)["Properties"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"router_reserved_concurrency": -1},
        {"cron_reserved_concurrency": "ten"},
        {"router_stage_rate_limit": 0},
        {"router_get_route_burst_limit": True},
    ],
)
def test_invalid_limits_fail_synth(tmp_path, overrides):
    result, _ = _run_synth(tmp_path, **overrides)
    assert result.returncode != 0
    assert next(iter(overrides)) in result.stderr
