"""Philips JointSPACE pairing: unlocks /powerstate and /screenstate.

Ambilight's own zone data and /ambilight/power both keep answering - and
claim the TV is on - through Philips' "Quick Start" network standby, for
however long that grace period lasts after the screen actually turns off.
/powerstate and /screenstate live behind digest auth on HTTPS (port 1926,
non-standard for the rest of this project's plain-HTTP polling) and answer
truthfully regardless: MEASURED against a real 2018-era Android TV set,
"Standby"/"Off" the instant the screen went dark, while /ambilight/power
kept saying "On" for minutes.

AUTH_SHARED_KEY is not a secret of this project's or the user's - it is a
fixed key baked into Philips' own companion apps, long since extracted and
published by the open-source JointSPACE client community (pylips,
ha-philipsjs); every such client hardcodes the same bytes to compute the
pairing PIN's HMAC signature.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from base64 import b64decode, b64encode
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

AUTH_SHARED_KEY = b64decode(
    "ZmVay1EQVFOaZhwQ4Kv81ypLAZNczV9sG4KkseXWn1NEk6cXmPKO/MCa9sryslvLCFMnNe4Z4CPXzToowvhHvA=="
)

HTTPS_PORT = 1926


class PairingError(Exception):
    """Pairing rejected or the TV could not be reached. Message is UI-facing."""


@dataclass
class PairingState:
    """Held between `request_pin()` and `grant_pin()` - one pairing attempt."""

    device_id: str
    device: dict[str, str]
    timestamp: int
    auth_key: str


def _device_spec(device_id: str) -> dict[str, str]:
    return {
        "device_name": "heliotrope",
        "device_os": "Android",
        "app_name": "AmbiWled",
        "type": "native",
        "id": device_id,
        "app_id": "ambiwled",
    }


def _base_url(tv_ip: str, port: int, scheme: str = "https") -> str:
    return f"{scheme}://{tv_ip}:{port}/6"


async def request_pin(
    session: aiohttp.ClientSession, tv_ip: str, port: int = HTTPS_PORT, scheme: str = "https"
) -> PairingState:
    """Start a pairing attempt. The TV shows a PIN on screen; pass it to
    `grant_pin()` with the returned state to finish.

    `scheme` only exists so tests can point this at a plain-HTTP fake
    server instead of standing up TLS - real callers never pass it."""
    device_id = secrets.token_hex(16)
    device = _device_spec(device_id)
    body = {"access": {"scope": ["read", "write", "control"]}, "device": device}
    url = f"{_base_url(tv_ip, port, scheme)}/pair/request"
    try:
        async with session.post(url, json=body, ssl=False, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json(content_type=None)
    except TimeoutError as exc:
        raise PairingError("could not reach the TV: timed out") from exc
    except aiohttp.ClientError as exc:
        raise PairingError(f"could not reach the TV: {exc}") from exc
    if not isinstance(data, dict) or data.get("error_id") != "SUCCESS":
        raise PairingError((data or {}).get("error_text", "pairing request rejected"))
    return PairingState(
        device_id=device_id, device=device, timestamp=int(data["timestamp"]), auth_key=str(data["auth_key"])
    )


def _hmac_signature(timestamp: int, pin: str) -> str:
    mac = hmac.new(AUTH_SHARED_KEY, str(timestamp).encode() + pin.encode(), hashlib.sha1)
    return b64encode(mac.digest()).decode()


_CHALLENGE_FIELD = re.compile(r'(\w+)=("[^"]*"|[^,]+)')


def _parse_challenge(www_authenticate: str) -> dict[str, str]:
    _, _, rest = www_authenticate.partition(" ")
    return {k: v.strip('"') for k, v in _CHALLENGE_FIELD.findall(rest)}


def _digest_header(username: str, password: str, method: str, uri: str, challenge: dict[str, str]) -> str:
    realm, nonce, qop, opaque = (
        challenge["realm"], challenge["nonce"], challenge.get("qop"), challenge.get("opaque"),
    )
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    nc, cnonce = "00000001", secrets.token_hex(8)
    if qop:
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    parts = [f'username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
             f'uri="{uri}"', f'response="{response}"']
    if opaque:
        parts.append(f'opaque="{opaque}"')
    if qop:
        parts += ["qop=auth", f"nc={nc}", f'cnonce="{cnonce}"']
    return "Digest " + ", ".join(parts)


async def _digest_request(
    session: aiohttp.ClientSession,
    method: str,
    tv_ip: str,
    path: str,
    username: str,
    password: str,
    port: int = HTTPS_PORT,
    json_body: dict[str, Any] | None = None,
    timeout: float = 3.0,
    scheme: str = "https",
) -> dict[str, Any]:
    """One digest-authenticated request: an unauthenticated attempt to collect
    the challenge, then the real request signed against it. Philips' TVs
    don't require nonce counting across requests, so a fresh challenge per
    call (rather than caching one) keeps this simple and stateless."""
    uri = f"/6/{path}"
    url = f"{_base_url(tv_ip, port, scheme)}/{path}"
    to = aiohttp.ClientTimeout(total=timeout)
    async with session.request(method, url, json=json_body, ssl=False, timeout=to) as r:
        if r.status != 401:
            r.raise_for_status()
            return await r.json(content_type=None)
        challenge = _parse_challenge(r.headers.get("WWW-Authenticate", ""))
    headers = {"Authorization": _digest_header(username, password, method, uri, challenge)}
    async with session.request(method, url, json=json_body, headers=headers, ssl=False, timeout=to) as r:
        r.raise_for_status()
        return await r.json(content_type=None)


async def grant_pin(
    session: aiohttp.ClientSession, state: PairingState, pin: str, tv_ip: str,
    port: int = HTTPS_PORT, scheme: str = "https",
) -> tuple[str, str]:
    """Finish pairing with the PIN shown on the TV. Returns (device_id,
    auth_key) to persist - the credentials for every future /powerstate and
    /screenstate call."""
    signature = _hmac_signature(state.timestamp, pin)
    auth = {
        "auth_appId": "1",
        "auth_timestamp": state.timestamp,
        "auth_signature": signature,
        "pin": pin,
    }
    body = {"auth": auth, "device": state.device}
    try:
        data = await _digest_request(
            session, "POST", tv_ip, "pair/grant", state.device_id, state.auth_key,
            port=port, json_body=body, timeout=5.0, scheme=scheme,
        )
    except aiohttp.ClientResponseError as exc:
        raise PairingError(f"the TV rejected pairing (HTTP {exc.status})") from exc
    except TimeoutError as exc:
        raise PairingError("could not reach the TV: timed out") from exc
    except aiohttp.ClientError as exc:
        raise PairingError(f"could not reach the TV: {exc}") from exc
    if not isinstance(data, dict) or data.get("error_id") != "SUCCESS":
        raise PairingError((data or {}).get("error_text", "wrong PIN"))
    return state.device_id, state.auth_key


async def get_screen_state(
    session: aiohttp.ClientSession, tv_ip: str, device_id: str, auth_key: str,
    port: int = HTTPS_PORT, timeout: float = 3.0, scheme: str = "https",
) -> str | None:
    """The TV's own truthful answer: 'On' or 'Off'/'Standby' (exact strings
    vary a little by firmware - callers should only ever compare against
    "On", never enumerate the off-ish values). None if unreachable or not
    paired against this TV - callers fall back to the ambilight-based
    detection in that case, same as before pairing existed."""
    try:
        data = await _digest_request(
            session, "GET", tv_ip, "screenstate", device_id, auth_key,
            port=port, timeout=timeout, scheme=scheme,
        )
    except Exception:
        # Deliberately broad, not just aiohttp.ClientError: a real TV gone
        # fully dark (deep standby, past Quick-Start's grace period) does
        # not refuse the connection, it just never answers - confirmed
        # live, that surfaces as a bare TimeoutError/asyncio.TimeoutError,
        # not aiohttp.ClientError. Any failure here degrades to "unknown";
        # this is a best-effort secondary check and must never take down
        # the poll loop that calls it.
        return None
    if not isinstance(data, dict):
        return None
    return data.get("screenstate")
