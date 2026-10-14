"""Cron timing: no Lambda async retry, and the contract knows how long the Lambda waits.

The EN and CN briefs share one session, so the CN turn queues behind EN in the
contract. The Lambda's own wait budget is the hard limit for both; the contract
needs it to stop before the Lambda does and to measure the turn from dispatch.
"""

import json
from unittest.mock import patch

from tests.test_runtime_routing import (
    ACTOR, USER, FakeRuntime, FakeTable, candidate, cron_event, cron_table, load_lambda,
)
from tests.test_runtime_routing_synth import run_app
from runtime_invocation import RELEASE_RESERVE_SECONDS


def _payloads(module):
    return [json.loads(call["payload"]) for call in module.agentcore_client.calls]


def test_cron_turn_sends_its_remaining_wait_budget():
    module = load_lambda("cron")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([{"response": "done"}])
    route = module.get_or_create_session(USER)
    with patch.object(module.time, "monotonic", return_value=1000.0):
        module.invoke_agentcore(route, "cron", USER, ACTOR, "telegram", "brief", deadline=1000.0 + 300)
    (payload,) = _payloads(module)
    # The budget is what invoke_bounded will actually wait: deadline minus the release reserve.
    assert payload["callerBudgetMs"] == (300 - RELEASE_RESERVE_SECONDS) * 1000


def test_warmup_does_not_send_a_wait_budget():
    module = load_lambda("cron")
    module.identity_table = FakeTable(candidate())
    module.agentcore_client = FakeRuntime([{"status": "ready"}])
    module.invoke_agentcore(module.get_or_create_session(USER), "warmup", USER, ACTOR, "telegram")
    assert "callerBudgetMs" not in _payloads(module)[0]


def test_handler_cron_budget_fits_inside_the_lambda_timeout():
    module = load_lambda("cron")
    module.identity_table = cron_table()
    module.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, {"response": "done"}])

    class Context:
        def get_remaining_time_in_millis(self):
            return 900_000

    with patch.object(module, "deliver_response"):
        assert module.handler(cron_event(), Context())["statusCode"] == 200
    budget = _payloads(module)[1]["callerBudgetMs"]
    # Lambda timeout > Lambda read deadline > contract budget, with margin for delivery.
    assert 0 < budget <= 900_000 - 15_000 - RELEASE_RESERVE_SECONDS * 1000


def test_cron_lambda_never_async_retries_a_brief(tmp_path):
    template = run_app(tmp_path)["OpenClawCron.template.json"]
    resources = template["Resources"]
    function_id = next(
        key for key, resource in resources.items()
        if resource["Type"] == "AWS::Lambda::Function"
        and resource["Properties"].get("FunctionName") == "openclaw-cron-executor"
    )
    configs = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::Lambda::EventInvokeConfig"]
    assert len(configs) == 1, "a timed-out brief must not be re-run by Lambda's async retry"
    assert configs[0]["MaximumRetryAttempts"] == 0
    assert configs[0]["FunctionName"] == {"Ref": function_id}
    function = resources[function_id]["Properties"]
    assert function["Timeout"] == int(function["Environment"]["Variables"]["LAMBDA_TIMEOUT_SECONDS"])
