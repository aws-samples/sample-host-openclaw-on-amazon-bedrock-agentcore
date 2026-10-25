"""telegram_groups_enabled (cdk.json context) reaches the Router Lambda as TELEGRAM_GROUPS_ENABLED."""

import pytest

from tests.test_runtime_routing_synth import run_app


def router_env(tmp_path, **context):
    template = run_app(tmp_path, context_overrides=context)["OpenClawRouter.template.json"]
    function, = [
        resource for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "openclaw-router"
    ]
    return function["Properties"]["Environment"]["Variables"]


def test_telegram_groups_are_off_by_default(tmp_path):
    assert router_env(tmp_path)["TELEGRAM_GROUPS_ENABLED"] == "false"


@pytest.mark.parametrize("value, expected", [(True, "true"), ("true", "true"), ("TRUE", "true"),
                                             (False, "false"), ("no", "false")])
def test_telegram_groups_setting_reaches_the_router(tmp_path, value, expected):
    assert router_env(tmp_path, telegram_groups_enabled=value)["TELEGRAM_GROUPS_ENABLED"] == expected
