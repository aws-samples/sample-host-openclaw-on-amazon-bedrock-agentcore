"""pytest fixtures and configuration for E2E tests."""

import json
from pathlib import Path

import pytest

from .config import E2EConfig, load_config

# Conversation scenarios: name -> list of messages
SCENARIOS = {
    "greeting": ["Hello! How are you?"],
    "multi_turn": [
        "Hi, remember me? I'm running an E2E test.",
        "What did I just say in my previous message?",
        "Great, thanks for confirming. Goodbye!",
    ],
    "task_request": ["Write a haiku about cloud computing."],
    "rapid_fire": [
        "Quick question one: what is 2+2?",
        "Quick question two: what is 3+3?",
    ],
    "file_operations": [
        'Save the text "E2E_SCOPED_CREDS_OK" to a file called e2e-creds-test.txt',
        "Read the contents of e2e-creds-test.txt",
        "Delete the file e2e-creds-test.txt",
    ],
}


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line("markers", "e2e: end-to-end tests requiring a deployed stack")
    config.addinivalue_line("markers", "guardrail: Bedrock Guardrail wiring tests")


def pytest_collection_modifyitems(config, items):
    """Auto-mark all tests in this directory with the 'e2e' marker."""
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    """Load E2E config once per test session."""
    return load_config()


@pytest.fixture(scope="session")
def browser_enabled() -> bool:
    """Check if enable_browser=true in cdk.json. Skip tests if not enabled."""
    cdk_json = Path(__file__).resolve().parents[2] / "cdk.json"
    enabled = False
    if cdk_json.exists():
        with open(cdk_json) as f:
            ctx = json.load(f).get("context", {})
            enabled = ctx.get("enable_browser", False)
    if not enabled:
        pytest.skip("Browser feature not enabled (set enable_browser=true in cdk.json)")
    return True


@pytest.fixture(params=list(SCENARIOS.keys()))
def conversation_scenario(request):
    """Parametrized fixture providing (name, messages) for each scenario."""
    name = request.param
    return name, SCENARIOS[name]
