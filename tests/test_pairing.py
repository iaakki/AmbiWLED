"""JointSPACE pairing against a fake TV. No real TV required.

The digest-auth check in FakeTvHttps mirrors pairing.py's own hashing
exactly, so this exercises the real algorithm end-to-end - not just that
the client sends *some* Authorization header. The algorithm itself was
verified once against a real Philips Android TV (see pairing.py's
docstring); this pins it down so it can't regress silently.
"""
from __future__ import annotations

import hashlib
import re

import pytest
from aiohttp import web

from ambiwled import pairing

PIN = "4242"


class FakeTvHttps:
    """Serves pair/request, pair/grant and screenstate/powerstate exactly
    like a real Philips TV over HTTPS:1926 - here over plain HTTP, since the
    TLS layer isn't what this project's own code is responsible for."""

    def __init__(self, pin: str = PIN, screenstate: str = "On"):
        self.pin = pin
        self.screenstate = screenstate
        self.auth_key = "server-issued-auth-key"
        self.timestamp = 555111
        self.nonce = "fixed-test-nonce"
        self.granted: tuple[str, str] | None = None  # (device_id, auth_key) once granted
        self.pair_requests = 0
        self.grant_attempts = 0

    async def start(self):
        app = web.Application()
        app.router.add_post("/6/pair/request", self.pair_request)
        app.router.add_post("/6/pair/grant", self.pair_grant)
        app.router.add_get("/6/screenstate", self.get_screenstate)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        await self.runner.cleanup()

    async def pair_request(self, request):
        self.pair_requests += 1
        body = await request.json()
        self._pending_device = body["device"]
        return web.json_response({
            "error_id": "SUCCESS", "error_text": "Authorization required",
            "auth_key": self.auth_key, "timestamp": self.timestamp, "timeout": 60,
        })

    def _challenge_response(self):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": f'Digest realm="XTV", nonce="{self.nonce}", algorithm=MD5, qop="auth"'},
        )

    def _expected_response(self, username, password, method, uri, auth_fields):
        ha1 = hashlib.md5(f"{username}:XTV:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        nc, cnonce = auth_fields.get("nc", "00000001"), auth_fields.get("cnonce", "")
        return hashlib.md5(f"{ha1}:{self.nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()

    def _authorized(self, request, username, password, method, uri):
        header = request.headers.get("Authorization")
        if not header:
            return False
        fields = dict(re.findall(r'(\w+)="?([^",]+)"?', header))
        if fields.get("username") != username:
            return False
        expected = self._expected_response(username, password, method, uri, fields)
        return fields.get("response") == expected

    async def pair_grant(self, request):
        self.grant_attempts += 1
        if not self._authorized(request, self._pending_device["id"], self.auth_key, "POST", "/6/pair/grant"):
            return self._challenge_response()
        body = await request.json()
        auth = body["auth"]
        expected_sig = pairing._hmac_signature(auth["auth_timestamp"], self.pin)
        if auth["pin"] != self.pin or auth["auth_signature"] != expected_sig:
            return web.json_response({"error_id": "INVALID_PIN", "error_text": "Invalid authentication parameters"})
        self.granted = (self._pending_device["id"], self.auth_key)
        return web.json_response({"error_id": "SUCCESS", "error_text": "Pairing completed"})

    async def get_screenstate(self, request):
        if self.granted is None:
            return self._challenge_response()
        device_id, auth_key = self.granted
        if not self._authorized(request, device_id, auth_key, "GET", "/6/screenstate"):
            return self._challenge_response()
        return web.json_response({"screenstate": self.screenstate})


async def _pair(session, tv, pin=PIN):
    state = await pairing.request_pin(session, "127.0.0.1", port=tv.port, scheme="http")
    return await pairing.grant_pin(session, state, pin, "127.0.0.1", port=tv.port, scheme="http")


async def test_pairing_succeeds_with_the_right_pin(aiohttp_client_session):
    tv = await FakeTvHttps().start()
    try:
        device_id, auth_key = await _pair(aiohttp_client_session, tv)
        assert device_id and auth_key
        assert tv.granted == (device_id, auth_key)
    finally:
        await tv.stop()


async def test_pairing_fails_with_the_wrong_pin(aiohttp_client_session):
    tv = await FakeTvHttps().start()
    try:
        with pytest.raises(pairing.PairingError):
            await _pair(aiohttp_client_session, tv, pin="0000")
        assert tv.granted is None
    finally:
        await tv.stop()


async def test_get_screen_state_returns_the_truthful_value(aiohttp_client_session):
    tv = await FakeTvHttps(screenstate="Off").start()
    try:
        device_id, auth_key = await _pair(aiohttp_client_session, tv)
        state = await pairing.get_screen_state(
            aiohttp_client_session, "127.0.0.1", device_id, auth_key, port=tv.port, scheme="http")
        assert state == "Off"
    finally:
        await tv.stop()


async def test_get_screen_state_is_none_when_never_paired(aiohttp_client_session):
    tv = await FakeTvHttps().start()
    try:
        state = await pairing.get_screen_state(
            aiohttp_client_session, "127.0.0.1", "unknown-device", "wrong-key", port=tv.port, scheme="http")
        assert state is None
    finally:
        await tv.stop()


async def test_get_screen_state_is_none_when_unreachable(aiohttp_client_session):
    state = await pairing.get_screen_state(
        aiohttp_client_session, "127.0.0.1", "d", "k", port=1, timeout=0.3, scheme="http")
    assert state is None
