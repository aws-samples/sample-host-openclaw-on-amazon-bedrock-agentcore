import importlib.util
from pathlib import Path
import sys

import pytest


GMAIL_DIR = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, GMAIL_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_load("gmail_models", "models.py")
scanner_module = _load("gmail_scanner_providers", "scanner.py")


class Request:
    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def execute(self):
        if self._error is not None:
            raise self._error
        return self._result


class ThreadsResource:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return Request({"threads": [{"id": "thread-1"}]})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return Request({"id": kwargs["id"], "messages": []})


class UsersResource:
    def __init__(self, threads):
        self._threads = threads

    def threads(self):
        return self._threads


class GmailService:
    def __init__(self, threads):
        self._users = UsersResource(threads)

    def users(self):
        return self._users


def test_google_api_adapter_exposes_only_read_thread_operations():
    threads = ThreadsResource()
    adapter = scanner_module.GoogleGmailApiClient(GmailService(threads))

    assert adapter.list_threads(query="in:sent", max_results=50) == [
        {"id": "thread-1"}
    ]
    assert adapter.get_thread(thread_id="thread-1", format="full") == {
        "id": "thread-1",
        "messages": [],
    }
    assert threads.calls == [
        (
            "list",
            {"userId": "me", "q": "in:sent", "maxResults": 50},
        ),
        (
            "get",
            {"userId": "me", "id": "thread-1", "format": "full"},
        ),
    ]
    assert not hasattr(adapter, "send")
    assert not hasattr(adapter, "create_draft")


def test_google_api_adapter_rejects_malformed_responses_and_redacts_provider_errors():
    class BadThreads(ThreadsResource):
        def list(self, **kwargs):
            return Request({"threads": "not-a-list"})

        def get(self, **kwargs):
            return Request(error=RuntimeError("ACCESS-TOKEN-AND-RAW-BODY"))

    adapter = scanner_module.GoogleGmailApiClient(GmailService(BadThreads()))

    with pytest.raises(scanner_module.GmailProviderError) as malformed:
        adapter.list_threads(query="in:sent", max_results=50)
    assert "ACCESS-TOKEN" not in str(malformed.value)

    with pytest.raises(scanner_module.GmailProviderError) as failed:
        adapter.get_thread(thread_id="thread-1", format="full")
    assert str(failed.value) == "Gmail thread retrieval failed"
    assert "RAW-BODY" not in repr(failed.value)
