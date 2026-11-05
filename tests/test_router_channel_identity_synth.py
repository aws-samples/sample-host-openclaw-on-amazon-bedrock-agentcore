"""channel_identity_mode (cdk.json context) reaches the Router Lambda as CHANNEL_IDENTITY_MODE.

The default ("personal") adds nothing to the function, so existing deployments
synthesize the same Router environment as before.
"""

import aws_cdk as cdk
import pytest

from stacks.router_stack import channel_identity_mode
from tests.test_router_telegram_groups_synth import router_env


def test_personal_is_the_default_and_adds_no_variable(tmp_path):
    # cdk.json ships "channel_identity_mode": "personal".
    assert "CHANNEL_IDENTITY_MODE" not in router_env(tmp_path)


def test_channel_mode_reaches_the_router(tmp_path):
    assert router_env(tmp_path, channel_identity_mode="channel")["CHANNEL_IDENTITY_MODE"] == "channel"


@pytest.mark.parametrize("value, expected", [
    (None, "personal"), ("", "personal"), ("  ", "personal"), ("personal", "personal"),
    ("Channel", "channel"), (" channel ", "channel"),
])
def test_mode_values(value, expected):
    context = {} if value is None else {"channel_identity_mode": value}
    assert channel_identity_mode(cdk.App(context=context).node) == expected


@pytest.mark.parametrize("value", ["channels", "group", "on", "true", True, False, 1])
def test_other_values_fail_at_synth(value):
    with pytest.raises(ValueError, match="channel_identity_mode must be personal or channel"):
        channel_identity_mode(cdk.App(context={"channel_identity_mode": value}).node)
