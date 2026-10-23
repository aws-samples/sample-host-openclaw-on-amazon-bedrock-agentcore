"""telegram_groups_enabled (cdk.json context) reaches the Router Lambda as TELEGRAM_GROUPS_ENABLED."""

import pytest

from tests.test_table_protection_synth import _synth_app


def router_env(tmp_path, **context):
    templates = _synth_app(tmp_path, **context)
    functions = [
        resource for template in templates.values() for resource in template.get("Resources", {}).values()
        if resource["Type"] == "AWS::Lambda::Function"
        and "TELEGRAM_TOKEN_SECRET_ID" in resource["Properties"].get("Environment", {}).get("Variables", {})
        and "REGISTRATION_OPEN" in resource["Properties"]["Environment"]["Variables"]
    ]
    assert len(functions) == 1, "expected exactly one Router Lambda"
    return functions[0]["Properties"]["Environment"]["Variables"]


def test_telegram_groups_are_off_by_default(tmp_path):
    assert router_env(tmp_path)["TELEGRAM_GROUPS_ENABLED"] == "false"


@pytest.mark.parametrize("value, expected", [(True, "true"), ("true", "true"), (False, "false"), ("no", "false")])
def test_telegram_groups_setting_reaches_the_router(tmp_path, value, expected):
    assert router_env(tmp_path, telegram_groups_enabled=value)["TELEGRAM_GROUPS_ENABLED"] == expected
