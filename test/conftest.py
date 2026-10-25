"""Keep automated tests from opening paid-provider or other external sockets."""

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_network_connections(monkeypatch):
    """Allow loopback test servers while rejecting every external socket."""

    def guarded_connect(sock, address):
        if sock.family == socket.AF_UNIX:
            return original_connect(sock, address)

        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="ignore")
        try:
            is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except (ValueError, AttributeError):
            is_loopback = False
        if not is_loopback:
            pytest.fail(f"Test attempted an external network connection to {host!r}")
        return original_connect(sock, address)

    original_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect_ex(sock, address):
        if sock.family == socket.AF_UNIX:
            return original_connect_ex(sock, address)

        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, bytes):
            host = host.decode("ascii", errors="ignore")
        try:
            is_loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
        except (ValueError, AttributeError):
            is_loopback = False
        if not is_loopback:
            pytest.fail(f"Test attempted an external network connection to {host!r}")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
