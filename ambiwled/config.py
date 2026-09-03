"""Configuration: defaults, load/save, validation."""
from __future__ import annotations

import copy
import json
import logging
import os
import ipaddress
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("AMBIWLED_CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"

# Synthetic edge sources (no TV zones behind them).
SYNTH_SOURCES = ("synth_gradient", "mirror_top", "average", "off")

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 2,
    "log_level": "INFO",
    # Whether we drive the strip at all.  `off` stops sending, which lets the
    # controller's realtime timeout hand the ceiling back to its own preset.
    "mode": "ambilight",          # ambilight | preset | off
    # Which controller preset to hand the strip to in "preset" mode. Presets
    # are user content on the controller (an audio-reactive one, a favourite
    # effect) - this project does not define what is in them.
    "preset_slot": 0,
    "source": {
        # Blank until configured: the service still starts and serves the UI.
        "tv_ip": "",
        "port": 1925,                 # JointSPACE default; rarely needs changing
        "api_version": None,          # null = auto-detect, else 1 or 6
        "endpoint": "processed",      # processed | measured
        "poll_hz": 20.0,
        "request_timeout_s": 1.0,
        "fail_threshold": 3,
        "off_probe_interval_s": 5.0,
        "adaptive_backoff": True,
        # Poll /ambilight/power so "TV on but Ambilight off" is distinguishable
        # from "TV showing a black frame".  0 disables the check.
        "ambilight_power_check_s": 3.0,
    },
    "led": {
        "count": 462,
    },
    "mapping": {
        "corner_blend": True,
        "edges": [
            {
                "name": "tv_wall",
                "pixel_start": 0,
                "pixel_count": 133,
                "source": "top",
                "reversed": False,
                "brightness": 1.0,
            },
            {
                "name": "right_side",
                "pixel_start": 133,
                "pixel_count": 98,
                "source": "right",
                "reversed": False,
                "brightness": 1.0,
            },
            {
                "name": "rear",
                "pixel_start": 231,
                "pixel_count": 133,
                "source": "synth_gradient",
                "synth_from": ["right", -1],
                "synth_to": ["left", 0],
                "mirror_of": "tv_wall",
                "reversed": False,
                "brightness": 1.0,
            },
            {
                "name": "left_side",
                "pixel_start": 364,
                "pixel_count": 98,
                "source": "left",
                "reversed": False,
                "brightness": 1.0,
            },
        ],
    },
    "colour": {
        "saturation": 1.0,
        # Brightness is a perceptual level: 0.5 looks half as bright, rather
        # than being half the PWM value (which looks far dimmer than half).
        "brightness": 1.0,
        "perceptual_brightness": True,
        "perceptual_gamma": 2.2,
        "gamma": 1.0,
        "black_floor": 0,
    },
    "dimming": {
        "enabled": False,
        "latitude": 0.0,            # set from the UI: "Use my location" or paste coordinates
        "longitude": 0.0,
        # A parabola peaking at solar noon, meeting night_level exactly at
        # sunrise and sunset. No schedule, no offsets - just these two levels
        # and where you are.
        "day_level": 1.0,
        "night_level": 0.4,
    },
    "frames": {
        # Real TV samples arrive at source.poll_hz; this is how often the
        # strip gets an update. Interpolated linearly between the last two
        # real samples, so equal to poll_hz means no interpolation at all.
        "output_fps": 60.0,
    },
    "output": {
        "timeout_s": 2,
        # Stop sending when nothing is listening.  UDP gives no feedback, so
        # reachability is probed over the controller's HTTP API instead.
        "require_online": True,
        "presence_check_s": 10.0,
        "presence_timeout_s": 2.0,
        "presence_fail_threshold": 2,
        "targets": [
            {
                "host": "",
                "port": 21324,
                "http_port": 80,
                "pixel_offset": 0,
                "pixel_count": 462,
                "enabled": False,
            }
        ],
    },
    "mqtt": {
        "enabled": False,
        "host": "",                   # e.g. the machine running Home Assistant
        "port": 1883,
        "username": "",
        "password": "",
        "base_topic": "ambiwled",
        "discovery_prefix": "homeassistant",
        "device_name": "AmbiWled",
        "publish_interval_s": 2.0,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "preview_fps": 15.0,
    },
}


_HOSTNAME = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


def _is_host(value: Any) -> bool:
    """A usable IP address or hostname.  Rejects half-typed addresses."""
    if not isinstance(value, str) or not value.strip():
        return False
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    return bool(_HOSTNAME.match(value)) and not value.replace(".", "").isdigit()


# Never handed out over the API; see redacted() / unredact().
SECRET_PATHS = (("mqtt", "password"),)
REDACTED = "********"


def redacted(cfg: dict[str, Any]) -> dict[str, Any]:
    """A copy safe to serve: secrets replaced by a sentinel.

    The web UI is unauthenticated on the LAN, so the broker password must not
    be readable from it.
    """
    out = copy.deepcopy(cfg)
    for section, key in SECRET_PATHS:
        if out.get(section, {}).get(key):
            out[section][key] = REDACTED
    return out


def unredact(candidate: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Turn the sentinel back into the stored secret, so a UI round-trip that
    never saw the password cannot erase it."""
    for section, key in SECRET_PATHS:
        if candidate.get(section, {}).get(key) == REDACTED:
            candidate[section][key] = current.get(section, {}).get(key, "")
    return candidate


class ValidationError(Exception):
    """Config rejected. Message is UI-facing."""


def default_config() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_CONFIG)


def _merge(base: Any, patch: Any) -> Any:
    """Recursive dict merge. Lists are replaced wholesale, not merged."""
    if isinstance(base, dict) and isinstance(patch, dict):
        out = dict(base)
        for k, v in patch.items():
            out[k] = _merge(base.get(k), v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(patch)


def _prune_unknown(cfg: Any, default: Any) -> Any:
    """Drop dict keys that no longer exist in the schema at that path.

    Only recurses into dicts: list items (mapping.edges, output.targets) are
    user content, not schema, and are left exactly as they are. Without this,
    a setting removed in a later version — like the dimming transition width
    this replaced with a curve exponent — sits in config.json forever, dead
    but never cleaned up.
    """
    if isinstance(cfg, dict) and isinstance(default, dict):
        return {k: _prune_unknown(v, default[k]) for k, v in cfg.items() if k in default}
    return cfg


def merge_defaults(cfg: dict[str, Any]) -> dict[str, Any]:
    """Fill in anything the stored file is missing (forward compatibility),
    and drop anything it has that the schema no longer defines."""
    return _prune_unknown(_merge(default_config(), cfg), default_config())


def validate(cfg: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems. Empty list = valid."""
    errors: list[str] = []

    try:
        led_count = int(cfg["led"]["count"])
    except (KeyError, TypeError, ValueError):
        return ["led.count must be an integer"]
    if led_count <= 0:
        errors.append("led.count must be greater than zero")

    edges = cfg.get("mapping", {}).get("edges")
    if not isinstance(edges, list) or not edges:
        errors.append("mapping.edges must be a non-empty list")
        return errors

    names: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    for i, e in enumerate(edges):
        label = e.get("name") or f"edge[{i}]"
        if not isinstance(e, dict):
            errors.append(f"{label}: not an object")
            continue
        if not e.get("name"):
            errors.append(f"edge[{i}]: missing name")
        elif e["name"] in names:
            errors.append(f"{label}: duplicate edge name")
        else:
            names.add(e["name"])

        try:
            start = int(e["pixel_start"])
            count = int(e["pixel_count"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label}: pixel_start and pixel_count must be integers")
            continue
        if start < 0:
            errors.append(f"{label}: pixel_start must be >= 0")
        if count <= 0:
            errors.append(f"{label}: pixel_count must be > 0")
        if start + count > led_count:
            errors.append(
                f"{label}: runs past the end of the strip "
                f"({start}+{count} > {led_count})"
            )
        if not e.get("source"):
            errors.append(f"{label}: missing source")
        edge_bri = e.get("brightness", 1.0)
        try:
            if not (0.0 <= float(edge_bri) <= 2.0):
                errors.append(f"{label}: brightness must be between 0 and 2")
        except (TypeError, ValueError):
            errors.append(f"{label}: brightness must be a number")
        spans.append((start, count, label))

    # Overlaps and gaps.
    spans.sort()
    cursor = 0
    for start, count, label in spans:
        if start < cursor:
            errors.append(f"{label}: overlaps the previous edge at pixel {start}")
        elif start > cursor:
            errors.append(f"gap of {start - cursor} pixels before {label} (pixels {cursor}-{start - 1})")
        cursor = max(cursor, start + count)
    if cursor < led_count:
        errors.append(f"gap of {led_count - cursor} pixels at the end (pixels {cursor}-{led_count - 1})")

    total = sum(c for _, c, _ in spans)
    if total != led_count:
        errors.append(f"edge pixel counts total {total}, but led.count is {led_count}")

    s = cfg.get("source", {})
    tv_ip = str(s.get("tv_ip", "") or "").strip()
    if tv_ip and not _is_host(tv_ip):
        errors.append(f"source.tv_ip '{tv_ip}' is not a valid IP address or hostname")
    if not (1 <= int(s.get("port", 1925)) <= 65535):
        errors.append("source.port must be between 1 and 65535")
    if not (0.5 <= float(s.get("poll_hz", 20)) <= 60):
        errors.append("source.poll_hz must be between 0.5 and 60")
    if s.get("api_version") not in (None, 1, 6):
        errors.append("source.api_version must be null, 1 or 6")
    if s.get("endpoint") not in ("processed", "measured"):
        errors.append("source.endpoint must be processed or measured")

    # Frame generation: an output rate, not a smoothing amount. It must be
    # able to interpolate evenly between real samples, so it is a whole
    # multiple of the poll rate; equal to it means passthrough.
    poll_hz = float(s.get("poll_hz", 20.0))
    f = cfg.get("frames", {})
    out_fps = float(f.get("output_fps", 60.0))
    if not (poll_hz <= out_fps <= 120):
        errors.append("frames.output_fps must be between source.poll_hz and 120")
    else:
        ratio = out_fps / poll_hz
        if abs(ratio - round(ratio)) > 1e-6:
            errors.append("frames.output_fps must be a whole multiple of source.poll_hz")

    d = cfg.get("dimming", {})
    if not (-90.0 <= float(d.get("latitude", 0)) <= 90.0):
        errors.append("dimming.latitude must be between -90 and 90")
    if not (-180.0 <= float(d.get("longitude", 0)) <= 180.0):
        errors.append("dimming.longitude must be between -180 and 180")
    for key in ("day_level", "night_level"):
        if not (0.0 <= float(d.get(key, 1.0)) <= 1.0):
            errors.append(f"dimming.{key} must be between 0 and 1")

    if cfg.get("mode") not in ("ambilight", "preset", "off"):
        errors.append("mode must be ambilight, preset or off")
    if cfg.get("mode") == "preset" and not (1 <= int(cfg.get("preset_slot") or 0) <= 250):
        errors.append("preset_slot must be set (1-250) to use preset mode")

    q = cfg.get("mqtt", {})
    if q.get("enabled"):
        if not _is_host(q.get("host", "")):
            errors.append(f"mqtt.host '{q.get('host', '')}' is not a valid IP address or hostname")
        if not (1 <= int(q.get("port", 1883)) <= 65535):
            errors.append("mqtt.port must be between 1 and 65535")
        if not str(q.get("base_topic", "")).strip("/"):
            errors.append("mqtt.base_topic must not be empty")
    if float(q.get("publish_interval_s", 2.0)) < 0.5:
        errors.append("mqtt.publish_interval_s must be at least 0.5")

    c = cfg.get("colour", {})
    if not (0.5 <= float(c.get("perceptual_gamma", 2.2)) <= 4.0):
        errors.append("colour.perceptual_gamma must be between 0.5 and 4.0")

    o = cfg.get("output", {})
    if not isinstance(o.get("targets"), list) or not o["targets"]:
        errors.append("output.targets must be a non-empty list")
    else:
        for i, t in enumerate(o["targets"]):
            host = str(t.get("host", "") or "").strip()
            if t.get("enabled", True) and not host:
                errors.append(f"output.targets[{i}]: enabled but has no host")
            elif host and not _is_host(host):
                errors.append(
                    f"output.targets[{i}]: '{host}' is not a valid IP address or hostname")
            if not (1 <= int(t.get("port", 0)) <= 65535):
                errors.append(f"output.targets[{i}]: port out of range")
            if not (1 <= int(t.get("http_port", 80)) <= 65535):
                errors.append(f"output.targets[{i}]: http_port out of range")
    if float(o.get("presence_check_s", 10)) < 0:
        errors.append("output.presence_check_s must be 0 (off) or positive")
    if not (1 <= int(o.get("timeout_s", 2)) <= 254):
        errors.append("output.timeout_s must be between 1 and 254 (255 disables WLED fallback)")

    return errors


def load() -> dict[str, Any]:
    """Load config from disk, falling back to defaults."""
    if CONFIG_PATH.exists():
        try:
            cfg = merge_defaults(json.loads(CONFIG_PATH.read_text()))
            problems = validate(cfg)
            if problems:
                log.error("stored config is invalid, using it anyway: %s", "; ".join(problems))
            return cfg
        except Exception:
            log.exception("could not read %s, falling back to defaults", CONFIG_PATH)
    log.info("no config at %s, writing defaults", CONFIG_PATH)
    cfg = default_config()
    try:
        save(cfg)
    except Exception:
        log.exception("could not write default config")
    return cfg


def save(cfg: dict[str, Any]) -> None:
    """Atomic write, so a crash mid-save cannot truncate the config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(CONFIG_PATH)
