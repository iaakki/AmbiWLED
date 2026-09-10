"""The Docker HEALTHCHECK script: a standalone file, not part of the package,
so it is imported straight from the repo root rather than `ambiwled.*`."""
from __future__ import annotations

import http.server
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import healthcheck  # noqa: E402


def test_port_defaults_when_no_config_exists(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    assert healthcheck._port() == healthcheck.DEFAULT_PORT


def test_port_is_read_from_config_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"web": {"port": 9090}}))
    assert healthcheck._port() == 9090


def test_port_falls_back_on_a_corrupt_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json")
    assert healthcheck._port() == healthcheck.DEFAULT_PORT


def test_port_falls_back_when_the_web_section_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"led": {"count": 462}}))
    assert healthcheck._port() == healthcheck.DEFAULT_PORT


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


@pytest.fixture
def fake_service():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, port
    server.shutdown()
    thread.join(timeout=2)


def test_main_succeeds_when_the_service_answers(tmp_path, monkeypatch, fake_service):
    server, port = fake_service
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"web": {"port": port}}))
    assert healthcheck.main() == 0


def test_main_fails_on_a_server_error(tmp_path, monkeypatch, fake_service):
    server, port = fake_service
    server.RequestHandlerClass.status = 503
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"web": {"port": port}}))
    assert healthcheck.main() == 1
    server.RequestHandlerClass.status = 200   # restore for any test after this one


def test_main_fails_when_nothing_is_listening(tmp_path, monkeypatch):
    monkeypatch.setenv("AMBIWLED_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"web": {"port": 1}}))
    assert healthcheck.main() == 1
