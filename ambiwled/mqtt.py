"""MQTT bridge with Home Assistant discovery.

Home Assistant and Node-RED both speak MQTT, so publishing here gives both at
once without a custom integration.  The contract is deliberately small:

  <base>/status    availability, "online" / "offline" (offline is the LWT, so
                   HA marks the device unavailable if this process dies)
  <base>/state     one retained JSON document with everything HA reads
  <base>/set       commands, either a bare ON/OFF or a JSON object

Discovery configs are retained under the HA discovery prefix and point every
entity at the single state topic with a value_template, so adding an entity
never means another topic to keep in sync.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import aiomqtt

log = logging.getLogger(__name__)

# (object_id, component, friendly name, value_template, extra discovery keys)
ENTITIES: list[tuple[str, str, str, str, dict[str, Any]]] = [
    ("tv_state", "sensor", "TV state", "{{ value_json.tv_state }}",
     {"icon": "mdi:television"}),
    ("source_fps", "sensor", "Source FPS", "{{ value_json.source_fps }}",
     {"unit_of_measurement": "fps", "state_class": "measurement", "icon": "mdi:import"}),
    ("output_fps", "sensor", "Output FPS", "{{ value_json.output_fps }}",
     {"unit_of_measurement": "fps", "state_class": "measurement", "icon": "mdi:export"}),
    ("source_latency", "sensor", "TV latency", "{{ value_json.source_latency_ms }}",
     {"unit_of_measurement": "ms", "state_class": "measurement",
      "device_class": "duration", "icon": "mdi:timer-outline"}),
    ("controller_fps", "sensor", "Controller FPS", "{{ value_json.controller_fps }}",
     {"unit_of_measurement": "fps", "state_class": "measurement", "icon": "mdi:led-strip-variant"}),
    ("controller_power", "sensor", "Strip draw", "{{ value_json.controller_power_ma }}",
     {"unit_of_measurement": "mA", "state_class": "measurement",
      "device_class": "current", "icon": "mdi:flash"}),
    ("failed_polls", "sensor", "Failed polls", "{{ value_json.failed_polls }}",
     {"state_class": "total_increasing", "entity_category": "diagnostic"}),
    ("emitting", "binary_sensor", "Emitting", "{{ 'ON' if value_json.emitting else 'OFF' }}",
     {"icon": "mdi:broadcast"}),
    ("ambilight_power", "binary_sensor", "TV Ambilight",
     "{{ 'ON' if value_json.ambilight_power else 'OFF' }}", {"icon": "mdi:television-ambient-light"}),
    ("controller_online", "binary_sensor", "Controller reachable",
     "{{ 'ON' if value_json.controller_online else 'OFF' }}",
     {"device_class": "connectivity", "entity_category": "diagnostic"}),
    ("preset_applied", "binary_sensor", "Preset applied",
     "{{ 'ON' if value_json.preset_applied else 'OFF' }}",
     {"icon": "mdi:check-circle-outline", "entity_category": "diagnostic"}),
    ("preset_name", "sensor", "Active preset", "{{ value_json.preset_name }}",
     {"icon": "mdi:playlist-star", "entity_category": "diagnostic"}),
]


# (object_id, component, name, discovery extras) - each gets <base>/set/<id>
CONTROLS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("power", "switch", "Ambilight", {
        "value_template": "{{ value_json.power }}",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:television-ambient-light"}),
    # Deliberately minimal: Home Assistant's own WLED integration already
    # gives a full remote (power, brightness, colour, a named preset picker)
    # by talking to the controller directly. These two exist only for a setup
    # that has NOT added that integration - a single AmbiWled entry point.
    # Where both are present, prefer the native one; its presets have names,
    # this one is a bare slot number. See README: "Add WLED to Home Assistant
    # too".
    ("mode", "select", "Mode", {
        "value_template": "{{ value_json.mode }}",
        "options": ["ambilight", "preset", "off"],
        "icon": "mdi:led-strip-variant"}),
    ("preset_slot", "number", "Preset", {
        "value_template": "{{ value_json.preset_slot }}",
        "min": 0, "max": 250, "step": 1, "mode": "box",
        "icon": "mdi:playlist-star"}),
    ("brightness", "number", "Brightness", {
        "value_template": "{{ value_json.brightness }}",
        "min": 0, "max": 1, "step": 0.05, "mode": "slider",
        "icon": "mdi:brightness-6"}),
    ("saturation", "number", "Saturation", {
        "value_template": "{{ value_json.saturation }}",
        "min": 0, "max": 3, "step": 0.05, "mode": "slider",
        "icon": "mdi:palette"}),
    ("black_floor", "number", "Black floor", {
        "value_template": "{{ value_json.black_floor }}",
        "min": 0, "max": 255, "step": 1, "mode": "box",
        "entity_category": "config", "icon": "mdi:contrast-box"}),
    ("autodim", "switch", "Auto-dim by sun", {
        "value_template": "{{ value_json.autodim }}",
        "payload_on": "ON", "payload_off": "OFF",
        "icon": "mdi:weather-sunset-down"}),
]


def state_payload(metrics: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Flatten what HA needs into one document."""
    controllers = metrics.get("controllers") or {}
    first = next(iter(controllers.values()), {}) if controllers else {}
    targets = metrics.get("targets") or {}
    return {
        "mode": metrics.get("mode", "ambilight"),
        "power": "ON" if metrics.get("mode") == "ambilight" else "OFF",
        "tv_state": metrics.get("tv_state"),
        "source_fps": metrics.get("source_fps"),
        "output_fps": metrics.get("output_fps"),
        "source_latency_ms": metrics.get("source_latency_ms"),
        "emitting": bool(metrics.get("emitting")),
        "ambilight_power": metrics.get("ambilight_power"),
        "failed_polls": metrics.get("failed_polls"),
        "controller_online": any(v is not False for v in targets.values()) if targets else None,
        "controller_fps": first.get("fps"),
        "controller_power_ma": first.get("power_ma"),
        "controller_version": first.get("version"),
        "brightness": cfg.get("colour", {}).get("brightness"),
        "preset_slot": cfg.get("preset_slot") or 0,
        "preset_applied": bool(metrics.get("preset_applied")),
        "preset_name": metrics.get("preset_name") or "",
        "saturation": cfg.get("colour", {}).get("saturation"),
        "black_floor": cfg.get("colour", {}).get("black_floor"),
        "autodim": "ON" if cfg.get("dimming", {}).get("enabled") else "OFF",
        "sunrise": (metrics.get("dimming") or {}).get("sunrise"),
        "sunset": (metrics.get("dimming") or {}).get("sunset"),
        "dim_level": (metrics.get("dimming") or {}).get("level"),
    }


class MqttBridge:
    def __init__(self, cfg: dict[str, Any], metrics: Callable[[], dict[str, Any]],
                 on_command: Callable[[dict[str, Any]], None],
                 on_action: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.metrics = metrics
        self.on_command = on_command
        self.on_action = on_action
        self._discovery_dirty = False
        self._edge_names: list[str] = []
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_error: str | None = None
        self.publishes = 0
        self.commands = 0
        self.update(cfg)

    def _connection_key(self) -> tuple:
        """The settings that require a reconnect if they change."""
        return (self.enabled, self.host, self.port, self.username, self.password,
                self.base, self.prefix)

    def edge_names(self) -> list[str]:
        return [str(e.get("name")) for e in self.cfg.get("mapping", {}).get("edges", [])
                if e.get("name")]

    def update(self, cfg: dict[str, Any]) -> None:
        previous = self._connection_key() if hasattr(self, "enabled") else None
        self.cfg = cfg
        names = self.edge_names()
        if names != self._edge_names:
            # Identify buttons are per edge, so renaming one changes the entities.
            self._edge_names = names
            self._discovery_dirty = True
        m = cfg.get("mqtt", {})
        self.enabled = bool(m.get("enabled", False))
        self.host = str(m.get("host", ""))
        self.port = int(m.get("port", 1883))
        self.username = m.get("username") or None
        self.password = m.get("password") or None
        self.base = str(m.get("base_topic", "ambiwled")).strip("/")
        self.prefix = str(m.get("discovery_prefix", "homeassistant")).strip("/")
        self.device_name = str(m.get("device_name", "AmbiWled"))
        self.interval = max(float(m.get("publish_interval_s", 2.0)), 0.5)
        self.node_id = self.base.replace("/", "_") or "ambiwled"

        # Enabling MQTT from the UI must connect straight away rather than
        # waiting for a restart, and changing the broker must reconnect to it.
        if previous is not None and previous != self._connection_key():
            self._reconnect()

    def _reconnect(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return          # no loop yet: start() will pick the settings up
        asyncio.create_task(self._restart(), name="mqtt-restart")

    async def _restart(self) -> None:
        await self.stop()
        self.publishes = 0
        self.last_error = None
        await self.start()

    # -- topics ----------------------------------------------------------

    @property
    def status_topic(self) -> str:
        return f"{self.base}/status"

    @property
    def state_topic(self) -> str:
        return f"{self.base}/state"

    @property
    def command_topic(self) -> str:
        return f"{self.base}/set"

    def device_block(self) -> dict[str, Any]:
        return {
            "identifiers": [self.node_id],
            "name": self.device_name,
            "manufacturer": "AmbiWled",
            "model": "Ambilight to WLED bridge",
        }

    def discovery_messages(self) -> list[tuple[str, str]]:
        """(topic, payload) pairs, retained, so HA rebuilds entities on restart."""
        out: list[tuple[str, str]] = []
        common = {
            "state_topic": self.state_topic,
            "availability_topic": self.status_topic,
            "device": self.device_block(),
        }

        for object_id, component, name, extra in CONTROLS:
            payload = {
                **common,
                "name": name,
                "unique_id": f"{self.node_id}_{object_id}",
                "object_id": f"{self.node_id}_{object_id}",
                "command_topic": f"{self.command_topic}/{object_id}",
                **extra,
            }
            out.append(
                (f"{self.prefix}/{component}/{self.node_id}/{object_id}/config",
                 json.dumps(payload)))

        # One button per ceiling run: the fastest way to find a run from HA
        # or a Node-RED flow without opening the AmbiWled UI.
        for name in self.edge_names():
            object_id = "identify_" + "".join(
                c if c.isalnum() else "_" for c in name.lower())
            payload = {
                "name": f"Identify {name}",
                "unique_id": f"{self.node_id}_{object_id}",
                "object_id": f"{self.node_id}_{object_id}",
                "availability_topic": self.status_topic,
                "device": self.device_block(),
                "command_topic": f"{self.command_topic}/identify",
                "payload_press": name,
                "entity_category": "config",
                "icon": "mdi:map-marker-radius",
            }
            out.append(
                (f"{self.prefix}/button/{self.node_id}/{object_id}/config",
                 json.dumps(payload)))

        for object_id, component, name, template, extra in ENTITIES:
            payload = {
                **common,
                "name": name,
                "unique_id": f"{self.node_id}_{object_id}",
                "object_id": f"{self.node_id}_{object_id}",
                "value_template": template,
                **extra,
            }
            out.append(
                (f"{self.prefix}/{component}/{self.node_id}/{object_id}/config",
                 json.dumps(payload)))
        return out

    # -- command handling ------------------------------------------------

    def route(self, topic: str, raw: str) -> tuple[str, Any] | None:
        """Map a command topic and payload to an action.

        Returns ("config", patch) for anything that is a setting, or
        ("identify", name) for the per-edge buttons.
        """
        suffix = topic.rsplit("/", 1)[-1] if "/" in topic else ""
        text = raw.strip()
        if suffix == "identify":
            return ("identify", text) if text else None
        if suffix in ("set", "") or topic == self.command_topic:
            patch = self.parse_command(text)
            return ("config", patch) if patch else None

        try:
            if suffix == "power":
                return ("config", {"mode": "ambilight" if text.upper() == "ON" else "off"})
            if suffix == "mode":
                if text not in ("ambilight", "preset", "off"):
                    return None
                return ("config", {"mode": text})
            if suffix == "preset_slot":
                return ("config", {"preset_slot": max(0, min(int(float(text)), 250))})
            if suffix == "autodim":
                return ("config", {"dimming": {"enabled": text.upper() == "ON"}})
            if suffix == "brightness":
                return ("config", {"colour": {"brightness": max(0.0, min(float(text), 2.0))}})
            if suffix == "saturation":
                return ("config", {"colour": {"saturation": max(0.0, min(float(text), 3.0))}})
            if suffix == "black_floor":
                return ("config", {"colour": {"black_floor": max(0.0, min(float(text), 255.0))}})
        except (TypeError, ValueError):
            return None
        return None

    def parse_command(self, raw: str) -> dict[str, Any] | None:
        """Accept a bare ON/OFF (what an HA switch sends) or a JSON object."""
        text = raw.strip()
        if not text:
            return None
        upper = text.upper()
        if upper in ("ON", "OFF"):
            return {"mode": "ambilight" if upper == "ON" else "off"}
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        patch: dict[str, Any] = {}
        if "mode" in data and data["mode"] in ("ambilight", "off"):
            patch["mode"] = data["mode"]
        elif "power" in data:
            patch["mode"] = "ambilight" if str(data["power"]).upper() == "ON" else "off"
        if "brightness" in data:
            try:
                patch.setdefault("colour", {})["brightness"] = max(
                    0.0, min(float(data["brightness"]), 2.0))
            except (TypeError, ValueError):
                pass
        return patch or None

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self.enabled and self.host:
            self._task = asyncio.create_task(self._run(), name="mqtt")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.connected = False

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.host, port=self.port,
                    username=self.username, password=self.password,
                    identifier=f"{self.node_id}-bridge",
                    will=aiomqtt.Will(self.status_topic, b"offline", qos=1, retain=True),
                ) as client:
                    self.connected = True
                    self.last_error = None
                    backoff = 2.0
                    log.info("MQTT connected to %s:%s as %s", self.host, self.port, self.node_id)

                    await client.publish(self.status_topic, b"online", qos=1, retain=True)
                    for topic, payload in self.discovery_messages():
                        await client.publish(topic, payload.encode(), qos=1, retain=True)
                    await client.subscribe(self.command_topic, qos=1)
                    await client.subscribe(f"{self.command_topic}/+", qos=1)

                    await asyncio.gather(self._publish_loop(client), self._listen(client))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                log.warning("MQTT connection lost (%s); retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _publish_loop(self, client: aiomqtt.Client) -> None:
        while True:
            if self._discovery_dirty:
                self._discovery_dirty = False
                for topic, payload in self.discovery_messages():
                    await client.publish(topic, payload.encode(), qos=1, retain=True)
                log.info("republished MQTT discovery after a config change")
            payload = json.dumps(state_payload(self.metrics(), self.cfg))
            await client.publish(self.state_topic, payload.encode(), qos=0, retain=True)
            self.publishes += 1
            await asyncio.sleep(self.interval)

    async def _listen(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            raw = message.payload.decode(errors="replace") if message.payload else ""
            routed = self.route(str(message.topic), raw)
            if routed is None:
                log.warning("ignoring unrecognised MQTT command on %s: %r",
                            message.topic, raw[:60])
                continue
            kind, value = routed
            self.commands += 1
            log.info("MQTT %s: %s", kind, value)
            try:
                if kind == "config":
                    self.on_command(value)
                elif kind == "identify" and self.on_action:
                    self.on_action("identify", {"edge": value})
            except Exception:
                log.exception("applying MQTT command failed")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "connected": self.connected,
            "host": self.host,
            "base_topic": self.base,
            "publishes": self.publishes,
            "commands": self.commands,
            "last_error": self.last_error,
        }


# CONNACK / MQTT v5 reason codes that mean the broker was reached and said no
# to the credentials, as opposed to nothing answering at all.
_AUTH_REASON_CODES = {4, 5, 134, 135}  # bad user/pass, not authorized (v3.1.1 + v5)


async def test_connection(host: str, port: int, username: str | None,
                           password: str | None, timeout: float = 5.0) -> dict[str, Any]:
    """Try a real connection, so the wizard can say *why* it failed.

    A broker that never answers (wrong host, firewalled, not running) and one
    that answers but rejects the login look identical from the outside unless
    something actually attempts the handshake - so this is a real, short-lived
    connection, not a socket probe.
    """
    try:
        async with aiomqtt.Client(
            hostname=host, port=port, username=username or None,
            password=password or None, identifier="ambiwled-test",
            timeout=timeout,
        ):
            pass
    except aiomqtt.MqttCodeError as exc:
        rc = exc.rc
        code = rc.value if hasattr(rc, "value") else rc
        if code in _AUTH_REASON_CODES:
            return {"ok": False, "reason": "auth"}
        return {"ok": False, "reason": "unreachable", "detail": str(exc)}
    except Exception as exc:
        return {"ok": False, "reason": "unreachable", "detail": str(exc)}
    return {"ok": True}
