# Ambilight → WLED Bridge — Technical Specification

A containerised service that reads Ambilight colour data from a Philips Android TV,
maps it onto a ceiling-mounted LED rectangle, and streams it to a WLED controller at a
higher frame rate than the source provides.

---

## 1. System context

```
Philips TV (JointSPACE)          Bridge service (any Docker host)         WLED controller
  /6/ambilight/processed   ──►   poll → map → smooth → emit   ──►    WLED controller, UDP :21324
  HTTP, 10–20 fps                Docker container                    UDP DNRGB, 60 fps
                                        │
                                        └── Web UI (config + live preview)
```

### Hardware facts (fixed)

| Item | Value |
|---|---|
| LED strip | SK6812/WS2814 RGBW, 12 V, 60 LED/m, 1 IC per LED |
| Controller | Single WLED device, two output buses |
| Bus 1 | pixels 0–230 (231 px) |
| Bus 2 | pixels 231–461 (231 px) |
| Total | 462 pixels |
| Geometry | Ceiling rectangle, 222 cm × 164 cm |
| Physical runs | 133 px (222 cm) and 98 px (164 cm), two of each |

### Source data shape (confirmed by probe)

`GET http://<tv-ip>:1925/6/ambilight/processed` returns:

```json
{"layer1": {
  "left":  {"0": {"r":..,"g":..,"b":..}, ... "4":  {...}},
  "top":   {"0": {...}, ...                  "10": {...}},
  "right": {"0": {...}, ...                  "4":  {...}}
}}
```

- Three sides only: **left = 5 zones, top = 11 zones, right = 5 zones** (21 total).
- **No `bottom` layer.** The rear ceiling edge must be synthesised.
- Index direction is a clockwise loop around the screen: `left` runs bottom→top,
  `top` runs left→right, `right` runs top→bottom.
- The service must **not** hardcode these counts. Read the JSON structure on connect
  and adapt. Log the detected layout at startup.

---

## 2. Functional requirements

### FR-1 — Source polling
- Poll the TV endpoint in a loop, with HTTP keep-alive (single reused connection).
- Target poll rate configurable, default 20 Hz. Actual achieved rate will be lower.
- Measure and expose actual source fps as a metric.
- **Adaptive backoff:** if median response time exceeds the poll interval, reduce the
  poll rate automatically rather than queueing requests. The TV is the weakest link and
  must not be hammered.
- API version detection at startup: `GET /<ip>:1925/6/system`. If `api_version.Major`
  is 1, use `/1/ambilight/processed` instead of `/6/`.

### FR-2 — TV presence detection
- Consecutive request failures (default 3) → TV considered off → stop emitting.
- Continue probing at a slow rate (default every 5 s) to detect wake-up.
- No separate integration needed; the poll loop is the detector.
- MEASURED (2026-09-05, real Philips Android TV, `os_type: MSAF_2018_O`):
  the JointSPACE API itself is not sufficient for this. Both
  `/ambilight/processed` and `/ambilight/power` kept answering
  successfully - `power: "On"`, zones all black - for minutes after the
  screen physically turned off, a Philips "Quick Start" network-standby
  behaviour that cannot be disabled without also losing the ability to
  wake the TV over the network. Pairing once (`ambiwled/pairing.py`, a
  digest-auth handshake reverse-engineered by the open-source JointSPACE
  client community, not invented here) unlocks `/powerstate` and
  `/screenstate` over HTTPS on port 1926, which answer truthfully
  regardless of standby - confirmed live against the same set. When
  paired, that answer overrides the ambilight-based guess; unpaired
  installs fall back to the behaviour above, unchanged.

### FR-3 — Mapping
Transform 21 source zones into 462 output pixels. See §3.

### FR-4 — Frame generation
Produce output frames at a configurable rate (default 60 Hz) regardless of source rate.
See §4.

### FR-5 — Output
Emit UDP DNRGB packets to the WLED controller. See §5.

### FR-6 — Web UI
Configuration, live preview, and metrics. See §6.

### FR-7 — Persistence
All configuration stored as a single JSON file in a mounted volume. Changes applied
live without restart. The web UI edits this file.

---

## 3. Mapping model

### 3.1 Concept

The screen image is conceptually hinged at its top edge and folded flat onto the
ceiling: the image's top edge lands on the ceiling edge nearest the TV, the image's
bottom edge lands on the far ceiling edge behind the viewer, and the image's left and
right edges stretch along the ceiling's side runs from front to back.

### 3.2 Default layout

The 222 cm run goes on the TV wall (11 source zones spread over the long run gives
~20 cm/zone; the 5-zone sides get ~33 cm/zone). Channel 1 starts at the TV-wall left
corner; channel 2 starts at the diagonally opposite corner.

| Output pixels | Ceiling edge | Direction | Source | px/zone |
|---|---|---|---|---|
| 0–132 | TV wall | left→right | `top[0..10]` | 12.1 |
| 133–230 | right side | front→back | `right[0..4]` | 19.6 |
| 231–363 | rear | right→left | synthesised | — |
| 364–461 | left side | back→front | `left[0..4]` | 19.6 |

All four segments consume source data in its natural index order — no reversed arrays.

### 3.3 Config schema for mapping

The layout must be **data-driven**, not hardcoded, because it will be iterated on
during physical installation. Each ceiling edge is described as:

```json
{
  "edges": [
    {
      "name": "tv_wall",
      "pixel_start": 0,
      "pixel_count": 133,
      "source": "top",
      "source_reversed": false,
      "output_reversed": false
    },
    {
      "name": "right_side",
      "pixel_start": 133,
      "pixel_count": 98,
      "source": "right",
      "source_reversed": false,
      "output_reversed": false
    },
    {
      "name": "rear",
      "pixel_start": 231,
      "pixel_count": 133,
      "source": "synth_gradient",
      "synth_from": ["right", -1],
      "synth_to": ["left", 0]
    },
    {
      "name": "left_side",
      "pixel_start": 364,
      "pixel_count": 98,
      "source": "left",
      "source_reversed": false,
      "output_reversed": false
    }
  ]
}
```

Pixel ranges must be validated: no overlaps, no gaps, total equals configured LED count.
Surface validation errors in the UI, do not silently accept.

### 3.4 Interpolation across an edge

Do **not** block-repeat zone colours — at ~20 px per zone this produces visible bands.

For an edge with `n` output pixels and `z` source zones:

1. Place each zone's colour at its **centre position**: zone `i` sits at
   output position `(i + 0.5) * n / z`.
2. Linearly interpolate between adjacent zone centres.
3. Before the first centre and after the last centre, either clamp (hold the end
   colour) or blend into the neighbouring edge's nearest zone.

**Corner blending:** option 3-blend is preferred and should be the default. Because
`top[10]` and `right[0]` are the same physical screen corner, blending across the edge
boundary makes corners continuous with no seam. Make this a boolean config option
(`corner_blend`, default true) so it can be disabled for debugging.

**Blend width (`mapping.zone_blend`, default 1.0):** step 2 above is really a
triangular (tent) kernel whose half-width is one zone-spacing - `zone_blend`
scales that width. At 1.0 it is exactly the plain 2-tap linear scheme described
above. Below 1.0 the kernel narrows until neighbouring zones no longer overlap
at all, at which point each pixel takes its single nearest zone verbatim - a
user-visible option for people who find interpolation too smooth relative to
what the TV actually sent (reported directly: with only ~11 zones across an
edge, a moving on-screen object can look like it crosses in discrete blocky
steps rather than smoothly, and that is a real property of having few zones,
not a bug in the blend - the slider lets someone choose to see it plainly
rather than have it smoothed over). Above 1.0 the kernel widens to pull in
zones beyond the immediate neighbours, for a softer blend than a 2-tap scheme
can produce. Range 0.0-4.0, exposed as a slider on the Colour page.

### 3.5 Rear edge synthesis

The rear edge has no source. Default mode `synth_gradient`: a linear gradient from
`right[last]` to `left[0]` — these are the screen's two bottom corners, so the rear
ceiling shows the overall tone of the lower image. Correct behaviour and cheap.

Additional modes to implement, selectable in config:

- `mirror_top` — copy the TV-wall edge, reversed.
- `average` — flat colour, the mean of all source zones.
- `off` — leave the rear edge dark.

### 3.6 Colour processing (applied after mapping, before smoothing)

Configurable, all optional, in this order:

1. **Saturation gain** (default 1.0) — Ambilight output is often muted; a modest boost
   looks better on a ceiling than on a TV bezel.
2. **Brightness scale** (default 1.0).
3. **Gamma** (default 1.0 = off). WLED has its own gamma setting; applying it in both
   places double-corrects and crushes low end. Document this clearly in the UI.
4. **Black floor / cutoff** — values below a threshold snap to zero, to stop the
   ceiling glowing faintly during dark scenes.

Output is **RGB only**. The W channel is derived by WLED's own
"Auto-calculate W channel from RGB" setting (Accurate). Do not stream RGBW: it would
push the packet past the single-packet limit and duplicates work WLED already does.

---

## 4. Frame generation

Source arrives at 10–20 fps with hard steps between frames. Output runs at a fixed
configurable rate (default 60 fps) driven by its own timer, decoupled from the poll loop.

### 4.1 Mode A — Exponential smoothing (default)

Per pixel, per channel, each output tick:

```
out += alpha * (target - out)
alpha = 1 - exp(-dt / tau)
```

Where `target` is the most recently mapped source frame, `dt` is the output tick
interval, and `tau` is the configurable time constant (default 80 ms).

**No added latency beyond the filter's own lag, and no overshoot.**

**Adaptive alpha (default on):** when the per-pixel delta is large — a scene cut —
scale `alpha` toward 1.0 so cuts stay snappy while gradual pans stay smooth:

```
delta = |target - out|              (0..255 per channel, take max of the three)
boost = clamp(delta / cut_threshold, 0, 1)
alpha_eff = alpha + (1 - alpha) * boost
```

`cut_threshold` configurable, default 100.

### 4.2 Mode B — Linear interpolation

Buffer the last two source frames and linearly interpolate between them based on
elapsed time since the newer frame arrived. Smoothest possible result.

**Costs one full source frame of latency (50–100 ms).** The UI must state this next to
the mode selector so the trade-off is explicit.

Requires estimating the source frame interval; use a rolling median of recent arrival
intervals rather than the configured poll rate, which will not match reality.

### 4.3 Mode C — Passthrough

Emit the latest mapped frame with no temporal processing, at the output rate. For
debugging and for measuring what the raw source actually looks like.

### 4.4 Notes

- Work in float internally, quantise to uint8 only at packet build.
- Use numpy; 462 × 3 at 60 Hz is negligible but a naive Python loop per pixel is not.
- **Disable WLED's own crossfade/transition time** when streaming. Two smoothing
  stages in series produce mush. Note this in the UI setup help.

---

## 5. Output protocol

### 5.1 DNRGB (WLED UDP realtime)

Port **21324**. Packet layout:

| Offset | Bytes | Value |
|---|---|---|
| 0 | 1 | `4` (DNRGB protocol) |
| 1 | 1 | timeout in seconds |
| 2–3 | 2 | start LED index, big-endian uint16 |
| 4+ | 3n | RGB triplets |

- Max 489 LEDs per packet. **462 fits in a single packet** (1390 bytes total) — no
  fragmentation logic required, but implement the multi-packet path anyway so the
  pixel count is not silently capped if the layout changes.
- **Timeout byte = 2.** After 2 s without packets WLED reverts to its previous state,
  which gives the automatic return-to-preset behaviour for free when the TV goes off.
  Do not set 255 (hold forever) — that breaks the fallback.

### 5.2 Emission behaviour

- Fire-and-forget UDP; no retries, no acknowledgement.
- When the TV is off, simply **stop sending**. Do not send black frames — letting the
  timeout expire is what triggers WLED's fallback to the boot preset.
- Support multiple output targets in config (list of `{host, port, pixel_offset,
  pixel_count}`) so a second controller can be added later without a rewrite.

---

## 6. Web UI

Single-page, served by the same container. No build toolchain requirement — plain
HTML/CSS/JS served static is fine and keeps the image small.

### 6.1 Live preview (primary view)

A top-down schematic of the ceiling rectangle rendered as an SVG or canvas, showing the
actual current output colours at ~15 fps via WebSocket. This is the main debugging tool:
mapping errors are obvious the moment you can see the rectangle next to the TV.

Alongside it, a strip view showing the raw 462-pixel array in output index order, so
edge boundaries and reversal errors are visible.

### 6.2 Configuration panels

**Source**
- TV IP address, API version (auto-detected, overridable)
- Poll rate target
- Detected layout, read-only: side names and zone counts

**Mapping**
- Per-edge editor: name, pixel start, pixel count, source side, reversal flags
- Rear synthesis mode
- Corner blend toggle
- **Live validation** with inline errors for overlaps, gaps, and total mismatch
- An "identify" button per edge that lights only that edge in a solid colour — the
  fastest way to verify physical orientation after installation

**Frame generation**
- Mode: smoothing / interpolation / passthrough
- Output fps
- Tau (smoothing mode)
- Adaptive alpha toggle and cut threshold
- Latency note displayed per mode

**Colour**
- Saturation, brightness, gamma, black floor
- Warning text about double gamma correction

**Output**
- Target list: host, port, offset, count
- Timeout seconds

### 6.3 Metrics bar (always visible)

- Source fps (actual, rolling average)
- Output fps (actual)
- Source latency (median TV response time)
- TV state: streaming / TV off / error
- Dropped or failed polls counter

### 6.4 Behaviour

- All changes apply live. No restart, no explicit save button for runtime values —
  persist on change with a short debounce.
- A "reset to defaults" action.
- Config export/import as JSON, so a working mapping can be backed up before
  experimenting.

---

## 7. Non-functional requirements

- **Language:** Python 3.11+, asyncio. `aiohttp` for both the TV client and the web
  server. `numpy` for pixel maths.
- **Container:** single image, `python:3.11-slim` base. Bridge networking is sufficient
  — outbound UDP works fine and only the web UI port needs mapping. Host networking is
  not required.
- **Config volume:** `/config`, containing `config.json`.
- **Logging:** structured, level configurable. Log the detected source layout, mapping
  validation results, and TV state transitions. Do not log per-frame.
- **Startup:** must come up and serve the UI even if the TV is unreachable, showing
  "TV off" rather than crash-looping.
- **Resource target:** well under 10% of one core at 60 fps output. If it exceeds that,
  the pixel path is doing something wrong.

---

## 8. Deliverables

1. `Dockerfile` and `docker-compose.yml`
2. Service source
3. Default `config.json` matching §3.2
4. `README.md`: setup, TV API activation, WLED-side settings required
   (Auto-calculate W = Accurate, crossfade disabled, realtime timeout, boot preset)
5. A standalone probe script that dumps the TV's Ambilight JSON and prints the detected
   layout — useful before the full service is configured

---

## 9. Open questions to resolve during implementation

1. **Actual achievable poll rate.** 20 Hz is the target; the TV may not sustain it.
   Measure early — it determines whether Mode B's latency cost is acceptable.
2. **Index direction confirmation.** The clockwise loop is inferred from corner colour
   continuity in a single sample. Confirm with a test image having distinctly coloured
   corners before trusting the default mapping.
3. **Zone counts across sources.** Do the zone counts change between Ambilight styles
   or input sources? If so, the mapping must handle a mid-stream layout change rather
   than only reading it at startup.
4. **Whether `measured` beats `processed`.** `/6/ambilight/measured` gives pre-algorithm
   values. Worth a comparison once the pipeline works — it may respond faster or offer
   more zones, at the cost of doing the smoothing work yourself.
