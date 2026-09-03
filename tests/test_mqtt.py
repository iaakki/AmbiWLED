"""MQTT bridge: discovery shape, command parsing, state payload, secrets."""
from __future__ import annotations

import asyncio
import json

import pytest

from ambiwled import config as config_mod
from ambiwled.mqtt import ENTITIES, MqttBridge, state_payload


def bridge(**mqtt):
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "192.0.2.30", **mqtt})
    return MqttBridge(cfg, lambda: {}, lambda patch: None)


# -- topics and discovery ----------------------------------------------------

def test_topics_follow_the_base():
    b = bridge(base_topic="ceiling")
    assert b.status_topic == "ceiling/status"
    assert b.state_topic == "ceiling/state"
    assert b.command_topic == "ceiling/set"


def test_discovery_covers_sensors_controls_and_identify_buttons():
    b = bridge()
    msgs = b.discovery_messages()
    assert len(msgs) == len(ENTITIES) + len(CONTROLS) + len(b.edge_names())
    topics = [t for t, _ in msgs]
    assert any("/switch/" in t for t in topics)
    assert any("/number/" in t for t in topics)
    assert any("/select/" in t for t in topics)
    assert any("/button/" in t for t in topics)
    assert all(t.startswith("homeassistant/") and t.endswith("/config") for t in topics)


def test_every_entity_is_uniquely_identified():
    """Without unique_id HA will not let you rename or customise an entity."""
    uids = [json.loads(p)["unique_id"] for _, p in bridge().discovery_messages()]
    assert len(uids) == len(set(uids))


def test_entities_share_one_device_so_they_group_in_ha():
    devices = [json.loads(p)["device"]["identifiers"] for _, p in bridge().discovery_messages()]
    assert all(d == devices[0] for d in devices)


def test_every_entity_has_availability_and_reads_the_one_state_topic():
    for topic, payload in bridge().discovery_messages():
        d = json.loads(payload)
        assert d["availability_topic"] == "ambiwled/status"
        if "/button/" in topic:
            continue          # buttons are write-only: no state to read
        assert d["state_topic"] == "ambiwled/state"
        assert "value_template" in d


def test_the_power_switch_is_commandable():
    switch = next(json.loads(p) for t, p in bridge().discovery_messages()
                  if t.endswith("/power/config"))
    assert switch["command_topic"] == "ambiwled/set/power"
    assert switch["payload_on"] == "ON" and switch["payload_off"] == "OFF"


def test_discovery_prefix_is_configurable():
    msgs = bridge(discovery_prefix="ha").discovery_messages()
    assert all(t.startswith("ha/") for t in msgs and [t for t, _ in msgs])


# -- commands ----------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("ON", {"mode": "ambilight"}),
    ("off", {"mode": "off"}),
    (' {"mode": "off"} ', {"mode": "off"}),
    ('{"power": "ON"}', {"mode": "ambilight"}),
    ('{"brightness": 0.4}', {"colour": {"brightness": 0.4}}),
    ('{"power":"OFF","brightness":0.5}', {"mode": "off", "colour": {"brightness": 0.5}}),
])
def test_commands_are_parsed(raw, expected):
    assert bridge().parse_command(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "garbage", "[1,2]", "{}", '{"mode":"effect"}', "null"])
def test_nonsense_commands_are_ignored(raw):
    assert bridge().parse_command(raw) is None


def test_brightness_is_clamped():
    assert bridge().parse_command('{"brightness": 99}')["colour"]["brightness"] == 2.0
    assert bridge().parse_command('{"brightness": -5}')["colour"]["brightness"] == 0.0


def test_a_command_goes_through_validation_not_straight_into_memory():
    """Commands arrive from outside; they must take the same validated path as
    the UI or a bad payload could put the service into an invalid state."""
    seen = []
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "192.0.2.30"})
    b = MqttBridge(cfg, lambda: {}, lambda patch: seen.append(patch))
    b.on_command(b.parse_command("OFF"))
    assert seen == [{"mode": "off"}]


# -- state payload -----------------------------------------------------------

def test_state_payload_flattens_what_ha_needs():
    metrics = {
        "mode": "ambilight", "tv_state": "streaming", "source_fps": 29.1,
        "output_fps": 60.0, "source_latency_ms": 13.0, "emitting": True,
        "ambilight_power": True, "failed_polls": 3,
        "targets": {"192.0.2.20": True},
        "controllers": {"192.0.2.20": {"fps": 81, "power_ma": 1227, "version": "16.0.1"}},
    }
    p = state_payload(metrics, config_mod.default_config())
    assert p["power"] == "ON"
    assert p["controller_online"] is True
    assert p["controller_fps"] == 81 and p["controller_power_ma"] == 1227
    assert p["emitting"] is True


def test_state_payload_reports_off():
    p = state_payload({"mode": "off", "emitting": False}, config_mod.default_config())
    assert p["power"] == "OFF" and p["emitting"] is False


def test_state_payload_survives_missing_metrics():
    p = state_payload({}, config_mod.default_config())
    assert p["power"] == "OFF" and p["controller_online"] is None
    json.dumps(p)          # must stay serialisable


def test_state_payload_marks_the_controller_down():
    p = state_payload({"targets": {"a": False}}, config_mod.default_config())
    assert p["controller_online"] is False


def test_every_discovery_template_reads_a_key_the_payload_provides():
    """A template pointing at a missing key gives HA 'unknown' forever."""
    payload = state_payload({"mode": "ambilight", "targets": {"a": True},
                             "controllers": {"a": {"fps": 1, "power_ma": 2}}},
                            config_mod.default_config())
    for topic, raw in bridge().discovery_messages():
        tpl = json.loads(raw).get("value_template")
        if not tpl:
            continue          # buttons have nothing to read
        key = tpl.split("value_json.")[1].split()[0].strip("}").strip()
        assert key in payload, f"{tpl} refers to a key the state payload does not publish"


# -- secrets -----------------------------------------------------------------

def test_the_broker_password_is_never_served():
    cfg = config_mod.default_config()
    cfg["mqtt"]["password"] = "hunter2"
    assert config_mod.redacted(cfg)["mqtt"]["password"] == config_mod.REDACTED
    assert cfg["mqtt"]["password"] == "hunter2", "redaction must not mutate the original"


def test_a_ui_round_trip_cannot_erase_the_password():
    cfg = config_mod.default_config()
    cfg["mqtt"]["password"] = "hunter2"
    came_back = config_mod.merge_defaults(config_mod.redacted(cfg))
    restored = config_mod.unredact(came_back, cfg)
    assert restored["mqtt"]["password"] == "hunter2"


def test_a_real_password_change_still_applies():
    cfg = config_mod.default_config()
    cfg["mqtt"]["password"] = "old"
    candidate = config_mod.merge_defaults({**cfg, "mqtt": {**cfg["mqtt"], "password": "new"}})
    assert config_mod.unredact(candidate, cfg)["mqtt"]["password"] == "new"


def test_disabled_mqtt_is_not_validated_for_host():
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": False, "host": ""})
    assert not [e for e in config_mod.validate(cfg) if "mqtt.host" in e]


def test_enabled_mqtt_requires_a_valid_broker():
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "192.0.2."})
    assert [e for e in config_mod.validate(cfg) if "mqtt.host" in e]


# -- runtime enable ----------------------------------------------------------

async def test_enabling_at_runtime_starts_the_client():
    """Regression: MQTT only connected if it was enabled before the process
    started, so turning it on in the UI appeared to do nothing at all."""
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": False, "host": "127.0.0.1", "port": 1})
    b = MqttBridge(cfg, lambda: {}, lambda p: None)
    await b.start()
    assert b._task is None, "disabled means no client"

    enabled = config_mod.default_config()
    enabled["mqtt"].update({"enabled": True, "host": "127.0.0.1", "port": 1})
    b.update(enabled)
    await asyncio.sleep(0.2)
    assert b._task is not None, "enabling it must connect without a restart"
    await b.stop()


async def test_disabling_at_runtime_stops_the_client():
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "127.0.0.1", "port": 1})
    b = MqttBridge(cfg, lambda: {}, lambda p: None)
    await b.start()
    assert b._task is not None
    off = config_mod.default_config()
    off["mqtt"].update({"enabled": False, "host": "127.0.0.1", "port": 1})
    b.update(off)
    await asyncio.sleep(0.3)
    assert b._task is None or b._task.cancelled() or b._task.done()
    await b.stop()


async def test_changing_the_broker_reconnects():
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "127.0.0.1", "port": 1})
    b = MqttBridge(cfg, lambda: {}, lambda p: None)
    await b.start()
    first = b._task
    moved = config_mod.default_config()
    moved["mqtt"].update({"enabled": True, "host": "127.0.0.2", "port": 1})
    b.update(moved)
    await asyncio.sleep(0.2)
    assert b._task is not first, "a new broker needs a new connection"
    await b.stop()


async def test_an_unrelated_change_does_not_reconnect():
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "127.0.0.1", "port": 1})
    b = MqttBridge(cfg, lambda: {}, lambda p: None)
    await b.start()
    first = b._task
    other = config_mod.default_config()
    other["mqtt"].update({"enabled": True, "host": "127.0.0.1", "port": 1,
                          "publish_interval_s": 5.0})
    b.update(other)
    await asyncio.sleep(0.2)
    assert b._task is first, "changing the publish interval must not drop the connection"
    await b.stop()


async def test_a_failed_connection_is_reported():
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "127.0.0.1", "port": 1})
    b = MqttBridge(cfg, lambda: {}, lambda p: None)
    await b.start()
    for _ in range(40):
        await asyncio.sleep(0.1)
        if b.last_error:
            break
    assert b.connected is False
    assert b.last_error, "a refused connection must surface, not sit silently"
    await b.stop()


# -- the wider control surface ----------------------------------------------

from ambiwled.mqtt import CONTROLS   # noqa: E402


def test_controls_are_all_commandable():
    msgs = {t: json.loads(p) for t, p in bridge().discovery_messages()}
    for object_id, component, _, _ in CONTROLS:
        topic = f"homeassistant/{component}/ambiwled/{object_id}/config"
        assert topic in msgs, f"{object_id} missing from discovery"
        assert msgs[topic]["command_topic"] == f"ambiwled/set/{object_id}"


def test_an_identify_button_exists_per_edge():
    b = bridge()
    names = b.edge_names()
    assert names, "the default config has edges"
    buttons = [json.loads(p) for t, p in b.discovery_messages() if "/button/" in t]
    assert len(buttons) == len(names)
    assert {x["payload_press"] for x in buttons} == set(names)
    assert all(x["command_topic"] == "ambiwled/set/identify" for x in buttons)


@pytest.mark.parametrize("suffix,payload,expected", [
    ("power", "ON", ("config", {"mode": "ambilight"})),
    ("power", "OFF", ("config", {"mode": "off"})),
    ("autodim", "ON", ("config", {"dimming": {"enabled": True}})),
    ("autodim", "OFF", ("config", {"dimming": {"enabled": False}})),
    ("brightness", "0.45", ("config", {"colour": {"brightness": 0.45}})),
    ("saturation", "1.6", ("config", {"colour": {"saturation": 1.6}})),
    ("black_floor", "12", ("config", {"colour": {"black_floor": 12.0}})),
    ("frame_mode", "passthrough", ("config", {"frames": {"mode": "passthrough"}})),
    ("identify", "Front", ("identify", "Front")),
])
def test_each_control_topic_routes(suffix, payload, expected):
    assert bridge().route(f"ambiwled/set/{suffix}", payload) == expected


@pytest.mark.parametrize("suffix,payload", [
    ("brightness", "not-a-number"), ("frame_mode", "magic"),
    ("identify", ""), ("unknown_thing", "1"), ("saturation", ""),
])
def test_bad_control_payloads_are_rejected(suffix, payload):
    assert bridge().route(f"ambiwled/set/{suffix}", payload) is None


def test_the_shared_set_topic_still_works():
    assert bridge().route("ambiwled/set", "OFF") == ("config", {"mode": "off"})


def test_control_values_are_clamped():
    assert bridge().route("ambiwled/set/brightness", "9")[1]["colour"]["brightness"] == 2.0
    assert bridge().route("ambiwled/set/saturation", "-1")[1]["colour"]["saturation"] == 0.0


def test_state_payload_feeds_every_control_template():
    """A control whose template reads a missing key shows as unknown in HA."""
    payload = state_payload({"mode": "ambilight", "dimming": {}}, config_mod.default_config())
    for _, raw in bridge().discovery_messages():
        d = json.loads(raw)
        tpl = d.get("value_template")
        if not tpl:
            continue
        key = tpl.split("value_json.")[1].split()[0].strip("}").strip()
        assert key in payload, f"{d['name']}: template reads {key}, not published"


def test_renaming_an_edge_marks_discovery_for_republish():
    b = bridge()
    b._discovery_dirty = False
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "192.0.2.30"})
    cfg["mapping"]["edges"][0]["name"] = "Renamed"
    b.update(cfg)
    assert b._discovery_dirty is True, "the identify buttons would be stale otherwise"


def test_identify_is_an_action_not_a_config_change():
    """Pressing identify must not write anything to the config."""
    actions = []
    cfg = config_mod.default_config()
    cfg["mqtt"].update({"enabled": True, "host": "192.0.2.30"})
    b = MqttBridge(cfg, lambda: {}, lambda p: pytest.fail("must not patch config"),
                   lambda kind, payload: actions.append((kind, payload)))
    kind, value = b.route("ambiwled/set/identify", "Front")
    assert kind == "identify"
    b.on_action(kind, {"edge": value})
    assert actions == [("identify", {"edge": "Front"})]
