"""Router API per-route throttling and optional reserved concurrency.

The whole app.py is synthesized in a child process with fixed account/region/AZs,
so no AWS calls.

    pytest tests/test_router_throttling_synth.py -v
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT, REGION = "123456789012", "us-west-2"
FUNCTIONS = {
    "OpenClawRouter": "RouterFn",
    "OpenClawCron": "CronExecutorFn",
    "OpenClawTokenMonitoring": "TokenMetricsFunction",
}


def _synth(tmp_path, **context_overrides):
    context = json.loads((ROOT / "cdk.json").read_text())["context"]
    context.update({"account": ACCOUNT, "region": REGION,
                    "availability_zones": [f"{REGION}a", f"{REGION}b"]})
    context.update(context_overrides)
    outdir = tmp_path / "cdk.out"
    env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_ENV_SUFFIX"}
    env.update(CDK_CONTEXT_JSON=json.dumps(context), CDK_OUTDIR=str(outdir))
    result = subprocess.run([sys.executable, "app.py"], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-4000:]
    return {p.name.removesuffix(".template.json"): json.loads(p.read_text())
            for p in outdir.glob("*.template.json")}


def _resources(template, cfn_type):
    return {k: v for k, v in template["Resources"].items() if v["Type"] == cfn_type}


def _function(templates, stack):
    prefix = FUNCTIONS[stack]
    fns = {k: v for k, v in _resources(templates[stack], "AWS::Lambda::Function").items()
           if k.startswith(prefix)}
    assert len(fns) == 1, sorted(fns)
    return next(iter(fns.values()))


@pytest.fixture(scope="module")
def default_templates(tmp_path_factory):
    return _synth(tmp_path_factory.mktemp("default"))


def test_unsigned_get_routes_get_their_own_lower_limit(default_templates):
    router = default_templates["OpenClawRouter"]
    (stage_id, stage), = _resources(router, "AWS::ApiGatewayV2::Stage").items()
    props = stage["Properties"]
    assert props["DefaultRouteSettings"]["ThrottlingRateLimit"] == 100
    assert props["DefaultRouteSettings"]["ThrottlingBurstLimit"] == 50
    assert set(props["RouteSettings"]) == {"GET /health", "GET /webhook/whatsapp"}
    for settings in props["RouteSettings"].values():
        assert settings["ThrottlingRateLimit"] == 10
        assert settings["ThrottlingBurstLimit"] == 20
    # Every route key named in RouteSettings must exist before the stage.
    routes = _resources(router, "AWS::ApiGatewayV2::Route")
    by_key = {r["Properties"]["RouteKey"]: logical_id for logical_id, r in routes.items()}
    for key in props["RouteSettings"]:
        assert by_key[key] in stage.get("DependsOn", []), key


def test_no_waf_on_the_http_api(default_templates):
    # API Gateway HTTP APIs cannot be associated with a WAFv2 WebACL.
    for template in default_templates.values():
        assert not _resources(template, "AWS::WAFv2::WebACL")
        assert not _resources(template, "AWS::WAFv2::WebACLAssociation")


def test_reserved_concurrency_is_off_by_default(default_templates):
    for stack in FUNCTIONS:
        assert "ReservedConcurrentExecutions" not in _function(default_templates, stack)["Properties"]


def test_reserved_concurrency_can_be_opted_in(tmp_path):
    templates = _synth(tmp_path, router_reserved_concurrency=50, cron_reserved_concurrency="5",
                       token_metrics_reserved_concurrency=3)
    got = {stack: _function(templates, stack)["Properties"].get("ReservedConcurrentExecutions")
           for stack in FUNCTIONS}
    assert got == {"OpenClawRouter": 50, "OpenClawCron": 5, "OpenClawTokenMonitoring": 3}
