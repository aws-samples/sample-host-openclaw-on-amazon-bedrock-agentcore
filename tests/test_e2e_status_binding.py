"""The live E2E status helper accepts the binding fields the bridge status returns.

bridge/agentcore-contract.js returns only userId and actorId under
lifecycle.binding (runtimeSessionId and routingGeneration are left out), so
tests/e2e/session.py:get_agent_status must compare the fields that are present
and still refuse a binding that names a different owner.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.e2e import session

ROUTE = SimpleNamespace(user_id="user_abc", actor_id="telegram:123", session_id="ses_" + "a" * 40, generation=3)


def status_with(binding, state="USER_READY"):
    diag = {"runtimePlatform": "V2", "lifecycle": {"state": state, "binding": binding}}
    return {"response": json.dumps(diag)}


def read(binding, state="USER_READY"):
    with patch.object(session, "get_session_route", return_value=ROUTE), \
            patch.object(session, "_payload", return_value={}), \
            patch.object(session, "_invoke", return_value=status_with(binding, state)):
        return session.get_agent_status(object())


def test_ready_status_with_owner_only_binding_is_accepted():
    status = read({"userId": "user_abc", "actorId": "telegram:123"})
    assert status["lifecycle"]["binding"]["userId"] == "user_abc"


def test_full_binding_that_matches_the_route_is_accepted():
    read({"userId": "user_abc", "actorId": "telegram:123", "runtimeSessionId": ROUTE.session_id,
          "routingGeneration": 3})


@pytest.mark.parametrize("binding", [
    {"userId": "someone_else", "actorId": "telegram:123"},
    {"userId": "user_abc", "actorId": "telegram:999"},
    {"actorId": "telegram:123"},
    {"userId": "user_abc", "actorId": "telegram:123", "runtimeSessionId": "ses_other"},
    {"userId": "user_abc", "actorId": "telegram:123", "routingGeneration": 4},
])
def test_binding_for_another_owner_or_route_is_refused(binding):
    with pytest.raises(RuntimeError, match="differs from the saved V2 route"):
        read(binding)


def test_ready_status_without_a_binding_is_refused():
    with pytest.raises(RuntimeError, match="lacks immutable binding evidence"):
        read(None)
