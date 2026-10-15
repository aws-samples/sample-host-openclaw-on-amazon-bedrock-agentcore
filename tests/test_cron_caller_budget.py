"""V1 cron Lambda: the cron invoke tells the contract how long the Lambda will wait.

The V1 contract reads `callerBudgetMs` (safe integer 1..900000) and replies
20 s before arrival + budget. The Lambda computes the budget at the moment of
the cron invoke, from whichever ends first: the Lambda's remaining time or
the AgentCore client's read timeout, minus a margin that leaves time to send
the fixed failure notice. Warm-up time is therefore not counted twice.
Same approach as main's lambda/cron (#147).
"""

import json
from unittest.mock import patch

from tests.cron_v1_helpers import ACTOR, READY, USER, FakeRuntime, cron_event, load_cron_lambda

BASE_KEYS = {"action", "userId", "actorId", "channel", "message"}


class FakeClock:
    """time.monotonic stand-in; tests move it forward explicitly."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class SlowWarmupRuntime(FakeRuntime):
    """Moves the clock forward by `warmup_seconds` during the warmup call."""

    def __init__(self, results, clock, warmup_seconds):
        super().__init__(results)
        self.clock = clock
        self.warmup_seconds = warmup_seconds

    def invoke_agent_runtime(self, **kwargs):
        if json.loads(kwargs["payload"])["action"] == "warmup":
            self.clock.now += self.warmup_seconds
        return super().invoke_agent_runtime(**kwargs)


def _context(remaining_ms):
    class Context:
        @staticmethod
        def get_remaining_time_in_millis():
            return remaining_ms

    return Context()


def _run(module, context, warmup_seconds=0):
    clock = FakeClock()
    module.agentcore_client = SlowWarmupRuntime([READY, {"response": "ok"}], clock, warmup_seconds)
    with patch.object(module.time, "monotonic", clock), patch.object(module, "deliver_response"):
        assert module.handler(cron_event(), context)["statusCode"] == 200
    payloads = [json.loads(call["payload"]) for call in module.agentcore_client.calls]
    assert [p["action"] for p in payloads] == ["warmup", "cron"]
    return payloads


def test_cron_invoke_sends_budget_from_time_left_at_the_invoke():
    module = load_cron_lambda()
    warmup, cron = _run(module, _context(300_000), warmup_seconds=40)
    assert set(cron) == BASE_KEYS | {"callerBudgetMs"}
    # 300 s left at handler start, 40 s spent warming up, 20 s margin.
    assert cron["callerBudgetMs"] == 240_000
    assert isinstance(cron["callerBudgetMs"], int)
    assert "callerBudgetMs" not in warmup


def test_budget_never_outlasts_the_client_read_timeout():
    module = load_cron_lambda()
    # LAMBDA_TIMEOUT_SECONDS=900 in the test env, so the read timeout is 870 s.
    assert module.AGENTCORE_READ_TIMEOUT_SECONDS == 870
    _, cron = _run(module, _context(900_000))
    assert cron["callerBudgetMs"] == 850_000


def test_budget_without_a_lambda_context_uses_the_configured_timeout():
    module = load_cron_lambda()
    _, cron = _run(module, None)
    assert cron["callerBudgetMs"] == 850_000


def test_budget_is_clamped_to_at_least_one_ms():
    module = load_cron_lambda()
    _, cron = _run(module, _context(5_000))
    assert cron["callerBudgetMs"] == 1


def test_budget_is_clamped_to_the_contract_maximum():
    module = load_cron_lambda()
    module.AGENTCORE_READ_TIMEOUT_SECONDS = 5_000
    clock = FakeClock()
    module.agentcore_client = FakeRuntime([{"response": "ok"}])
    with patch.object(module.time, "monotonic", clock):
        module.invoke_agentcore("ses_" + "s" * 40, "cron", USER, ACTOR, "telegram", "hi",
                                deadline=clock() + 5_000)
    assert json.loads(module.agentcore_client.calls[0]["payload"])["callerBudgetMs"] == 900_000


def test_non_cron_actions_carry_no_budget():
    module = load_cron_lambda()
    module.agentcore_client = FakeRuntime([READY])
    module.invoke_agentcore("ses_" + "s" * 40, "warmup", USER, ACTOR, "telegram")
    assert set(json.loads(module.agentcore_client.calls[0]["payload"])) == {
        "action", "userId", "actorId", "channel"}
