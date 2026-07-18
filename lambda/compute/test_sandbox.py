"""RED-first proof that the networkless sandbox fails every egress attempt.

These tests model the locked job namespace with in-process fakes rather than a
real container. Inside :func:`networkless_namespace`, DNS resolution, outbound
TCP, VPC-endpoint connects, IMDS, and the boto3 credential-provider chain must
all fail closed while a pure compute job still completes.
"""

from __future__ import annotations

import socket

import pytest

from compute import runner

IMDS_ADDRESS = "169.254.169.254"
VPC_ENDPOINT = "10.0.0.4"


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
    assert socket.socket.connect is runner._ORIGINAL_CONNECT
