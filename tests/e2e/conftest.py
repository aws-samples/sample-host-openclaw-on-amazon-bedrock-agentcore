"""pytest fixtures and configuration for E2E tests."""

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


def pytest_collection_modifyitems(config, items):
    """Auto-mark all tests in this directory with the 'e2e' marker."""
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def e2e_config() -> E2EConfig:
    """Load E2E config once per test session."""
    return load_config()


@pytest.fixture(params=list(SCENARIOS.keys()))
def conversation_scenario(request):
    """Parametrized fixture providing (name, messages) for each scenario."""
    name = request.param
    return name, SCENARIOS[name]
