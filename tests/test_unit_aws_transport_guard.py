from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from botocore.httpsession import URLLib3Session
import pytest

from tests.conftest import block_remote_aws_transport, is_loopback_url


@pytest.mark.parametrize("url,allowed", [
    ("https://bedrock-agentcore.us-west-2.amazonaws.com/invocations", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("http://localhost.example.com", False),
    ("http://localhost:8080", True),
    ("http://127.0.0.1:8080", True),
    ("http://[::1]:8080", True),
])
def test_only_literal_loopback_transport_allowed(url, allowed):
    assert is_loopback_url(url) is allowed


def test_guard_rejects_before_send_and_reports_even_if_application_swallows_error(monkeypatch):
    original_send = Mock()
    monkeypatch.setattr(URLLib3Session, "send", original_send)
    request = SimpleNamespace(node=SimpleNamespace(path=Path(__file__)))
    guard = block_remote_aws_transport.__wrapped__(request, monkeypatch)
    next(guard)
    with pytest.raises(AssertionError, match="non-loopback"):
        URLLib3Session().send(SimpleNamespace(url="https://sts.us-west-2.amazonaws.com"))
    original_send.assert_not_called()
    with pytest.raises(AssertionError, match="blocked before connection"):
        next(guard)


def test_guard_allows_fake_loopback_send_and_exempts_actual_e2e(monkeypatch):
    original_send = Mock(return_value="local")
    monkeypatch.setattr(URLLib3Session, "send", original_send)
    for path, url in [
        (Path(__file__), "http://127.0.0.1:1234"),
        (Path(__file__).parent / "e2e/test_example.py", "https://example.invalid"),
    ]:
        request = SimpleNamespace(node=SimpleNamespace(path=path))
        with monkeypatch.context() as changes:
            guard = block_remote_aws_transport.__wrapped__(request, changes)
            next(guard)
            assert URLLib3Session().send(SimpleNamespace(url=url)) == "local"
            with pytest.raises(StopIteration):
                next(guard)
    assert original_send.call_count == 2
