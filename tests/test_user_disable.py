"""Operator disable switch: USER#<id>/PROFILE.accessStatus = DISABLED.

Set by scripts/manage-allowlist.sh disable. Users without the attribute are
unaffected, so existing deployments keep their behaviour until an operator
disables someone.
"""

import sys
from pathlib import Path
from unittest.mock import patch

# The router imports its sibling modules (documents, whatsapp, ...) by name.
for _path in (Path(__file__).resolve().parents[1] / "lambda/router",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.test_runtime_enrollment import add_v2_bind_target, canonical, router
from tests.test_runtime_routing import FakeRuntime, load_lambda


def _disable(module, user_id):
    module.identity_table.items[(f"USER#{user_id}", "PROFILE")]["accessStatus"] = "DISABLED"


def _enable(module, user_id):
    module.identity_table.items[(f"USER#{user_id}", "PROFILE")].pop("accessStatus")


def test_users_without_the_attribute_are_unaffected():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    assert module.resolve_user("telegram", "123") == (user_id, False)
    assert module.create_bind_code(user_id)


def test_disabled_user_is_rejected_on_every_linked_channel_and_restored_on_enable():
    module = router()
    user_id = add_v2_bind_target(module)
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (user_id, True)
    _disable(module, user_id)
    assert module.resolve_user("telegram", "owner") == (None, False)
    assert module.resolve_user("slack", "456") == (None, False)
    _enable(module, user_id)
    assert module.resolve_user("telegram", "owner") == (user_id, False)
    assert module.resolve_user("slack", "456") == (user_id, False)


def test_disabled_user_cannot_issue_or_redeem_bind_codes():
    module = router()
    user_id = add_v2_bind_target(module)
    _disable(module, user_id)
    assert module.create_bind_code(user_id) is None
    assert module.redeem_bind_code("LINKCODE", "slack", "456") == (None, False)
    assert ("CHANNEL#slack:456", "PROFILE") not in module.identity_table.items


def test_disabling_does_not_affect_other_users():
    module = router()
    first, _ = module.resolve_user("telegram", "1")
    second, _ = module.resolve_user("telegram", "2")
    _disable(module, first)
    assert module.resolve_user("telegram", "2") == (second, False)
    assert second == canonical("telegram", "2")


def _cron_for(module, user_id):
    module.identity_table.items[(f"USER#{user_id}", "CRON#schedule123")] = {
        "PK": f"USER#{user_id}", "SK": "CRON#schedule123",
    }
    cron = load_lambda("cron")
    cron.identity_table = module.identity_table
    cron.agentcore_client = FakeRuntime([{"status": "ready", "phase": "USER_READY"}, {"response": "done"}])
    event = {"userId": user_id, "actorId": "telegram:123", "channel": "telegram",
             "channelTarget": "123", "message": "hi", "scheduleId": "schedule123"}
    return cron, event


def test_scheduled_task_of_disabled_user_does_not_run_or_deliver():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    cron, event = _cron_for(module, user_id)
    _disable(module, user_id)
    with patch.object(cron, "deliver_response") as deliver:
        assert cron.handler(event, None) == {"statusCode": 403, "body": "User is disabled"}
    deliver.assert_not_called()
    assert cron.agentcore_client.calls == []


def test_async_cron_phase_stops_for_a_disabled_user():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    cron, event = _cron_for(module, user_id)
    _disable(module, user_id)
    with patch.object(cron, "deliver_response") as deliver:
        state = cron.run_async_phase("poll", {**event, "cronPhase": "poll", "jobId": "j1"}, None)
    assert state["next"] == "done"
    deliver.assert_not_called()


def test_scheduled_task_of_enabled_user_still_runs():
    module = router()
    user_id, _ = module.resolve_user("telegram", "123")
    cron, event = _cron_for(module, user_id)
    with patch.object(cron, "deliver_response") as deliver:
        assert cron.handler(event, None)["statusCode"] == 200
    deliver.assert_called_once_with("telegram", "123", "done")
