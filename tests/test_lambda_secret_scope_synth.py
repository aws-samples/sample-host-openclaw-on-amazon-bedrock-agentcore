"""Synth-time tests for the Router and Cron Lambdas' Secrets Manager access.

Both Lambdas list each secret they read as ``secret:<name>-??????``: Secrets
Manager appends '-' and six random characters to a name, so the pattern
matches exactly that secret. A ``-*`` suffix would also match a longer name
such as ``<name>-stg-XXXXXX``, which is how a suffixed deployment in the same
account names its secrets.

Neither Lambda may read the Cognito secrets, the gateway token or per-user
secrets. The whole app is synthesized; no AWS calls.

    pytest tests/test_lambda_secret_scope_synth.py -v
"""

import pytest

from tests._app_synth import (
    ACCOUNT,
    REGION,
    iam_pattern_matches,
    resolve,
    resources,
    secret_arns_by_export,
    synth_app,
)

_LAMBDA_ROLE_POLICIES = {
    "router": ("OpenClawRouter", "RouterFnServiceRoleDefaultPolicy"),
    "cron": ("OpenClawCron", "CronExecutorFnServiceRoleDefaultPolicy"),
}


def _arn(name, suffix="AbCdEf"):
    return f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{name}-{suffix}"


def _secret_patterns(templates, which):
    base_stack, prefix = _LAMBDA_ROLE_POLICIES[which]
    # A suffixed deployment names its stacks <base>-<suffix>.
    stack = next(n for n in templates if n == base_stack or n.startswith(f"{base_stack}-"))
    imports = secret_arns_by_export(templates)
    patterns = []
    for _, lid, res in resources({stack: templates[stack]}, "AWS::IAM::Policy"):
        if not lid.startswith(prefix):
            continue
        for stmt in res["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
            if not any(a.startswith("secretsmanager:") or a == "*" for a in actions):
                continue
            resources_ = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
            patterns += [resolve(r, imports) for r in resources_]
    assert patterns, f"{which} has no Secrets Manager statement"
    return patterns


@pytest.fixture(scope="module")
def base():
    return synth_app()


@pytest.fixture(scope="module")
def staging():
    return synth_app(environment_suffix="stg")


@pytest.mark.parametrize("which", sorted(_LAMBDA_ROLE_POLICIES))
def test_each_secret_is_matched_by_its_six_character_suffix(base, which):
    for pattern in _secret_patterns(base, which):
        assert pattern.startswith(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:openclaw/"), pattern
        assert pattern.endswith("-??????"), pattern
        assert "*" not in pattern, pattern


@pytest.mark.parametrize("which", sorted(_LAMBDA_ROLE_POLICIES))
def test_own_channel_secrets_are_readable(base, which):
    patterns = _secret_patterns(base, which)
    for name in ("openclaw/channels/telegram", "openclaw/channels/slack"):
        assert any(iam_pattern_matches(p, _arn(name)) for p in patterns), name


def test_router_can_read_the_webhook_secret(base):
    patterns = _secret_patterns(base, "router")
    assert any(iam_pattern_matches(p, _arn("openclaw/webhook-secret")) for p in patterns)


@pytest.mark.parametrize("which", sorted(_LAMBDA_ROLE_POLICIES))
@pytest.mark.parametrize("name", [
    "openclaw/cognito-password-secret",
    "openclaw/cognito-user/us-west-2_EXAMPLE/telegram_1",
    "openclaw/gateway-token",
    "openclaw/user/telegram_1/api-key",
])
def test_cannot_read_cognito_gateway_or_user_secrets(base, which, name):
    for pattern in _secret_patterns(base, which):
        assert not iam_pattern_matches(pattern, _arn(name)), (pattern, name)


@pytest.mark.parametrize("which", sorted(_LAMBDA_ROLE_POLICIES))
def test_base_deployment_cannot_read_a_suffixed_deployments_secrets(base, staging, which):
    staging_arns = sorted(secret_arns_by_export(staging).values())
    staging_arns += [_arn(f"openclaw/channels/{c}-stg") for c in ("whatsapp", "discord", "line")]
    assert any("-stg-" in arn for arn in staging_arns), staging_arns
    for pattern in _secret_patterns(base, which):
        for arn in staging_arns:
            assert not iam_pattern_matches(pattern, arn), (pattern, arn)


@pytest.mark.parametrize("which", sorted(_LAMBDA_ROLE_POLICIES))
def test_suffixed_deployment_reads_its_own_secrets(staging, which):
    patterns = _secret_patterns(staging, which)
    assert any(iam_pattern_matches(p, _arn("openclaw/channels/telegram-stg")) for p in patterns)
    assert not any(iam_pattern_matches(p, _arn("openclaw/channels/telegram")) for p in patterns)
