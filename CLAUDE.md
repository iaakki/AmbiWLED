# AmbiWled — notes for Claude Code

The original design specification lives in [`docs/SPEC.md`](docs/SPEC.md); read it
before changing the pixel path, the mapping model or the output protocol.

## Working agreements

- **Measure, don't assume.** Several things in this project behave differently
  from the documentation: WLED ignores ledmaps on 1D strips, silently drops
  preset saves while a realtime stream is active, and only scans for ledmap
  files at boot. Each was found by testing, not by reading. When a device
  reports success, verify the effect.
- **Tests must not need hardware.** `make test` runs against a fake TV and a
  fake controller. Anything that needs a real device is a gap in the fakes.
  `tests/test_ui.py` drives the real UI in a browser to catch bugs the HTTP
  API alone can't see (stale DOM state, duplicated listeners, response-
  ordering races) - it skips itself, rather than fails, if Firefox and
  geckodriver aren't installed, so it stays within this rule everywhere else.
- **The config is the single source of truth for geometry.** Segment layouts,
  offsets and pixel ranges are derived from `mapping.edges`, never hand-typed
  in two places.
- **Never commit personal data.** Device backups, `config/config.json` and
  generated ledmaps are gitignored; they contain addresses, WiFi SSIDs and
  broker credentials.

## Layout

```
ambiwled/        service: source → mapping → colour → frames → output
  mapping.py     source zones to pixels, as one weight matrix
  frames.py      smoothing, interpolation, passthrough
  output.py      DNRGB UDP packets
  health.py      controller reachability and telemetry
  daylight.py    local sunrise/sunset, no external API
  mqtt.py        Home Assistant discovery and control
  wled.py        segment and preset management on the controller
tools/           standalone helpers, stdlib only
tests/           pytest; no TV or controller required
```
