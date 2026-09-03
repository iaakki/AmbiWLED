# Roadmap

## Known device behaviour

Findings that cost real debugging time. Each was measured, not read.

- **Ledmaps do not apply to 1D strips.** A map uploads, `info.maps` lists it and
  `ledmap: 1` selects it, but effect rendering ignores it — a solid block written
  to logical 0..9 lit physical 0..9. They appear to be a 2D-matrix feature.
  `ledmap_from_edges()` and `tools/make_ledmap.py` are kept for reference and
  wired to nothing.
- **Ledmaps never affect realtime data.** Verified with Identify while streaming.
- **WLED scans for ledmap files only at boot.** A freshly uploaded map is
  selectable and silently ignored until a restart. Recent firmware calls the
  field `maps`, older builds `ledmaps` as a bitfield.
- **Preset saves are dropped while a realtime stream is active**, and `psave`
  still answers `{"success":true}`. `save_preset()` verifies by reading the
  preset back, never by the status code.
- **Segments live in presets, not config.** Every preset carries its own mirror
  and offset; one that lacks them resets the layout when selected.

## Wanted

### WLED brightness selection from the UI
The health check already talks to `/json/info` every 10 s, so the transport
exists for pulling the controller's own master brightness into the AmbiWled UI.
Preset selection already shipped (`mode: preset`); brightness is the piece
still missing, and it is a smaller case of the same pattern.

### Live log level
`log_level` is read once at startup; changing it in the UI does nothing until
restart.

### Structured logging
Output is human-readable text. Emit key=value or JSON when a flag asks for it.

## Open questions

### `measured` vs `processed`
`/6/ambilight/measured` gives pre-algorithm values — possibly faster, possibly
more zones, at the cost of doing the smoothing yourself. The UI already switches
endpoints; nobody has compared them.

### Music-reactive modes
WLED's AudioReactive usermod runs on the controller and needs a microphone wired
to it. It also **suspends audio processing while a realtime stream is active**,
so the two are mutually exclusive by construction: AmbiWled stops streaming and
the controller takes over. Driving that handover from MQTT would make it a mode
Home Assistant can select.

Note the mic is mono, so left and right cannot respond differently to stereo.

### Row-wise effects across the room
Effects that treat the whole front run as one row, the sides as the rows
between, and the rear run as the last row. Not expressible with 1D segments,
and a faithful 2D matrix needs a canvas large enough to address every pixel on
the perimeter — roughly 134 × 99 for 462 pixels, about 52 KB of buffer against
~108 KB of free heap on an ESP32. A board with PSRAM would change that.

The closest approximation that does work is the front → back segment layout: one
mirrored segment offset to the middle of the front run, so a single effect
curls from the front centre to the rear centre in phase.

## Done

- Per-edge brightness trim, applied after the global level.
- Container `HEALTHCHECK` against `/api/state`, reading the configured port
  from `config.json` so it survives a port change.

- Automated tests: no TV or controller required, both faked.
- Emission gated on three conditions: TV streaming, TV's Ambilight actually
  powered on, and a controller answering.
- Controller telemetry and conflict detection: reports when something else is
  driving the strip, or when it has been overridden locally.
- Perceptual brightness curve.
- Sun-driven auto-dim, computed locally: a parabolic dome peaking at solar
  noon, flat at the minimum through the night, with a configurable exponent
  to narrow or broaden the midday peak.
- MQTT with Home Assistant discovery.
- Segment layouts derived from the edge configuration.
- Config written immediately and flushed before shutdown.
