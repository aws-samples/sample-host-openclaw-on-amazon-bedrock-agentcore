"""pytest configuration for E2E tests.

Auto-marks all tests in this directory with pytest.mark.e2e for selective
execution. Provides shared fixtures for config, session reset, and
conversation scenarios.

Usage:
    pytest tests/e2e/ -v                 # run all E2E tests
    pytest tests/e2e/ -v -k smoke        # run only smoke tests
    pytest -m "not e2e"                  # skip E2E tests in fast CI
"""

from __future__ import annotations

import pytest

from tests.e2e.config import load_config, E2EConfig
from tests.e2e.log_tailer import tail_logs
from tests.e2e.session import reset_session, reset_user
from tests.e2e.bot_test import SCENARIOS


# --- Auto-mark all tests in this directory as e2e ---

def pytest_collection_modifyitems(items):
    """Add 'e2e' marker to all tests in the tests/e2e/ directory."""
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


# --- Fixtures ---

@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    """Load E2E config once per test session."""
    return load_config()


@pytest.fixture
def tail(e2e_config):
    """Provide a log tailing function bound to the current config."""
    def _tail(*, start_time=None, chat_id=None, timeout=300):
        return tail_logs(
            e2e_config,
            start_time=start_time,
            chat_id=chat_id,
            timeout=timeout,
        )
    return _tail


@pytest.fixture
def do_reset_session(e2e_config):
    """Reset the user's session (force cold start)."""
    return reset_session(e2e_config)


@pytest.fixture
def do_reset_user(e2e_config):
    """Fully reset the user (delete all DynamoDB items)."""
    return reset_user(e2e_config)


@pytest.fixture(params=list(SCENARIOS.keys()))
def conversation_scenario(request):
    """Parametrized fixture providing each conversation scenario."""
    return request.param, SCENARIOS[request.param]
