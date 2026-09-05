"""Shared fixtures.  No TV and no WLED are required: both are faked."""
from __future__ import annotations

import json
import socket
from pathlib import Path

import aiohttp
import pytest

from ambiwled import config as config_mod
from ambiwled.bridge import Bridge
from ambiwled.mapping import Layout
from ambiwled.server import Server

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


@pytest.fixture
async def aiohttp_client_session():
    """A plain client session, for tests that talk to a fake server directly
    rather than through Bridge/Server (test_pairing.py)."""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture(scope="session")
def browser():
    """Headless Firefox, for the handful of tests in test_ui.py that check
    actual browser behaviour (DOM state, event listeners, redraws) rather
    than the HTTP/websocket API alone - a real config-schema round trip
    would happily miss a UI-only bug like duplicated DOM nodes or a stale
    fetch response winning a race, since nothing about the API contract
    changes when those go wrong.

    Skipped, not failed, when Firefox/geckodriver aren't installed: unlike
    the TV and the controller, a browser is not something this project
    fakes, so `make test` stays runnable without one - this class of test
    only runs where a browser actually exists to run it in.
    """
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.firefox.options import Options

    def try_launch(binary_location=None):
        opts = Options()
        opts.add_argument("--headless")
        opts.add_argument("--width=430")
        opts.add_argument("--height=900")
        if binary_location:
            opts.binary_location = binary_location
        try:
            return webdriver.Firefox(options=opts)
        except WebDriverException:
            return None

    driver = try_launch()
    if driver is None:
        # A snap-packaged Firefox exposes a shell-script wrapper at the
        # usual binary path, which Selenium cannot launch directly - the
        # real ELF binary underneath it works fine.
        snap_firefox = "/snap/firefox/current/usr/lib/firefox/firefox"
        if Path(snap_firefox).exists():
            driver = try_launch(snap_firefox)
    if driver is None:
        pytest.skip("Firefox/geckodriver not available - browser UI tests skipped")
    yield driver
    driver.quit()


@pytest.fixture
async def running(tmp_config):
    """A real Server on an ephemeral port.  The poller is never started, so no
    TV is contacted; the output loop is not running either."""
    cfg = config_mod.default_config()
    cfg["web"] = {"host": "127.0.0.1", "port": 0, "preview_fps": 15.0}
    config_mod.save(cfg)

    bridge = Bridge(cfg)
    server = Server(bridge)
    runner = await server.start()
    host, port = runner.addresses[0][:2]
    base = f"http://{host}:{port}"
    async with aiohttp.ClientSession() as session:
        yield server, bridge, base, session, tmp_config
    await server.stop(runner)


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
