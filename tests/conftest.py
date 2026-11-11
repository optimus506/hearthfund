"""Suite-wide guard: no test may open a network connection off this machine (the app makes no outbound calls
unless Plaid is turned on, and its tests use a fake Plaid; docs/PLAID.md)."""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from typing import Any

import pytest

_connect = socket.socket.connect


def _local(address: Any) -> bool:
    if not isinstance(address, tuple):          # a Unix socket path
        return True
    host = str(address[0])
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _guarded(self: socket.socket, address: Any) -> Any:
    if not _local(address):
        raise AssertionError('a test tried to connect to %r: tests must not use the network' % (address,))
    return _connect(self, address)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(socket.socket, 'connect', _guarded)
    yield


def test_the_guard_refuses_outside_addresses() -> None:
    with socket.socket() as s, pytest.raises(AssertionError, match='must not use the network'):
        s.connect(('192.0.2.1', 443))
