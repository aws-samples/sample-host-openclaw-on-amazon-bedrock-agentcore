"""RED-first proof that the networkless sandbox fails every egress attempt.

These tests model the locked job namespace with in-process fakes rather than a
real container. Inside :func:`networkless_namespace`, all DNS resolver aliases,
stream/datagram/raw socket egress, VPC-endpoint connects, IMDS, and the boto3
credential-provider chain must fail closed while a pure compute job completes.
"""

from __future__ import annotations

import _socket
import socket

import pytest

from compute import runner

IMDS_ADDRESS = "169.254.169.254"
VPC_ENDPOINT = "10.0.0.4"
LOOPBACK = "127.0.0.1"


def test_dns_resolution_is_blocked_but_pure_job_completes():
    completed = {}
    with runner.networkless_namespace():
        with pytest.raises(runner.NetworklessViolation):
            socket.getaddrinfo("example.com", 443)
        # A pure computation that touches no network still runs to completion.
        completed["value"] = sum(index * index for index in range(1000))
    assert completed["value"] == sum(index * index for index in range(1000))
    # The guard is fully removed once the namespace context exits.
    assert socket.getaddrinfo is runner._ORIGINAL_GETADDRINFO


@pytest.mark.parametrize(
    ("resolver", "arguments"),
    [
        (lambda: socket.gethostbyname, ("localhost",)),
        (lambda: socket.gethostbyname_ex, ("localhost",)),
        (lambda: socket.gethostbyaddr, (LOOPBACK,)),
        (lambda: socket.getnameinfo, ((LOOPBACK, 9), 0)),
        (lambda: socket.getfqdn, ("localhost",)),
        (lambda: _socket.gethostbyname, ("localhost",)),
        (lambda: _socket.getaddrinfo, ("localhost", 9, 0, 0, 0, 0)),
    ],
    ids=[
        "gethostbyname",
        "gethostbyname_ex",
        "gethostbyaddr",
        "getnameinfo",
        "getfqdn",
        "_socket.gethostbyname",
        "_socket.getaddrinfo",
    ],
)
def test_alternate_dns_resolvers_cannot_bypass_the_fence(resolver, arguments):
    with runner.networkless_namespace():
        with pytest.raises(runner.NetworklessViolation):
            resolver()(*arguments)


def test_outbound_internet_tcp_connect_is_refused():
    with runner.networkless_namespace():
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(runner.NetworklessViolation):
            client.connect(("93.184.216.34", 443))


def test_vpc_endpoint_connect_is_refused():
    with runner.networkless_namespace():
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(runner.NetworklessViolation):
            client.connect((VPC_ENDPOINT, 443))


def test_connect_ex_cannot_bypass_the_stream_connect_fence():
    with runner.networkless_namespace():
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(runner.NetworklessViolation):
                client.connect_ex((LOOPBACK, 9))
        finally:
            client.close()


def test_udp_sendto_is_refused_without_resolving_a_hostname():
    with runner.networkless_namespace():
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(runner.NetworklessViolation):
                client.sendto(b"synthetic", (LOOPBACK, 9))
        finally:
            client.close()


@pytest.mark.skipif(
    not hasattr(socket.socket, "sendmsg"), reason="sendmsg is platform dependent"
)
def test_udp_sendmsg_is_refused_without_resolving_a_hostname():
    with runner.networkless_namespace():
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(runner.NetworklessViolation):
                client.sendmsg([b"synthetic"], [], 0, (LOOPBACK, 9))
        finally:
            client.close()


@pytest.mark.parametrize("method_name", ["send", "sendall"])
def test_connected_socket_writes_are_refused(method_name):
    client, peer = socket.socketpair()
    try:
        with runner.networkless_namespace():
            with pytest.raises(runner.NetworklessViolation):
                getattr(client, method_name)(b"synthetic")
    finally:
        client.close()
        peer.close()


def test_connected_socket_sendfile_is_refused(tmp_path):
    payload = tmp_path / "payload.txt"
    payload.write_bytes(b"synthetic")
    client, peer = socket.socketpair()
    try:
        with payload.open("rb") as handle, runner.networkless_namespace():
            with pytest.raises(runner.NetworklessViolation):
                client.sendfile(handle)
    finally:
        client.close()
        peer.close()


@pytest.mark.parametrize(
    "constructor",
    [
        lambda: socket.socket,
        lambda: socket.SocketType,
        lambda: _socket.socket,
        lambda: _socket.SocketType,
    ],
    ids=["socket", "SocketType", "_socket.socket", "_socket.SocketType"],
)
def test_raw_socket_constructors_are_refused_before_syscall(constructor):
    with runner.networkless_namespace():
        with pytest.raises(runner.NetworklessViolation):
            constructor()(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)


def test_low_level_socket_type_cannot_bypass_udp_write_fences():
    with runner.networkless_namespace():
        client = _socket.SocketType(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(runner.NetworklessViolation):
                client.sendto(b"synthetic", (LOOPBACK, 9))
        finally:
            client.close()


def test_imds_endpoint_is_unreachable():
    with runner.networkless_namespace():
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(runner.NetworklessViolation):
            client.connect((IMDS_ADDRESS, 80))
        with pytest.raises(runner.NetworklessViolation):
            socket.create_connection((IMDS_ADDRESS, 80), timeout=1)


def test_boto3_credential_provider_chain_resolves_to_nothing():
    with runner.networkless_namespace():
        with pytest.raises(runner.NetworklessViolation):
            runner.resolve_ambient_credentials()


def test_networkless_namespace_restores_socket_on_exception():
    with pytest.raises(ValueError):
        with runner.networkless_namespace():
            raise ValueError("job body error")
    assert socket.getaddrinfo is runner._ORIGINAL_GETADDRINFO
    assert socket.socket is runner._ORIGINAL_SOCKET
    assert socket.SocketType is runner._ORIGINAL_SOCKET_TYPE
    assert _socket.socket is runner._ORIGINAL_LOW_LEVEL_SOCKET
    assert _socket.SocketType is runner._ORIGINAL_LOW_LEVEL_SOCKET_TYPE
    assert socket.socket.connect is runner._ORIGINAL_CONNECT
    assert socket.socket.connect_ex is runner._ORIGINAL_CONNECT_EX
    assert socket.socket.send is runner._ORIGINAL_SEND
    assert socket.socket.sendall is runner._ORIGINAL_SENDALL
    assert socket.socket.sendto is runner._ORIGINAL_SENDTO
    assert socket.socket.sendfile is runner._ORIGINAL_SENDFILE
    if runner._ORIGINAL_SENDMSG is not None:
        assert socket.socket.sendmsg is runner._ORIGINAL_SENDMSG
