"""Unit tests may use AWS fakes or loopback servers, never remote AWS transport."""

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from botocore.httpsession import URLLib3Session
import pytest


def is_loopback_url(url):
    host = urlsplit(url).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_remote_aws_transport(request, monkeypatch):
    if "e2e" in Path(request.node.path).relative_to(Path(__file__).parent).parts:
        yield
        return
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "offline-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "offline-test")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "offline-test")
    original = URLLib3Session.send
    violations = []

    def send(session, http_request):
        if not is_loopback_url(http_request.url):
            violations.append(urlsplit(http_request.url).hostname)
            raise AssertionError("Unit test attempted non-loopback AWS HTTP transport")
        return original(session, http_request)

    monkeypatch.setattr(URLLib3Session, "send", send)
    yield
    assert not violations, f"Non-loopback AWS HTTP blocked before connection: {violations}"
