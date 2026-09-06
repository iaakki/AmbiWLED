"""Browser-driven regressions for bugs that only show up in the real UI -
found by hand this session, pinned down here so they can't come back
silently. See conftest.py's `browser` fixture: these skip themselves,
rather than fail, on a machine with no Firefox/geckodriver.

Selenium's calls are synchronous. The `running` fixture's Server lives on
this same test's event loop, and the browser talks to it over real HTTP/WS -
so blocking that loop with a bare `driver.click()` would starve the very
server the browser is waiting on. Every test's actual browser work happens
in a worker thread instead (`_in_thread`), keeping the loop free to serve
requests the whole time; only cheap assertions happen back on the loop.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

selenium = pytest.importorskip("selenium")
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.common.keys import Keys  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402


async def _in_thread(fn, *args):
    return await asyncio.to_thread(fn, *args)


def _wait_booted(browser, timeout=5):
    """The page has received `hello` over the websocket and rendered it."""
    WebDriverWait(browser, timeout).until(
        lambda d: d.find_element(By.ID, "status-line").text not in ("", "…")
    )


def _click_tab(browser, label):
    for t in browser.find_elements(By.CSS_SELECTOR, "#tab-bar .tab-btn"):
        if t.text.strip() == label:
            t.click()
            return
    raise AssertionError(f"tab {label!r} not found in the tab bar")


def _open_advanced_tab(browser, label):
    browser.find_element(By.ID, "open-advanced").click()
    _click_tab(browser, label)
    time.sleep(0.3)


# -- wiring order: no way to describe a real strip's physical order --------

def _reorder_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    _open_advanced_tab(browser, "Mapping")
    cards = browser.find_elements(By.CSS_SELECTOR, ".edge-card")
    down = cards[0].find_elements(By.CSS_SELECTOR, ".reorder button")[1]  # front's down-arrow
    down.click()
    time.sleep(1.0)  # past the debounce
    return json.loads(browser.execute_script(
        "return JSON.stringify(get('mapping.edges').map(e => [e.name, e.pixel_start, e.pixel_count]))"
    ))


async def test_reordering_edges_updates_pixel_start(running, browser):
    """Regression: presentSlots()/repack() used to always force
    front/left/right/back order regardless of cfg.mapping.edges, so there
    was no way to describe a strip wired in a different physical order - a
    real installation (Left first at pixel 0, Front second at 98) simply
    could not be represented. Moving front below left must swap which one
    starts at pixel 0."""
    _, bridge, base, session, _ = running
    edges = await _in_thread(_reorder_body, browser, base)
    names = [e[0] for e in edges]
    assert names[0] == "left" and names[1] == "front"
    by_name = {name: (start, count) for name, start, count in edges}
    assert by_name["left"][0] == 0
    assert by_name["front"][0] == by_name["left"][1]


# -- auto-dim chart: hour labels used to pile up on every redraw -----------

def _dim_chart_ticks_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    _open_advanced_tab(browser, "Auto-dim")
    before = len(browser.find_elements(By.CSS_SELECTOR, "#curve-ticks span"))
    containers_before = len(browser.find_elements(By.ID, "curve-ticks"))
    browser.execute_script("for (let i = 0; i < 40; i++) window.drawDimChart();")
    after = len(browser.find_elements(By.CSS_SELECTOR, "#curve-ticks span"))
    containers_after = len(browser.find_elements(By.ID, "curve-ticks"))
    return before, after, containers_before, containers_after


async def test_dim_chart_ticks_do_not_duplicate_on_redraw(running, browser):
    """Regression: drawDimChart() inserted a brand-new hour-labels row after
    the chart via wrap.after(ticks) on every single call, instead of
    replacing the previous one. Harmless on a page load (one call, one
    row) - but the day/night sliders call this on every pointermove, so
    dragging either one spammed a new row per pixel of drag."""
    _, bridge, base, session, _ = running
    before, after, c_before, c_after = await _in_thread(_dim_chart_ticks_body, browser, base)
    assert before == 5 and after == 5, "always exactly the 0/6/12/18/24h labels, not one set per redraw"
    assert c_before == 1 and c_after == 1, "one #curve-ticks element, not one per redraw"


# -- brightness slider: listeners used to pile up on every render ----------

def _slider_listener_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    browser.execute_script("""
      window.__fetchCount = 0;
      const orig = window.fetch;
      window.fetch = function(...args) { window.__fetchCount++; return orig.apply(this, args); };
    """)
    # Stands in for the many renderSimple() calls a real session accumulates
    # (every toggle, every save, every navigation back from Advanced).
    browser.execute_script("for (let i = 0; i < 15; i++) window.renderSimple();")
    browser.execute_script("""
      const track = document.getElementById('bright-slider');
      const evt = new PointerEvent('pointerdown',
        {clientX: track.getBoundingClientRect().left + 50, bubbles: true, pointerId: 1});
      track.dispatchEvent(evt);
    """)
    time.sleep(0.6)
    return browser.execute_script("return window.__fetchCount")


async def test_brightness_slider_does_not_accumulate_duplicate_listeners(running, browser):
    """Regression: wireSlider() attached a fresh pointerdown/pointermove
    listener pair every time it ran - fine for the Advanced pages' sliders
    (rebuilt fresh on every render anyway), wrong for #bright-slider on the
    Simple page, which is static markup re-wired on every renderSimple()
    call. One real drag used to fire one PUT per accumulated listener."""
    _, bridge, base, session, _ = running
    fetch_count = await _in_thread(_slider_listener_body, browser, base)
    assert fetch_count == 1, "one drag interaction must send exactly one request, not one per past render"


# -- config PUT responses: out-of-order arrival used to win silently -------

def _stale_response_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    # Start from a known "on" state, before the fetch mock goes in, so the
    # two toggles below are unambiguously disable-then-enable rather than
    # depending on dimming.enabled's default.
    browser.execute_script("window.set('dimming.enabled', true, true)")
    time.sleep(0.5)

    # Wrap the real fetch (still hits the real server below) so the FIRST
    # call's response is deliberately delayed past the SECOND call's -
    # exactly what unlucky network timing can do to two quick toggles.
    browser.execute_script("""
      const real = window.fetch;
      let call = 0;
      window.fetch = function(url, opts) {
        const delay = call === 0 ? 400 : 0;
        call++;
        return new Promise((resolve, reject) => {
          real(url, opts).then((r) => setTimeout(() => resolve(r), delay), reject);
        });
      };
    """)
    toggle = browser.find_element(By.ID, "autodim-toggle")
    toggle.click()  # disable: request #0, response artificially held back 400ms
    toggle.click()  # enable: request #1, resolves immediately
    time.sleep(0.8)
    return browser.execute_script("return get('dimming.enabled')")


async def test_stale_config_response_cannot_undo_a_newer_change(running, browser):
    """Regression: pushConfig() applied `cfg = data.config` on every response
    unconditionally, in whatever order they happened to arrive - not the
    order the requests were sent. Toggling off then straight back on fires
    two independent in-flight requests with no ordering guarantee; a late
    response to the first could land after the response to the second and
    silently revert the user's actual last action, with nothing forcing a
    re-render to reveal it."""
    _, bridge, base, session, _ = running
    enabled = await _in_thread(_stale_response_body, browser, base)
    assert enabled is True, "the user's last click (enable) must win, however the responses arrive"


# -- MQTT broker field: "host:port" used to go straight into mqtt.host -----

def _mqtt_split_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    _open_advanced_tab(browser, "Integrations")
    browser.find_element(By.CSS_SELECTOR, "#page-body .toggle-switch").click()  # Publish to MQTT
    time.sleep(0.3)
    host = browser.find_element(By.ID, "mqtt-host")
    host.clear()
    host.send_keys("192.0.2.30:1884")
    host.send_keys(Keys.TAB)
    time.sleep(0.6)
    return json.loads(browser.execute_script(
        "return JSON.stringify({host: get('mqtt.host'), port: get('mqtt.port')})"
    ))


async def test_mqtt_broker_field_splits_host_and_port(running, browser):
    """Regression: the broker-address field displays and accepts
    "host:port" (its own placeholder says so), but saved the whole string
    into mqtt.host verbatim - mqtt.port, a separate schema field, never
    updated, so the real MQTT client would try to resolve a hostname that
    still had a colon and a port number stuck to the end of it."""
    _, bridge, base, session, _ = running
    data = await _in_thread(_mqtt_split_body, browser, base)
    assert data["host"] == "192.0.2.30"
    assert data["port"] == 1884


# -- wizard: location step set up auto-dim but never turned it on ----------

def _wizard_location_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    before = browser.execute_script("return get('dimming.enabled')")
    browser.execute_script("window.startWizard()")
    time.sleep(0.4)
    next_btn = browser.find_element(By.ID, "wiz-next")
    # Find TV, find lights, which sides (all four stay on by default), then
    # one "next side" per side to walk through the count step - seven clicks
    # land on "Where you are", the step that sets location and night level.
    for _ in range(7):
        next_btn.click()
        time.sleep(0.2)
    time.sleep(0.3)
    after = browser.execute_script("return get('dimming.enabled')")
    return before, after


async def test_wizard_reaching_the_location_step_turns_autodim_on(running, browser):
    """Regression: "Where you are" sets location and a night level, but
    nothing in the wizard ever flipped dimming.enabled - finishing setup
    used to leave auto-dim fully configured and silently off until someone
    separately found the toggle in Advanced."""
    _, bridge, base, session, _ = running
    before, after = await _in_thread(_wizard_location_body, browser, base)
    assert before is False
    assert after is True


# -- tab layout: Frame generation folded into Output, Mode moved to Home ---

def _tab_layout_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    browser.find_element(By.ID, "open-advanced").click()
    tabs = [t.text.strip() for t in browser.find_elements(By.CSS_SELECTOR, "#tab-bar .tab-btn")]

    _click_tab(browser, "Output")
    time.sleep(0.3)
    output_text = browser.find_element(By.ID, "page-body").text

    _click_tab(browser, "Colour")
    time.sleep(0.3)
    colour_text = browser.find_element(By.ID, "page-body").text

    _click_tab(browser, "Home")
    time.sleep(0.3)
    home_text = browser.find_element(By.ID, "page-body").text

    return tabs, output_text, colour_text, home_text


async def test_frame_generation_lives_under_output_not_its_own_tab(running, browser):
    """Regression: Frame generation used to be its own top-level tab. It
    only ever controls how the strip is driven (output_fps), so it belongs
    alongside the WLED controller settings on Output, not as a whole tab of
    its own."""
    _, bridge, base, session, _ = running
    tabs, output_text, colour_text, home_text = await _in_thread(_tab_layout_body, browser, base)
    assert "Frame generation" not in tabs
    assert "Frame generation" in output_text
    assert "Frames sent to the strip" in output_text


async def test_mode_switch_lives_on_home_not_colour(running, browser):
    """Regression: choosing ambilight/preset/off used to live at the top of
    the Colour tab, an odd home for something that is not a colour setting
    at all. It belongs on Home, appended after the live summary."""
    _, bridge, base, session, _ = running
    tabs, output_text, colour_text, home_text = await _in_thread(_tab_layout_body, browser, base)
    assert "Follow the TV" not in colour_text
    assert "Hold a WLED preset" not in colour_text
    assert "Follow the TV" in home_text
    assert "Hold a WLED preset" in home_text


# -- wizard: TV pairing is offered but must not block finishing setup ------

def _wizard_pairing_step_body(browser, base):
    browser.get(base)
    _wait_booted(browser)
    browser.execute_script("window.startWizard()")
    time.sleep(0.4)
    next_btn = browser.find_element(By.ID, "wiz-next")
    # JS-dispatched, not a native Selenium click: the debounced "Saved"
    # toast from an earlier step's autosave can still be fading out and
    # physically overlap the button at this click rate, which a real
    # slower-clicking user would never hit.
    def click_next():
        browser.execute_script("document.getElementById('wiz-next').click()")
    # Same seven clicks as the location-step test reach "Where you are";
    # two more (Where you are -> Home Assistant -> Accurate detection,
    # HA left off by default so it falls straight through) land here.
    for _ in range(9):
        click_next()
        time.sleep(0.2)
    step_text = browser.find_element(By.ID, "wiz-body").text
    wiz_open_at_pairing = not browser.find_element(By.ID, "wizard").get_attribute("hidden")

    click_next()  # -> Done
    time.sleep(0.2)
    done_text = browser.find_element(By.ID, "wiz-body").text

    click_next()  # finish
    time.sleep(0.3)
    wizard_hidden_after = browser.find_element(By.ID, "wizard").get_attribute("hidden")
    return step_text, wiz_open_at_pairing, done_text, wizard_hidden_after


async def test_wizard_offers_pairing_but_finishes_without_it(running, browser):
    """Pairing is recommended, not required: a fresh install has no way to
    know whether this TV even supports /powerstate - the wizard must reach
    "All done" and actually close on Next alone, with no pairing attempted."""
    _, bridge, base, session, _ = running
    step_text, was_open, done_text, hidden_after = await _in_thread(_wizard_pairing_step_body, browser, base)
    assert was_open
    assert "Pair for accurate detection" in step_text
    assert "That's it" in done_text or "you're set" in done_text
    assert hidden_after == "true"
