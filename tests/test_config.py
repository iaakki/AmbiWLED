"""Validation and persistence.  Persistence had a shipped bug; keep it covered."""
from __future__ import annotations

import json

import pytest

from ambiwled import config as config_mod


def errors_for(cfg, needle):
    return [e for e in config_mod.validate(cfg) if needle in e]


def test_defaults_are_valid(cfg):
    assert config_mod.validate(cfg) == []


def test_overlap_is_rejected(cfg):
    cfg["mapping"]["edges"][1]["pixel_start"] = 100   # tv_wall runs to 132
    assert errors_for(cfg, "overlaps")


def test_gap_is_rejected(cfg):
    cfg["mapping"]["edges"][0]["pixel_count"] = 120   # leaves 13 px unassigned
    problems = config_mod.validate(cfg)
    assert any("gap" in p for p in problems)
    assert any("total" in p for p in problems)


def test_total_mismatch_is_rejected(cfg):
    cfg["led"]["count"] = 500
    assert errors_for(cfg, "led.count is 500")


def test_edge_past_end_of_strip_is_rejected(cfg):
    cfg["mapping"]["edges"][-1]["pixel_count"] = 400
    assert errors_for(cfg, "runs past the end")


def test_duplicate_edge_names_rejected(cfg):
    cfg["mapping"]["edges"][1]["name"] = cfg["mapping"]["edges"][0]["name"]
    assert errors_for(cfg, "duplicate")


@pytest.mark.parametrize("host", ["192.0.2.", "192.0.2", "1.2.3.4.5", "999.1.1.1"])
def test_half_typed_addresses_are_rejected(cfg, host):
    """A partially typed IP must never reach the poller (this happened live)."""
    cfg["source"]["tv_ip"] = host
    assert errors_for(cfg, "tv_ip")


@pytest.mark.parametrize("host", ["", "   "])
def test_a_blank_tv_address_means_not_configured_yet(cfg, host):
    """A fresh install ships with no address. That is a starting state, not
    an error, and it must not stop the service from coming up."""
    cfg["source"]["tv_ip"] = host
    assert not errors_for(cfg, "tv_ip")


@pytest.mark.parametrize("host", ["192.0.2.10", "10.0.0.1", "wled.local", "tv", "::1"])
def test_real_hosts_are_accepted(cfg, host):
    cfg["source"]["tv_ip"] = host
    assert not errors_for(cfg, "tv_ip")


def test_output_host_is_validated(cfg):
    cfg["output"]["targets"][0]["host"] = "192.0.2."
    assert errors_for(cfg, "output.targets[0]")


def test_timeout_255_is_rejected(cfg):
    """255 means hold forever, which breaks WLED's fallback to its boot preset."""
    cfg["output"]["timeout_s"] = 255
    assert errors_for(cfg, "timeout_s")
    cfg["output"]["timeout_s"] = 2
    assert not errors_for(cfg, "timeout_s")


@pytest.mark.parametrize("value,needle", [
    (500, "output_fps"),               # past the 120 cap
    (0, "output_fps"),                 # below the poll rate
    (45, "whole multiple"),            # 45 fps out of a 20 fps source: not a multiple
])
def test_frame_settings_are_bounded(cfg, value, needle):
    cfg["frames"]["output_fps"] = value
    assert errors_for(cfg, needle)


def test_output_fps_equal_to_poll_rate_is_valid():
    """Equal means "no interpolation" - a valid, common choice, not an edge case."""
    cfg = config_mod.default_config()
    cfg["frames"]["output_fps"] = cfg["source"]["poll_hz"]
    assert config_mod.validate(cfg) == []


def test_output_fps_must_be_a_whole_multiple_of_the_poll_rate():
    cfg = config_mod.default_config()
    cfg["source"]["poll_hz"] = 20.0
    cfg["frames"]["output_fps"] = 90.0     # not a multiple of 20
    assert errors_for(cfg, "whole multiple")
    cfg["frames"]["output_fps"] = 80.0     # 4x
    assert config_mod.validate(cfg) == []


@pytest.mark.parametrize("field,value", [("poll_hz", 100), ("api_version", 3), ("endpoint", "raw")])
def test_source_settings_are_bounded(cfg, field, value):
    cfg["source"][field] = value
    assert config_mod.validate(cfg)


# -- persistence -------------------------------------------------------------

def test_save_load_roundtrip(tmp_config, cfg):
    cfg["source"]["tv_ip"] = "192.0.2.10"
    cfg["colour"]["saturation"] = 1.4
    config_mod.save(cfg)
    assert config_mod.load() == cfg


def test_load_writes_defaults_when_absent(tmp_config):
    assert not tmp_config.exists()
    loaded = config_mod.load()
    assert tmp_config.exists()
    assert loaded == config_mod.default_config()


def test_corrupt_config_falls_back_without_crashing(tmp_config):
    tmp_config.write_text("{not json at all")
    loaded = config_mod.load()
    assert loaded["led"]["count"] == 462   # defaults, no exception


def test_save_is_atomic(tmp_config, cfg):
    """A crash mid-save must not leave a truncated config behind."""
    config_mod.save(cfg)
    leftovers = list(tmp_config.parent.glob("*.tmp"))
    assert leftovers == []
    assert json.loads(tmp_config.read_text())["led"]["count"] == 462


def test_missing_keys_are_filled_from_defaults():
    """An old config file must not lose new settings."""
    merged = config_mod.merge_defaults({"led": {"count": 300}})
    assert merged["led"]["count"] == 300
    assert merged["frames"]["output_fps"] == 60.0
    assert merged["output"]["timeout_s"] == 2


def test_merge_replaces_lists_wholesale():
    """Edges must be replaced, not element-merged, or removing one is impossible."""
    base = config_mod.default_config()
    patch = {"mapping": {"edges": [
        {"name": "only", "pixel_start": 0, "pixel_count": 462, "source": "top"}]}}
    merged = config_mod.merge_defaults(config_mod._merge(base, patch))
    assert len(merged["mapping"]["edges"]) == 1
    assert config_mod.validate(merged) == []


# -- preset mode ---------------------------------------------------------------

def test_preset_mode_requires_a_slot(cfg):
    cfg["mode"] = "preset"
    cfg["preset_slot"] = 0
    assert errors_for(cfg, "preset_slot")


def test_preset_mode_with_a_slot_is_valid(cfg):
    cfg["mode"] = "preset"
    cfg["preset_slot"] = 5
    assert config_mod.validate(cfg) == []


def test_preset_slot_is_ignored_outside_preset_mode(cfg):
    cfg["mode"] = "ambilight"
    cfg["preset_slot"] = 0
    assert config_mod.validate(cfg) == []


@pytest.mark.parametrize("bad", [-1, 0, 251, 1000])
def test_preset_slot_out_of_range_is_rejected(cfg, bad):
    cfg["mode"] = "preset"
    cfg["preset_slot"] = bad
    assert errors_for(cfg, "preset_slot")


def test_merge_defaults_prunes_settings_removed_from_the_schema():
    """A setting dropped in a later version (dimming's old transition width,
    replaced by a curve exponent) must not sit in config.json forever."""
    merged = config_mod.merge_defaults(
        {"dimming": {"enabled": True, "transition_minutes": 45.0}})
    assert "transition_minutes" not in merged["dimming"]
    assert merged["dimming"]["enabled"] is True


def test_merge_defaults_prunes_unknown_top_level_keys():
    merged = config_mod.merge_defaults({"this_was_never_a_setting": 1})
    assert "this_was_never_a_setting" not in merged


def test_merge_defaults_never_touches_list_item_contents():
    """Edges and targets are user content, not schema; pruning must not
    reach inside them even if an item has a field the template lacks."""
    merged = config_mod.merge_defaults(
        {"mapping": {"edges": [{"name": "x", "some_future_field": 1}]}})
    assert merged["mapping"]["edges"] == [{"name": "x", "some_future_field": 1}]
