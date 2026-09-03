"""Shared fixtures.  No TV and no WLED are required: both are faked."""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ambiwled import config as config_mod
from ambiwled.mapping import Layout

# The layout this TV actually reports: three sides, no bottom.
REAL_LAYOUT = Layout([("left", 5), ("top", 11), ("right", 5)])


@pytest.fixture
def cfg():
    return config_mod.default_config()


@pytest.fixture
def layout():
    return Layout(list(REAL_LAYOUT.sides))


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Point the config module at a temp dir so tests never touch /config."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    return path


@pytest.fixture
def udp_sink():
    """A bound UDP socket standing in for WLED.  Yields (host, port, recv)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    host, port = sock.getsockname()

    def recv_all():
        packets = []
        sock.setblocking(False)
        try:
            while True:
                packets.append(sock.recv(65535))
        except BlockingIOError:
            pass
        finally:
            sock.setblocking(True)
        return packets

    yield host, port, recv_all
    sock.close()


def ambilight_payload(sides=(("left", 5), ("top", 11), ("right", 5)), value=None):
    """Build a TV response shaped exactly like the real one."""
    layer = {}
    n = 0
    for side, count in sides:
        layer[side] = {}
        for i in range(count):
            v = value(side, i) if value else {"r": n, "g": (n * 2) % 256, "b": (n * 3) % 256}
            layer[side][str(i)] = v
            n += 1
    return {"layer1": layer}
