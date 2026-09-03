"""Sun-driven dimming.  Times are computed locally, so they are testable."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from ambiwled.daylight import DimmingSchedule, polar_state, sun_times

HELSINKI = (60.17, 24.94)
UTSJOKI = (69.9, 27.0)          # above the Arctic circle


def utc(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# -- the sunrise equation ----------------------------------------------------

@pytest.mark.parametrize("day,rise,set_,tol", [
    (date(2026, 12, 21), "07:24", "13:13", 4),      # midwinter, ~09:24-15:13 local
    (date(2026, 6, 21), "00:55", "19:51", 4),       # midsummer, ~03:55-22:51 local
    (date(2026, 3, 20), "04:24", "16:38", 8),       # equinox
])
def test_helsinki_times_match_reality(day, rise, set_, tol):
    got = sun_times(*HELSINKI, day)
    assert got is not None
    for actual, expected in zip(got, (rise, set_)):
        want_h, want_m = (int(x) for x in expected.split(":"))
        delta = abs((actual.hour * 60 + actual.minute) - (want_h * 60 + want_m))
        assert delta <= tol, f"{actual:%H:%M} vs {expected}"


def test_day_is_longer_in_summer_than_winter():
    summer = sun_times(*HELSINKI, date(2026, 6, 21))
    winter = sun_times(*HELSINKI, date(2026, 12, 21))
    assert (summer[1] - summer[0]) > (winter[1] - winter[0]) + timedelta(hours=10)


def test_sunset_moves_across_the_year():
    """The whole point: a fixed clock schedule cannot work at this latitude."""
    sets = [sun_times(*HELSINKI, d)[1]
            for d in (date(2026, 12, 21), date(2026, 3, 20),
                      date(2026, 6, 21), date(2026, 9, 22))]
    minutes = [s.hour * 60 + s.minute for s in sets]
    assert max(minutes) - min(minutes) > 6 * 60     # over six hours of movement


def test_polar_night_and_day_are_reported_not_crashed():
    assert sun_times(*UTSJOKI, date(2026, 12, 21)) is None
    assert polar_state(*UTSJOKI, date(2026, 12, 21)) == "polar_night"
    assert sun_times(*UTSJOKI, date(2026, 6, 21)) is None
    assert polar_state(*UTSJOKI, date(2026, 6, 21)) == "polar_day"


def test_southern_hemisphere_is_inverted():
    sydney = (-33.87, 151.21)
    june = sun_times(*sydney, date(2026, 6, 21))
    december = sun_times(*sydney, date(2026, 12, 21))
    assert (december[1] - december[0]) > (june[1] - june[0])


# -- the schedule ------------------------------------------------------------

def schedule(**overrides):
    cfg = {"dimming": {"enabled": True, "latitude": HELSINKI[0], "longitude": HELSINKI[1],
                       "day_level": 1.0, "night_level": 0.4,
                       "sunrise_offset_minutes": 0.0, "sunset_offset_minutes": 0.0}}
    cfg["dimming"].update(overrides)
    return DimmingSchedule(cfg)


def solar_noon(day: date) -> datetime:
    """Sunrise and sunset are symmetric around transit by construction, so
    their midpoint *is* solar noon — no separate computation needed."""
    sunrise, sunset = sun_times(*HELSINKI, day)
    return sunrise + (sunset - sunrise) / 2


def test_disabled_schedule_never_dims():
    s = schedule(enabled=False)
    assert s.level(utc(2026, 12, 21, 23)) == 1.0


def test_solar_noon_is_the_peak_and_midnight_is_the_minimum():
    s = schedule()
    assert s.level(solar_noon(date(2026, 6, 21))) == pytest.approx(1.0, abs=1e-6)
    assert s.level(utc(2026, 12, 21, 23)) == pytest.approx(0.4)


def test_night_is_flat_at_the_minimum_not_still_fading():
    """The whole point of the change: 2 a.m. must read exactly the minimum,
    not a lingering tail of the previous evening's ramp."""
    s = schedule()
    day = date(2026, 12, 21)
    _, sunset = sun_times(*HELSINKI, day)
    for hours in (1, 2, 4, 6):
        assert s.level(sunset + timedelta(hours=hours)) == pytest.approx(0.4, abs=1e-6)


def test_the_curve_touches_the_minimum_exactly_at_sunrise_and_sunset():
    s = schedule()
    sunrise, sunset = sun_times(*HELSINKI, date(2026, 12, 21))
    assert s.level(sunrise) == pytest.approx(0.4, abs=1e-6)
    assert s.level(sunset) == pytest.approx(0.4, abs=1e-6)
    assert s.level(sunrise - timedelta(seconds=1)) == pytest.approx(0.4, abs=1e-6)
    assert s.level(sunset + timedelta(seconds=1)) == pytest.approx(0.4, abs=1e-6)


def test_the_curve_is_a_parabola_not_a_plateau():
    """At the exponent default this is a literal `4s(1-s)`: exactly 0.75 of
    the way to peak at the quarter- and three-quarter points of the day."""
    s = schedule()
    sunrise, sunset = sun_times(*HELSINKI, date(2026, 6, 21))
    span = sunset - sunrise
    quarter = s.level(sunrise + span / 4)
    three_quarter = s.level(sunrise + span * 3 / 4)
    expected = 0.4 + (1.0 - 0.4) * 0.75
    assert quarter == pytest.approx(expected, abs=0.01)
    assert three_quarter == pytest.approx(expected, abs=0.01)


def test_rises_to_noon_then_falls_to_sunset_monotonically():
    s = schedule()
    sunrise, sunset = sun_times(*HELSINKI, date(2026, 12, 21))
    noon = sunrise + (sunset - sunrise) / 2
    morning = [s.level(sunrise + (noon - sunrise) * f) for f in (0, 0.25, 0.5, 0.75, 1.0)]
    afternoon = [s.level(noon + (sunset - noon) * f) for f in (0, 0.25, 0.5, 0.75, 1.0)]
    assert morning == sorted(morning)
    assert afternoon == sorted(afternoon, reverse=True)


def test_sunrise_brings_it_back_up():
    s = schedule()
    sunrise, _ = sun_times(*HELSINKI, date(2026, 12, 21))
    assert s.level(sunrise - timedelta(minutes=40)) < s.level(sunrise + timedelta(minutes=40))


def test_offsets_shift_the_window():
    early = schedule(sunset_offset_minutes=-120)
    plain = schedule()
    _, sunset = sun_times(*HELSINKI, date(2026, 12, 21))
    probe = sunset - timedelta(minutes=90)
    assert early.level(probe) < plain.level(probe)


def test_curve_exponent_narrows_or_broadens_the_peak():
    sharp = schedule(curve_exponent=3.0)
    flat = schedule(curve_exponent=0.5)
    plain = schedule(curve_exponent=1.0)
    sunrise, sunset = sun_times(*HELSINKI, date(2026, 6, 21))
    probe = sunrise + (sunset - sunrise) / 4          # quarter into the day

    # All three still meet the same anchors: minimum at the edges, peak at noon.
    for s in (sharp, flat, plain):
        assert s.level(sunrise) == pytest.approx(0.4, abs=1e-6)
        noon = sunrise + (sunset - sunrise) / 2
        assert s.level(noon) == pytest.approx(1.0, abs=1e-6)

    # Between the anchors, exponent > 1 pulls the curve down; < 1 pushes it up.
    assert sharp.level(probe) < plain.level(probe) < flat.level(probe)


def test_curve_exponent_is_never_zero_or_negative():
    """A zero or negative exponent would make the dome ill-defined; the
    schedule must clamp rather than propagate NaN into the light output."""
    s = schedule(curve_exponent=-5.0)
    level = s.level(solar_noon(date(2026, 6, 21)))
    assert level == level          # not NaN
    assert 0.0 <= level <= 1.0


def test_polar_night_holds_the_night_level():
    s = schedule(latitude=UTSJOKI[0], longitude=UTSJOKI[1])
    assert s.level(utc(2026, 12, 21, 12)) == pytest.approx(0.4)


def test_polar_day_holds_the_day_level():
    s = schedule(latitude=UTSJOKI[0], longitude=UTSJOKI[1])
    assert s.level(utc(2026, 6, 21, 0)) == pytest.approx(1.0)


def test_describe_gives_the_ui_what_it_needs():
    s = schedule()
    d = s.describe(utc(2026, 12, 21, 12))
    assert d["enabled"] is True
    assert d["sunrise"] and d["sunset"]
    assert 0.0 <= d["level"] <= 1.0
    assert d["polar"] is None

    polar = schedule(latitude=UTSJOKI[0], longitude=UTSJOKI[1]).describe(utc(2026, 12, 21, 12))
    assert polar["polar"] == "polar_night"
    assert polar["sunset"] is None
