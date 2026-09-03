"""Sun-driven dimming.

Finland's sunset moves from about 15:20 in midwinter to past 23:00 at midsummer,
so a clock schedule is useless — the level has to follow the sun.  Times are
computed locally with the standard sunrise equation: no external API, no network
dependency, correct when the internet is down.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

# Solar disc centre at -0.833 deg accounts for refraction and the disc radius.
_ZENITH = math.radians(-0.833)
_OBLIQUITY = math.radians(23.4397)
_J2000 = 2451545.0


def sun_times(lat: float, lon: float, day: date) -> tuple[datetime, datetime] | None:
    """Sunrise and sunset in UTC for `day`, or None above the polar circles
    when the sun neither rises nor sets.  Accurate to about a minute."""
    n = day.toordinal() - date(2000, 1, 1).toordinal() + 0.0008
    j_star = n - lon / 360.0

    m = math.radians((357.5291 + 0.98560028 * j_star) % 360.0)
    c = (1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m))
    lam = math.radians((math.degrees(m) + c + 180.0 + 102.9372) % 360.0)

    j_transit = _J2000 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    declination = math.asin(math.sin(lam) * math.sin(_OBLIQUITY))

    phi = math.radians(lat)
    denom = math.cos(phi) * math.cos(declination)
    if abs(denom) < 1e-12:
        return None
    cos_omega = (math.sin(_ZENITH) - math.sin(phi) * math.sin(declination)) / denom
    if cos_omega >= 1.0:
        return None          # polar night: the sun never rises
    if cos_omega <= -1.0:
        return None          # polar day: the sun never sets

    omega = math.degrees(math.acos(cos_omega))
    return _from_julian(j_transit - omega / 360.0), _from_julian(j_transit + omega / 360.0)


def polar_state(lat: float, lon: float, day: date) -> str:
    """When there is no sunrise/sunset, say which case it is."""
    n = day.toordinal() - date(2000, 1, 1).toordinal() + 0.0008
    j_star = n - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360.0)
    c = (1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m))
    lam = math.radians((math.degrees(m) + c + 180.0 + 102.9372) % 360.0)
    declination = math.asin(math.sin(lam) * math.sin(_OBLIQUITY))
    phi = math.radians(lat)
    denom = math.cos(phi) * math.cos(declination)
    if abs(denom) < 1e-12:
        return "unknown"
    cos_omega = (math.sin(_ZENITH) - math.sin(phi) * math.sin(declination)) / denom
    return "polar_night" if cos_omega >= 1.0 else "polar_day"


def _from_julian(j: float) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=j - 2440587.5)


def _dome(s: float, exponent: float) -> float:
    """0 at s=0 and s=1, peak 1 at s=0.5 — a parabola at exponent=1.

    A downward parabola over [0, 1] is `4s(1-s)`: zero at both ends, exactly
    1.0 at the midpoint. Raising it to a power keeps those same three points
    fixed while reshaping what happens between them — > 1 narrows the midday
    peak, < 1 broadens it into more of a plateau.
    """
    s = min(max(s, 0.0), 1.0)
    base = 4.0 * s * (1.0 - s)
    return base if exponent == 1.0 else base ** exponent


class DimmingSchedule:
    """Turns the clock and a location into a 0..1 brightness level.

    Outside the sunrise..sunset window the level sits flat at the minimum —
    "dark" is dark all night, not still fading at 2 a.m. Inside it, the level
    follows a dome peaking at solar noon, so midday is brightest and both ends
    of the day taper down to meet the night level with no seam: the dome is
    exactly 0 at sunrise and at sunset, which is also where the flat night
    segment sits.
    """

    def __init__(self, cfg: dict[str, Any]) -> None:
        self._cache: tuple[date, tuple[datetime, datetime] | None] | None = None
        self.update(cfg)

    def update(self, cfg: dict[str, Any]) -> None:
        d = cfg.get("dimming", {})
        self.enabled = bool(d.get("enabled", False))
        self.latitude = float(d.get("latitude", 60.17))
        self.longitude = float(d.get("longitude", 24.94))
        self.day_level = float(d.get("day_level", 1.0))
        self.night_level = float(d.get("night_level", 0.4))
        self.curve_exponent = max(float(d.get("curve_exponent", 1.0)), 0.05)
        self.sunrise_offset = float(d.get("sunrise_offset_minutes", 0.0))
        self.sunset_offset = float(d.get("sunset_offset_minutes", 0.0))
        self._cache = None

    def times_for(self, day: date) -> tuple[datetime, datetime] | None:
        if self._cache and self._cache[0] == day:
            return self._cache[1]
        times = sun_times(self.latitude, self.longitude, day)
        self._cache = (day, times)
        return times

    def day_fraction(self, now: datetime) -> float:
        """1.0 at solar noon, 0.0 outside the sunrise..sunset window."""
        now = now.astimezone(timezone.utc)
        times = self.times_for(now.date())
        if times is None:
            return 0.0 if polar_state(self.latitude, self.longitude, now.date()) == "polar_night" else 1.0

        sunrise, sunset = times
        sunrise = sunrise + timedelta(minutes=self.sunrise_offset)
        sunset = sunset + timedelta(minutes=self.sunset_offset)
        span = (sunset - sunrise).total_seconds()
        if span <= 0 or not (sunrise <= now <= sunset):
            return 0.0
        s = (now - sunrise).total_seconds() / span
        return _dome(s, self.curve_exponent)

    def level(self, now: datetime | None = None) -> float:
        """The multiplier to apply to the configured brightness."""
        if not self.enabled:
            return 1.0
        now = now or datetime.now(timezone.utc)
        f = self.day_fraction(now)
        return self.night_level + (self.day_level - self.night_level) * f

    def describe(self, now: datetime | None = None) -> dict[str, Any]:
        """What the UI shows: today's times and where we are in the ramp."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        times = self.times_for(now.date())
        out: dict[str, Any] = {
            "enabled": self.enabled,
            "level": round(self.level(now), 4),
            "day_fraction": round(self.day_fraction(now), 4),
            "sunrise": None,
            "sunset": None,
            "polar": None,
        }
        if times is None:
            out["polar"] = polar_state(self.latitude, self.longitude, now.date())
        else:
            out["sunrise"] = times[0].isoformat()
            out["sunset"] = times[1].isoformat()
        return out
