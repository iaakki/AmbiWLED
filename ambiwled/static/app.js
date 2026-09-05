'use strict';

/* ============================================================
   AmbiWled — vanilla-JS UI, ported from the Organic-design
   prototype (see docs/SPEC.md history). No framework: this talks
   directly to /api/config, /api/state and /ws, same contract the
   old UI used, so nothing about the backend needed to change to
   support a from-scratch look.
   ============================================================ */

const $ = (s, root) => (root || document).querySelector(s);
const $$ = (s, root) => Array.from((root || document).querySelectorAll(s));
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const el = (tag, attrs, ...kids) => {
  const n = document.createElement(tag);
  for (const k in (attrs || {})) {
    const v = attrs[k];
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') n.className = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, '');
    else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) { if (k != null) n.append(k.nodeType ? k : document.createTextNode(k)); }
  return n;
};

/* ---------- global state ---------- */

let cfg = null;
let metrics = {};
let pixels = new Uint8Array(0);
let ws = null;
let wsBackoff = 1000;

let advanced = false;
let page = 'home';
let wiz = null;               // {step, countIdx, pick, mqttTest, mqttErr} while running
let pwEdit = false;            // Integrations page: password field unlocked
let flashEdge = '';
let confirmState = null;

let pendingPatch = null;
let saveTimer = null;

const SLOTS = ['front', 'left', 'right', 'back'];
const SLOT_COLOUR = { front: '#ff8a3d', left: '#e2557a', right: '#4fb8a8', back: '#6a7fd8' };
const SLOT_LABEL = { front: 'Over the TV', left: 'Left side', right: 'Right side', back: 'Behind the couch' };
const SLOT_QUESTION = {
  front: 'How many LEDs over the TV?', left: 'How many LEDs down the left side?',
  right: 'How many LEDs down the right side?', back: 'How many LEDs behind the couch?',
};
const SLOT_DEFAULT_SOURCE = { front: 'top', left: 'left', right: 'right', back: 'synth_gradient' };
const SOURCE_OPTIONS = [
  ['top', 'Top of the picture'], ['left', 'Left of the picture'], ['right', 'Right of the picture'],
  ['synth_gradient', 'Made up from its neighbours'], ['mirror_top', 'Mirror of the front'],
  ['average', 'Average of the whole picture'], ['off', 'Stays dark'],
];
const TABS = [
  ['home', 'Home'], ['colour', 'Colour'], ['dim', 'Auto-dim'], ['source', 'Source'],
  ['mapping', 'Mapping'], ['output', 'Output'],
  ['integrations', 'Integrations'], ['config', 'Config'],
];

/* ---------- boot ---------- */

function boot() {
  advanced = localStorage.getItem('ambiwled.mode') === 'advanced';
  const theme = localStorage.getItem('ambiwled.theme');
  if (theme === 'dark' || theme === 'light') document.documentElement.setAttribute('data-theme', theme);

  connectWs();
  setInterval(renderClock, 1000);
  setInterval(() => { if (page === 'dim' && advanced) renderPage(); }, 60000);
  wireStaticControls();
}

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { wsBackoff = 1000; setLink(true); };
  ws.onclose = () => { setLink(false); scheduleReconnect(); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      if (msg.type === 'hello') {
        cfg = msg.config;
        renderAll();
      } else if (msg.type === 'metrics') {
        metrics = msg;
        renderLive();
      }
    } else {
      pixels = new Uint8Array(ev.data);
      renderPreviewFromPixels();
    }
  };
}
function scheduleReconnect() {
  setTimeout(connectWs, wsBackoff);
  wsBackoff = Math.min(wsBackoff * 1.6, 15000);
}
function setLink(up) {
  const dot = $('#link-dot');
  if (dot) dot.classList.toggle('down', !up);
}

/* ---------- config patch / autosave ---------- */

function mergeInto(base, patch) {
  for (const k in patch) {
    const v = patch[k];
    if (v && typeof v === 'object' && !Array.isArray(v) && base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])) {
      mergeInto(base[k], v);
    } else {
      base[k] = v;
    }
  }
  return base;
}

let _pushSeq = 0;
function pushConfig(patch, immediate) {
  pendingPatch = mergeInto(pendingPatch || {}, patch);
  clearTimeout(saveTimer);
  // Two immediate pushes in quick succession (e.g. toggling something off
  // then straight back on) are two separate in-flight requests, and network
  // latency gives no guarantee the responses come back in the order they
  // were sent. Only the response to the *most recently issued* request may
  // update cfg - an older one arriving late must not un-apply a newer
  // change the user already made.
  const mySeq = ++_pushSeq;
  const send = async () => {
    const body = pendingPatch;
    pendingPatch = null;
    if (!body || !Object.keys(body).length) return;
    try {
      const r = await fetch('/api/config', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patch: body }),
      });
      const data = await r.json();
      if (!r.ok) { toast((data.errors || ['not applied'])[0], true); return; }
      if (mySeq === _pushSeq) {
        cfg = data.config;
        showSaved();
      }
    } catch (err) {
      toast('Not applied: ' + err.message, true);
    }
  };
  if (immediate) send(); else saveTimer = setTimeout(send, 350);
}

/** Set one field, mutate the local mirror immediately, save debounced. */
function set(path, value, immediate) {
  const parts = path.split('.');
  let cur = cfg;
  for (let i = 0; i < parts.length - 1; i++) cur = cur[parts[i]];
  cur[parts[parts.length - 1]] = value;
  const patch = {};
  let pcur = patch;
  parts.forEach((p, i) => { pcur[p] = i === parts.length - 1 ? value : {}; pcur = pcur[p]; });
  pushConfig(patch, immediate !== false);
}
function get(path) { return path.split('.').reduce((o, k) => (o == null ? o : o[k]), cfg); }

let _savedTimer = null;
function showSaved() {
  const t = $('#toast');
  clearTimeout(_savedTimer);
  t.textContent = 'Saved'; t.className = ''; t.hidden = false;
  _savedTimer = setTimeout(() => { t.hidden = true; }, 1300);
}
function toast(msg, bad) {
  const t = $('#toast');
  clearTimeout(_savedTimer);
  t.textContent = msg; t.className = bad ? 'bad' : ''; t.hidden = false;
  _savedTimer = setTimeout(() => { t.hidden = true; }, bad ? 3200 : 1300);
}

/* ---------- confirm dialog ---------- */

function confirmAction(title, body, actionLabel, danger, onConfirm) {
  confirmState = { title, body, actionLabel, danger, onConfirm };
  renderConfirm();
}
function renderConfirm() {
  const overlay = $('#confirm-overlay');
  if (!confirmState) { overlay.hidden = true; return; }
  overlay.hidden = false;
  $('#confirm-title').textContent = confirmState.title;
  $('#confirm-body').textContent = confirmState.body;
  const ok = $('#confirm-ok');
  ok.textContent = confirmState.actionLabel;
  ok.style.background = confirmState.danger ? 'var(--err)' : 'var(--color-accent)';
}
function wireConfirmDialog() {
  $('#confirm-cancel').addEventListener('click', () => { confirmState = null; renderConfirm(); });
  $('#confirm-ok').addEventListener('click', () => {
    const fn = confirmState && confirmState.onConfirm;
    confirmState = null; renderConfirm();
    if (fn) fn();
  });
}

/* ---------- edges (front/left/right/back canonical slots) ---------- */

function edgeBySlot(slot) { return cfg.mapping.edges.find((e) => e.name === slot); }
/** Present sides, in *wiring* order - the order they actually sit in
    cfg.mapping.edges, which is what determines each one's pixel_start.
    Not the fixed front/left/right/back constant: which side's strip your
    controller's data line reaches first depends on the install, not on
    this list. */
function presentSlots() {
  return cfg.mapping.edges.map((e) => e.name).filter((n) => SLOTS.includes(n));
}

function repack() {
  let cursor = 0;
  for (const e of cfg.mapping.edges) {
    e.pixel_start = cursor;
    cursor += Math.max(1, e.pixel_count | 0);
  }
  cfg.led.count = cursor;
}
function moveEdge(slot, dir) {
  const edges = cfg.mapping.edges;
  const i = edges.findIndex((e) => e.name === slot);
  const j = i + dir;
  if (i < 0 || j < 0 || j >= edges.length) return;
  [edges[i], edges[j]] = [edges[j], edges[i]];
  repack();
  pushEdges();
}
function pushEdges(immediate) {
  pushConfig({ mapping: { edges: cfg.mapping.edges }, led: { count: cfg.led.count } }, immediate !== false);
}

function toggleSlot(slot) {
  const existing = edgeBySlot(slot);
  if (existing) {
    if (presentSlots().length === 1) { toast("At least one side has to stay on", true); return; }
    cfg.mapping.edges = cfg.mapping.edges.filter((e) => e.name !== slot);
  } else {
    const src = SLOT_DEFAULT_SOURCE[slot];
    const e = { name: slot, pixel_start: 0, pixel_count: 60, source: src, reversed: false, brightness: 1.0 };
    if (src === 'synth_gradient') { e.synth_from = ['right', -1]; e.synth_to = ['left', 0]; }
    if (src === 'mirror_top') e.mirror_of = 'front';
    cfg.mapping.edges.push(e);
  }
  repack();
  pushEdges(true);
}
function editEdge(slot, patch) {
  Object.assign(edgeBySlot(slot), patch);
  repack();
  pushEdges();
}
function hexToRgb(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
  return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [255, 255, 255];
}
/** Same colour on the real strip as this edge's dot in the app - a chase,
    not a flat fill, so a flipped edge is visible: it runs the same
    direction on the LEDs as the Flip button says it should. */
function flashSlot(slot) {
  clearTimeout(window._flashTimer);
  flashEdge = slot;
  renderPreviewFromPixels();
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ type: 'identify', edge: slot, seconds: 4.8, colour: hexToRgb(SLOT_COLOUR[slot]) }));
  }
  window._flashTimer = setTimeout(() => { flashEdge = ''; renderPreviewFromPixels(); }, 900);
}

/* ---------- sliders (custom pointer drag, matches the design) ---------- */

// Most sliders live inside a page that gets torn down and rebuilt (innerHTML
// = '') on every render, so a fresh element each time is normal and each
// needs its own listeners. #bright-slider on the Simple page is the one
// exception - it's static markup, re-wired on every renderSimple() call
// (every toggle, every save) - so without this guard it would pick up a
// fresh pair of pointerdown/pointermove listeners each time, all firing on
// every future drag: one PUT per accumulated listener per pointermove
// event, not one.
const _wiredSliders = new WeakSet();
function wireSlider(track, thumb, fill, thumbSize, getPct, setPct) {
  const paint = (pct) => {
    fill.style.width = pct + '%';
    thumb.style.left = `calc((100% - ${thumbSize}px) * ${clamp(pct, 0, 100) / 100})`;
  };
  if (!_wiredSliders.has(track)) {
    _wiredSliders.add(track);
    const fromEvent = (ev) => {
      const r = track.getBoundingClientRect();
      return clamp(Math.round(((ev.clientX - r.left) / r.width) * 100), 0, 100);
    };
    track.addEventListener('pointerdown', (ev) => {
      try { track.setPointerCapture(ev.pointerId); } catch (e) {}
      const pct = fromEvent(ev); paint(pct); setPct(pct);
    });
    track.addEventListener('pointermove', (ev) => {
      if (!ev.buttons) return;
      const pct = fromEvent(ev); paint(pct); setPct(pct);
    });
  }
  paint(getPct());
  return paint;
}

/* ---------- clock / preview ---------- */

function renderClock() {
  const c = $('#clock');
  if (c) c.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function edgeAverageColour(slot) {
  const e = edgeBySlot(slot);
  if (!e || e.source === 'off' || !pixels.length) return null;
  const start = e.pixel_start * 3, count = e.pixel_count;
  if (start + count * 3 > pixels.length) return null;
  let r = 0, g = 0, b = 0;
  for (let i = 0; i < count; i++) { r += pixels[start + i * 3]; g += pixels[start + i * 3 + 1]; b += pixels[start + i * 3 + 2]; }
  r = Math.round(r / count); g = Math.round(g / count); b = Math.round(b / count);
  return `rgb(${r},${g},${b})`;
}

function paintPreview(prefix) {
  const on = get('mode') !== 'off';
  for (const slot of SLOTS) {
    const poly = $(`#${prefix}-${slot}`);
    const line = $(`#${prefix}-${slot}-ln`);
    const e = edgeBySlot(slot);
    let colour = 'transparent', opacity = 0;
    if (flashEdge === slot) { colour = '#ffffff'; opacity = 1; }
    else if (e && e.source !== 'off' && on) {
      colour = edgeAverageColour(slot) || SLOT_COLOUR[slot];
      opacity = 0.85;
    } else if (e && e.source !== 'off') {
      colour = SLOT_COLOUR[slot]; opacity = 0.08;
    }
    if (poly) { poly.setAttribute('fill', colour); poly.setAttribute('opacity', opacity); }
    if (line) { line.setAttribute('stroke', colour); line.setAttribute('stroke-opacity', opacity); }
  }
}
function renderPreviewFromPixels() {
  if (!cfg) return;
  paintPreview('pv');
  if (wiz) paintPreview('wz');
  const veil = $('#preview-veil');
  if (veil) veil.style.opacity = get('mode') === 'off' ? 0.7 : 0;
}

/* ---------- live (metrics-driven, no full re-render) ---------- */

function renderLive() {
  renderPreviewFromPixels();
  const on = get('mode') !== 'off';
  const emitting = !!metrics.emitting;
  const line = $('#status-line'), sub = $('#status-sub');
  if (line) {
    if (!on) { line.textContent = 'The ceiling is off.'; }
    else if (metrics.tv_state && metrics.tv_state !== 'streaming') { line.textContent = "Waiting for the TV…"; }
    else if (!emitting) { line.textContent = "Can't reach the lights."; }
    else { line.textContent = 'Your ceiling is following the telly.'; }
  }
  if (sub) {
    sub.hidden = on;
    sub.textContent = 'Tap the big button to turn it back on.';
  }

  if (advanced && page === 'source') {
    const meta = $('#source-meta');
    if (meta) meta.textContent = `API v${metrics.api_version || '?'} · polling at ${cfg.source.poll_hz} fps · `
      + `${metrics.source_latency_ms ?? '–'} ms · ${metrics.failed_polls ?? 0} failed`;
  }
  if (advanced && page === 'output') {
    const s = $('#output-summary');
    if (s) {
      const online = metrics.targets_online || 0;
      const total = presentSlots().reduce((n, sl) => n + edgeBySlot(sl).pixel_count, 0);
      s.textContent = `${presentSlots().length} segments · ${total} LEDs · ${online} controller${online === 1 ? '' : 's'} reachable`;
    }
    const dot = $('#output-dot');
    if (dot) dot.classList.toggle('bad', !(metrics.targets_online > 0));
  }
  if (advanced && page === 'config') {
    const d = $('#diag-box');
    if (d) d.textContent = diagText();
  }
  if (advanced && page === 'mapping') {
    // per-row live readouts only; full rebuild happens on structural change
  }
}

function diagText() {
  const m = metrics;
  const load = Array.isArray(m.load_avg) ? m.load_avg.map((v) => v.toFixed(2)).join(' ') : '–';
  return `ws   /ws        open · ${m.source_fps ?? 0} fps in · ${m.output_fps ?? 0} fps out\n`
    + `tv   ${cfg.source.tv_ip || '(not set)'} ${m.tv_state || '?'} · ${m.source_latency_ms ?? '–'} ms · ${m.failed_polls ?? 0} failed polls\n`
    + `wled ${(cfg.output.targets[0] || {}).host || '(not set)'} ${metrics.targets_online ? 'ok' : 'unreachable'}\n`
    + `mqtt ${cfg.mqtt.enabled ? (metrics.mqtt && metrics.mqtt.connected ? 'connected' : 'connecting') : 'disabled'}\n`
    + `cpu  ${m.cpu_percent ?? 0}% of 1 core · ${m.cpu_count ?? '?'} core${m.cpu_count === 1 ? '' : 's'} available · load ${load}`;
}

/* ================= SIMPLE VIEW ================= */

function renderSimple() {
  const on = get('mode') !== 'off';
  const sw = $('#power-switch');
  sw.classList.toggle('on', on);
  $('#power-label').textContent = on ? 'On' : 'Off';

  const bright = Math.round((cfg.colour.brightness || 0) * 100);
  $('#bright-label').textContent = bright + '%';
  // Computed here, not read from the last metrics push: the whole point is
  // that this updates the instant brightness or auto-dim changes, not once
  // the next periodic websocket tick happens to arrive.
  const level = dimmingLevelAt(Date.now(), cfg.dimming);
  const eff = Math.round(level * bright);
  $('#bright-eff').textContent = cfg.dimming.enabled ? `→ ${eff}% out` : '';
  wireSlider($('#bright-slider'), $('#bright-thumb'), $('#bright-fill'), 34,
    () => bright,
    (pct) => { $('#bright-label').textContent = pct + '%'; set('colour.brightness', pct / 100); });

  $('#brightness-block').classList.toggle('dim', !on);

  const dim = $('#autodim-toggle');
  dim.classList.toggle('on', !!cfg.dimming.enabled);

  renderLive();
}

function wireStaticControls() {
  $('#power-switch').addEventListener('click', () => { set('mode', get('mode') === 'off' ? 'ambilight' : 'off', true); renderSimple(); });
  $('#autodim-toggle').addEventListener('click', () => {
    set('dimming.enabled', !cfg.dimming.enabled, true); renderSimple();
    if (advanced && page === 'dim') renderPage();
  });
  $('#theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme')
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('ambiwled.theme', next);
  });
  $('#open-advanced').addEventListener('click', () => { setAdvanced(true); });
  $('#back-to-simple').addEventListener('click', () => { setAdvanced(false); });
  wireConfirmDialog();
  wireWizardFooter();
  $('#import-file').addEventListener('change', onImportFile);
}

function setAdvanced(on) {
  advanced = on;
  localStorage.setItem('ambiwled.mode', on ? 'advanced' : 'simple');
  $('#simple-view').hidden = on || !!wiz;
  $('#advanced-view').hidden = !on || !!wiz;
  // Mode (on/off, ambilight/preset) is editable from Advanced's Colour page
  // too - the Simple view has to catch up on return, not just when it was
  // the one making the change.
  renderSimple();
  if (on) { renderTabs(); renderPage(); }
}

/* ================= ADVANCED SHELL ================= */

function renderTabs() {
  const bar = $('#tab-bar');
  bar.innerHTML = '';
  for (const [id, label] of TABS) {
    const b = el('button', {
      class: 'tab-btn' + (page === id ? ' active' : ''),
      onclick: () => { page = id; renderTabs(); renderPage(); },
    }, label);
    bar.append(b);
  }
}

function renderPage() {
  const body = $('#page-body');
  body.innerHTML = '';
  const renderers = {
    home: pageHome, colour: pageColour, dim: pageDim, source: pageSource,
    mapping: pageMapping, output: pageOutput,
    integrations: pageIntegrations, config: pageConfig,
  };
  (renderers[page] || pageHome)(body);
  renderLive();
}

/* -- Home: live summary -- */

function pageHome(body) {
  body.append(el('div', { class: 'page' },
    el('div', { class: 'ambient-strip' },
      el('div', { class: 'caption' }, 'live · one websocket')),
    el('div', { class: 'summary-list', id: 'summary-list' }),
    el('div', { class: 'field-hint' }, 'The preview keeps streaming while you move between these pages — one socket, never renegotiated.'),
    el('div', { style: 'height:1px;background:var(--color-divider)' }),
    el('div', {}, el('div', { class: 'section-title' }, 'Mode'), el('div', { id: 'mode-wrap' })),
  ));
  renderSummaryRows();
  renderModeSwitch();
}
function renderModeSwitch() {
  const wrap = $('#mode-wrap');
  if (!wrap) return;
  wrap.innerHTML = '';
  wrap.style.cssText = 'display:flex;flex-direction:column;gap:8px';
  const modes = [
    ['ambilight', 'Follow the TV', 'Colour comes from what is on screen'],
    ['preset', 'Hold a WLED preset', 'Leaves the strip on whatever WLED is doing'],
    ['off', 'Off', 'Nothing is sent to the controller'],
  ];
  for (const [id, label, hint] of modes) {
    wrap.append(el('button', {
      class: 'option-btn' + (cfg.mode === id ? ' active' : ''),
      onclick: () => { set('mode', id, true); renderPage(); renderSimple(); },
    }, el('span', { class: 'dot' }), el('span', { class: 'label' },
      el('span', { class: 'name' }, label), el('span', { class: 'hint' }, hint))));
  }
}
function renderSummaryRows() {
  const list = $('#summary-list');
  if (!list) return;
  const total = presentSlots().reduce((n, s) => n + edgeBySlot(s).pixel_count, 0);
  const rows = [
    ['Mode', ({ ambilight: 'Follow the TV', preset: 'Hold a WLED preset', off: 'Off' })[cfg.mode]],
    ['Colour strength', (cfg.colour.saturation).toFixed(2) + '×'],
    ['Frames', `${cfg.source.poll_hz} fps in → ${cfg.frames.output_fps} out`],
    ['Brightness', Math.round(cfg.colour.brightness * 100) + '% master'],
    ['Auto-dim', cfg.dimming.enabled ? `On · ${Math.round(cfg.dimming.day_level * 100)}% day / ${Math.round(cfg.dimming.night_level * 100)}% night` : 'Off'],
    ['Ceiling', `${presentSlots().length} sides · ${total} LEDs`],
    ['MQTT', cfg.mqtt.enabled ? 'Enabled' : 'Off'],
  ];
  list.innerHTML = '';
  for (const [k, v] of rows) {
    list.append(el('div', { class: 'summary-row' }, el('span', { class: 'k' }, k), el('span', { class: 'v' }, v)));
  }
}

/* -- Colour -- */

function pageColour(body) {
  const page1 = el('div', { class: 'page' },
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'Colour strength'),
        el('span', { class: 'value', id: 'strength-label' })),
      el('div', { class: 'slider small', id: 'strength-track' },
        el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'strength-fill', style: 'background:linear-gradient(90deg,#8d8d8d,#e2557a 55%,#ff8a3d)' })),
        el('div', { class: 'thumb', id: 'strength-thumb' })),
      el('div', { style: 'display:flex;justify-content:space-between;font-size:12px;opacity:.5;margin-top:7px' },
        el('span', {}, 'Grey'), el('span', {}, 'As filmed'), el('span', {}, 'Vivid'))),
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'Black floor'),
        el('span', { class: 'value', id: 'floor-label' })),
      el('div', { class: 'slider small', id: 'floor-track' },
        el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'floor-fill', style: 'background:linear-gradient(90deg,#2b2622,var(--color-accent-2))' })),
        el('div', { class: 'thumb', id: 'floor-thumb' })),
      el('div', { class: 'field-hint' }, 'Stops very dark scenes flickering on some strips.')),
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'Zone blending'),
        el('span', { class: 'value', id: 'zoneblend-label' })),
      el('div', { class: 'slider small', id: 'zoneblend-track' },
        el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'zoneblend-fill', style: 'background:linear-gradient(90deg,#8d8d8d,var(--color-accent))' })),
        el('div', { class: 'thumb', id: 'zoneblend-thumb' })),
      el('div', { style: 'display:flex;justify-content:space-between;font-size:12px;opacity:.5;margin-top:7px' },
        el('span', {}, 'Blocky'), el('span', {}, 'Smooth')),
      el('div', { class: 'field-hint' }, "How far each LED's colour is drawn from the TV's own zones. Low: each zone shows as a hard block, exactly as the TV sends it. High: blends further across neighbouring zones for a softer look.")),
  );
  body.append(page1);

  const strengthPct = Math.round((cfg.colour.saturation || 1) * 50);
  $('#strength-label').textContent = (cfg.colour.saturation).toFixed(2) + '×';
  wireSlider($('#strength-track'), $('#strength-thumb'), $('#strength-fill'), 30,
    () => strengthPct,
    (pct) => { $('#strength-label').textContent = (pct / 50).toFixed(2) + '×'; set('colour.saturation', pct / 50); });

  const floorPct = Math.round(((cfg.colour.black_floor || 0) / 255) * 100);
  $('#floor-label').textContent = floorPct + '%';
  wireSlider($('#floor-track'), $('#floor-thumb'), $('#floor-fill'), 30,
    () => floorPct,
    (pct) => { $('#floor-label').textContent = pct + '%'; set('colour.black_floor', Math.round((pct / 100) * 255)); });

  const zoneBlendPct = Math.round(((cfg.mapping.zone_blend ?? 1) / 4) * 100);
  $('#zoneblend-label').textContent = (cfg.mapping.zone_blend ?? 1).toFixed(2) + '×';
  wireSlider($('#zoneblend-track'), $('#zoneblend-thumb'), $('#zoneblend-fill'), 30,
    () => zoneBlendPct,
    (pct) => {
      const v = Math.round((pct / 100) * 4 * 20) / 20; // snap to 0.05 steps
      $('#zoneblend-label').textContent = v.toFixed(2) + '×';
      set('mapping.zone_blend', v, true);
    });
}

/* -- Auto-dim -- */

function pageDim(body) {
  const wrap = el('div', { class: 'page' },
    el('div', { class: 'toggle-row' },
      el('span', { class: 'name' }, 'Dim by the sun'),
      el('button', { class: 'toggle-switch' + (cfg.dimming.enabled ? ' on' : ''), id: 'dim-toggle-adv' }, el('span', { class: 'knob' }))),
    el('div', { id: 'dim-body', style: cfg.dimming.enabled ? '' : 'opacity:.4' }),
  );
  body.append(wrap);
  $('#dim-toggle-adv').addEventListener('click', () => { set('dimming.enabled', !cfg.dimming.enabled, true); renderPage(); renderSimple(); });

  const dimBody = $('#dim-body');
  dimBody.append(
    el('div', { class: 'curve-card' },
      el('div', { class: 'curve-head' }, el('span', { class: 'label' }, 'Next 24 hours'), el('span', { class: 'now', id: 'curve-now' })),
      el('div', { class: 'curve-wrap', id: 'curve-wrap' }),
      el('div', { class: 'curve-ticks', id: 'curve-ticks' })),
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'Day level'), el('span', { class: 'value', id: 'day-label' })),
      el('div', { class: 'slider small', id: 'day-track' }, el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'day-fill' })), el('div', { class: 'thumb', id: 'day-thumb' })),
      el('div', { class: 'field-hint' }, 'The top of the arch, at solar noon.')),
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'Night level'), el('span', { class: 'value', id: 'night-label' })),
      el('div', { class: 'slider small', id: 'night-track' }, el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'night-fill' })), el('div', { class: 'thumb', id: 'night-thumb' })),
      el('div', { class: 'field-hint' }, 'The floor, from sunset to sunrise.')),
    el('div', { class: 'field' }, el('label', {}, 'Location'),
      el('input', { class: 'input', id: 'loc-input', placeholder: 'Paste coordinates or a map link', value: locationText() })),
    el('div', { class: 'row-actions', style: 'display:flex;gap:8px' },
      el('button', { class: 'pill-btn soft', id: 'use-location' }, 'Use my location')),
    el('div', { class: 'field-hint', id: 'sun-line' }),
    el('div', { class: 'note-card' }, el('span', { id: 'master-note' })),
    el('div', { class: 'field-hint' }, 'The level is recalculated every minute and follows the curve, so it never steps — there is nothing to schedule.'),
  );

  const dayPct = Math.round((cfg.dimming.day_level || 1) * 100);
  const nightPct = Math.round((cfg.dimming.night_level || 0) * 100);
  $('#day-label').textContent = dayPct + '%';
  $('#night-label').textContent = nightPct + '%';
  wireSlider($('#day-track'), $('#day-thumb'), $('#day-fill'), 30,
    () => Math.round((cfg.dimming.day_level || 1) * 100),
    (pct) => { const v = Math.max(pct, Math.round(cfg.dimming.night_level * 100) + 1) / 100; $('#day-label').textContent = Math.round(v * 100) + '%'; set('dimming.day_level', v); drawDimChart(); });
  wireSlider($('#night-track'), $('#night-thumb'), $('#night-fill'), 30,
    () => Math.round((cfg.dimming.night_level || 0) * 100),
    (pct) => { const v = Math.min(pct, Math.round(cfg.dimming.day_level * 100) - 1) / 100; $('#night-label').textContent = Math.round(v * 100) + '%'; set('dimming.night_level', v); drawDimChart(); });

  $('#use-location').addEventListener('click', () => useMyLocation($('#use-location')));
  const locInput = $('#loc-input');
  locInput.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Enter' && ev.key !== 'Go' && ev.keyCode !== 13) return;
    ev.preventDefault(); submitLocationText(locInput);
  });
  locInput.addEventListener('change', () => submitLocationText(locInput));

  updateMasterNote();
  drawDimChart();
}

/** The broker field shows/accepts "host:port" together; the schema keeps
    them as separate fields, so this is the one place that splits it. */
function mqttHostValue() { return cfg.mqtt.host ? `${cfg.mqtt.host}:${cfg.mqtt.port}` : ''; }
function setMqttHostPort(text) {
  const s = String(text).trim();
  const i = s.lastIndexOf(':');
  const port = i > 0 ? parseInt(s.slice(i + 1), 10) : NaN;
  const host = i > 0 && !Number.isNaN(port) ? s.slice(0, i) : s;
  cfg.mqtt.host = host;
  const patch = { mqtt: { host } };
  if (!Number.isNaN(port)) { cfg.mqtt.port = port; patch.mqtt.port = port; }
  pushConfig(patch, true);
}

function locationText() {
  const lat = cfg.dimming.latitude, lon = cfg.dimming.longitude;
  if (!lat && !lon) return '';
  return `${lat}, ${lon}`;
}
function updateMasterNote() {
  const n = $('#master-note');
  if (!n) return;
  const level = dimmingLevelAt(Date.now(), cfg.dimming);
  const bright = Math.round((cfg.colour.brightness || 1) * 100);
  const eff = Math.round(level * bright);
  n.textContent = cfg.dimming.enabled
    ? `Day and night levels are the schedule. The Brightness dial on the Home page is a master on top of it — at ${bright}% the ceiling is putting out ${eff}% right now, against a scheduled ${Math.round(level * 100)}%.`
    : `With dimming off, the Brightness dial on the Home page is the only level — the ceiling sits at ${bright}% all day.`;
  const sun = $('#sun-line');
  if (sun && metrics.dimming) {
    if (metrics.dimming.sunrise && metrics.dimming.sunset) {
      const fmt = (iso) => new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      sun.textContent = `Sunrise ${fmt(metrics.dimming.sunrise)} · sunset ${fmt(metrics.dimming.sunset)}.`;
    } else if (metrics.dimming.polar) {
      sun.textContent = metrics.dimming.polar === 'polar_day' ? 'The sun never sets here right now.' : 'The sun never rises here right now.';
    }
  }
}

/* -- Source -- */

function pageSource(body) {
  body.append(el('div', { class: 'page' },
    el('div', { class: 'status-pill' },
      el('span', { class: 'dot', id: 'tv-dot' }),
      el('span', { class: 'body' }, el('span', { class: 'name' }, cfg.source.tv_ip ? 'Television' : 'No TV set yet'),
        el('span', { class: 'meta', id: 'source-meta' }))),
    el('div', { class: 'field' }, el('label', {}, 'TV address'),
      el('input', { class: 'input', id: 'tv-ip', value: cfg.source.tv_ip, placeholder: '192.168.1.44' })),
    el('div', { class: 'field-hint' }, 'Saved when you tap away from the field, never mid-typing.'),
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'How often we ask the TV'), el('span', { class: 'value' }, cfg.source.poll_hz + ' fps')),
      el('div', { class: 'field-hint' }, pollHint()),
      el('div', { class: 'chip-row', id: 'poll-chips' })),
    el('div', { class: 'field-hint', id: 'poll-warn' }),
  ));

  const tvIp = $('#tv-ip');
  tvIp.addEventListener('change', () => { set('source.tv_ip', tvIp.value.trim(), true); pageSourceMeta(); });

  const chips = $('#poll-chips');
  for (const v of [10, 15, 20, 25, 30]) {
    chips.append(el('button', {
      class: 'chip-btn' + (cfg.source.poll_hz === v ? ' active' : ''),
      onclick: () => {
        const mult = Math.max(1, Math.min(Math.floor(120 / v), Math.round(cfg.frames.output_fps / v)));
        set('source.poll_hz', v);
        set('frames.output_fps', mult * v, true);
        renderPage();
      },
    }, v + ' fps'));
  }
  pollWarn();
  pageSourceMeta();
}
function pollHint() { return 'Frames a second read off the TV. Frame generation fills the gap between this and what the strip gets.'; }
function pollWarn() {
  const w = $('#poll-warn');
  if (!w) return;
  w.textContent = cfg.source.poll_hz >= 25
    ? 'Fast, but not every TV keeps up — if the log starts showing failed polls, come back down to 20.'
    : cfg.source.poll_hz <= 10
      ? 'Very light on the TV. Colour changes will visibly step unless frame generation smooths it.'
      : '20 is the safe default — almost every set manages it.';
}
function pageSourceMeta() {
  const dot = $('#tv-dot');
  if (dot) dot.classList.toggle('bad', metrics.tv_state !== 'streaming');
  renderLive();
}

/* -- Mapping -- */

function pageMapping(body) {
  const wrap = el('div', { class: 'page' },
    el('div', {},
      el('div', { class: 'section-title' }, 'Which sides have strip'),
      el('div', { class: 'field-hint', id: 'sides-summary' }),
      el('div', { class: 'two-col', id: 'side-toggles', style: 'margin-top:10px' })),
    el('div', { style: 'height:1px;background:var(--color-divider)' }),
    el('div', { class: 'field-hint', id: 'total-label' }),
    el('div', { id: 'edge-rows', style: 'display:flex;flex-direction:column;gap:12px' }),
  );
  body.append(wrap);
  renderSideToggles();
  renderTotalLabel();
  renderEdgeRows();
}
function renderSideToggles() {
  const box = $('#side-toggles');
  box.innerHTML = '';
  for (const slot of SLOTS) {
    const present = !!edgeBySlot(slot);
    box.append(el('button', {
      class: 'side-btn' + (present ? ' active' : ''),
      onclick: () => { toggleSlot(slot); renderPage(); renderPreviewFromPixels(); },
    }, el('span', { class: 'dot', style: present ? `background:${SLOT_COLOUR[slot]};box-shadow:0 0 9px ${SLOT_COLOUR[slot]}` : '' }),
       el('span', { class: 'name' }, SLOT_LABEL[slot])));
  }
}
function renderTotalLabel() {
  const total = presentSlots().reduce((n, s) => n + edgeBySlot(s).pixel_count, 0);
  const s1 = $('#sides-summary'), t1 = $('#total-label');
  if (s1) s1.textContent = presentSlots().length === 1 ? 'One run of strip. Everything else stays dark.'
    : `${presentSlots().length} sides · ${presentSlots().map((s) => SLOT_LABEL[s]).join(' · ')}`;
  if (t1) t1.textContent = `${total} LEDs, packed head to tail in this order — no gaps possible. `
    + 'Use ↑↓ on a card below to match the order your strip is actually wired in.';
}
function renderEdgeRows() {
  const box = $('#edge-rows');
  box.innerHTML = '';
  const slots = presentSlots();
  slots.forEach((slot, i) => {
    const e = edgeBySlot(slot);
    const live = e.source !== 'off';
    const card = el('div', { class: 'edge-card' },
      el('div', { class: 'head' },
        el('span', { class: 'dot', style: `background:${SLOT_COLOUR[slot]};box-shadow:0 0 9px ${SLOT_COLOUR[slot]}` }),
        el('span', { class: 'name' }, SLOT_LABEL[slot]),
        el('div', { class: 'reorder', style: 'display:flex;gap:4px' },
          el('button', { class: 'round-btn', style: `width:34px;height:34px;font-size:14px;${i === 0 ? 'opacity:.3' : ''}`, disabled: i === 0, onclick: () => { moveEdge(slot, -1); renderPage(); } }, '↑'),
          el('button', { class: 'round-btn', style: `width:34px;height:34px;font-size:14px;${i === slots.length - 1 ? 'opacity:.3' : ''}`, disabled: i === slots.length - 1, onclick: () => { moveEdge(slot, 1); renderPage(); } }, '↓')),
        el('button', { class: 'pill-btn', onclick: () => flashSlot(slot) }, 'Identify')),
      el('div', { class: 'edge-row' },
        el('span', { class: 'label' }, `LEDs · from ${e.pixel_start}`),
        el('button', { class: 'round-btn', onclick: () => { editEdge(slot, { pixel_count: Math.max(1, e.pixel_count - 1) }); renderPage(); } }, '−'),
        el('span', { class: 'count-value' }, e.pixel_count),
        el('button', { class: 'round-btn', onclick: () => { editEdge(slot, { pixel_count: e.pixel_count + 1 }); renderPage(); } }, '+')),
      el('div', { class: 'edge-row' },
        el('span', { class: 'label' }, 'Colour comes from'),
        el('button', {
          class: 'pill-btn', onclick: () => {
            const i = SOURCE_OPTIONS.findIndex((o) => o[0] === e.source);
            const next = SOURCE_OPTIONS[(i + 1) % SOURCE_OPTIONS.length][0];
            const patch = { source: next };
            if (next === 'synth_gradient' && !e.synth_from) { patch.synth_from = ['right', -1]; patch.synth_to = ['left', 0]; }
            if (next === 'mirror_top' && !e.mirror_of) patch.mirror_of = 'front';
            editEdge(slot, patch); renderPage();
          },
        }, (SOURCE_OPTIONS.find((o) => o[0] === e.source) || SOURCE_OPTIONS[0])[1])),
    );
    if (live) {
      card.append(
        el('div', { class: 'edge-row' },
          el('span', { class: 'label' }, 'Strip direction'),
          el('button', { class: 'pill-btn', onclick: () => { editEdge(slot, { reversed: !e.reversed }); renderPreviewFromPixels(); } }, 'Flip')),
        el('div', {},
          el('div', { style: 'display:flex;justify-content:space-between;font-size:13px;opacity:.6;margin-bottom:7px' },
            el('span', {}, 'Brightness trim'), el('span', { id: `trim-label-${slot}` }, (e.brightness || 1).toFixed(2) + '×')),
          el('div', { class: 'slider small', id: `trim-track-${slot}` },
            el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: `trim-fill-${slot}`, style: 'background:var(--color-accent-2);opacity:.65' })),
            el('div', { class: 'thumb', id: `trim-thumb-${slot}` }))),
      );
    } else {
      card.append(el('div', { class: 'dark-note' }, `These ${e.pixel_count} LEDs stay dark, so there is nothing to aim or trim. They still count towards the total.`));
    }
    box.append(card);
    if (live) {
      const pct = Math.round(((e.brightness || 1) / 2) * 100);
      wireSlider($(`#trim-track-${slot}`), $(`#trim-thumb-${slot}`), $(`#trim-fill-${slot}`), 26,
        () => pct,
        (p) => { const v = (p / 100) * 2; $(`#trim-label-${slot}`).textContent = v.toFixed(2) + '×'; editEdge(slot, { brightness: v }); });
    }
  });
}

/* -- Output -- */

function pageOutput(body) {
  const target = cfg.output.targets[0] || { host: '', port: 21324, http_port: 80, enabled: false };
  body.append(el('div', { class: 'page' },
    el('div', { class: 'field' }, el('label', {}, 'WLED controller'),
      el('input', { class: 'input', id: 'wled-host', value: target.host, placeholder: 'wled-ceiling.local' })),
    el('div', { class: 'status-pill' },
      el('span', { class: 'dot', id: 'output-dot' }),
      el('span', { class: 'body' }, el('span', { class: 'name' }, 'Reachable'), el('span', { class: 'meta', id: 'output-summary' }))),
    el('button', { class: 'cta-btn', onclick: () => confirmPush() }, 'Push segments to the controller'),
    el('div', { class: 'field-hint' }, 'One segment per side, front to back — so WLED\'s own effects line up with your ceiling. Overwrites the layout stored on the device.'),
    el('div', { style: 'height:1px;background:var(--color-divider)' }),
  ));
  const hostInput = $('#wled-host');
  hostInput.addEventListener('change', () => {
    const targets = cfg.output.targets.slice();
    if (!targets.length) targets.push({ host: '', port: 21324, http_port: 80, pixel_offset: 0, pixel_count: cfg.led.count, enabled: false });
    targets[0] = Object.assign({}, targets[0], { host: hostInput.value.trim(), enabled: !!hostInput.value.trim(), pixel_count: cfg.led.count });
    set('output.targets', targets, true);
  });
  pageFrameGeneration(body.querySelector('.page'));
  renderLive();
}
function confirmPush() {
  confirmAction('Push segments?',
    `The segment layout on the WLED controller will be overwritten with these ${presentSlots().length} sides.`,
    'Push', false, async () => {
      try {
        const r = await fetch('/api/wled/segments?mode=front_to_back', { method: 'POST' });
        const data = await r.json();
        toast(data.ok ? 'Segments pushed' : 'Could not reach the controller', !data.ok);
      } catch (err) { toast('Could not reach the controller', true); }
    });
}

/* -- Frame generation (part of the Output page) -- */

function pageFrameGeneration(pageEl) {
  const poll = cfg.source.poll_hz;
  const mults = [];
  for (let k = 1; k * poll <= 120; k++) mults.push(k * poll);
  const cur = Math.max(0, mults.indexOf(cfg.frames.output_fps));
  const madeUp = Math.max(0, Math.round(cfg.frames.output_fps / poll) - 1);

  pageEl.append(
    el('div', { class: 'section-title' }, 'Frame generation'),
    el('div', {},
      el('div', { class: 'field-title' }, el('span', { class: 'name' }, 'Frames sent to the strip'), el('span', { class: 'value' }, cfg.frames.output_fps + ' fps')),
      el('div', { class: 'field-hint' }, madeUp === 0
        ? `Straight through: the ${poll} frames a second your TV gives us are the ${poll} frames the strip gets.`
        : `${poll} fps in → ${cfg.frames.output_fps} fps out, so ${madeUp === 1 ? 'one frame is' : madeUp + ' frames are'} made up between every real one.`),
      el('div', { class: 'slider', id: 'fps-track' }, el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'fps-fill' })), el('div', { class: 'thumb', id: 'fps-thumb' })),
      el('div', { style: 'display:flex;justify-content:space-between;font-size:13px;opacity:.55;margin-top:8px' },
        el('span', {}, `Coarse · ${poll} fps`), el('span', {}, 'Smooth')),
      el('div', { class: 'fps-hist' },
        el('div', { class: 'fps-bars', id: 'fps-bars' }),
        el('div', { class: 'fps-legend' },
          el('span', { class: 'sw' }, el('i', { style: 'background:var(--color-accent)' }), 'from the TV'),
          el('span', { class: 'sw' }, el('i', { style: 'background:var(--color-accent-2)' }), 'made up in between'))),
      el('div', { class: 'field-hint' }, madeUp === 0
        ? `Every step in the colour is a real step in the picture. On a ${poll} fps feed you may see it march.`
        : cfg.frames.output_fps >= 90 ? 'Motion looks continuous. Costs the most CPU on the box.' : 'Smoother than the feed without much extra work.')),
    el('div', { class: 'callout' },
      el('svg', { width: '18', height: '18', viewBox: '0 0 24 24', fill: 'none', stroke: 'var(--color-accent)', 'stroke-width': '2.75', 'stroke-linecap': 'round' }),
      el('span', {}, "Turn WLED's own crossfade off while AmbiWled is streaming — two smoothing stages in a row turn the picture to mush.")),
  );

  const pct = mults.length > 1 ? Math.round((cur / (mults.length - 1)) * 100) : 0;
  wireSlider($('#fps-track'), $('#fps-thumb'), $('#fps-fill'), 32,
    () => pct,
    (p) => {
      const idx = Math.round((p / 100) * (mults.length - 1));
      set('frames.output_fps', mults[idx], true);
      renderPage();
    });

  const bars = $('#fps-bars');
  for (let i = 0; i < 24; i++) {
    const real = madeUp === 0 || i % (madeUp + 1) === 0;
    bars.append(el('div', { class: 'bar' + (real ? ' real' : ''), style: `height:${real ? 100 : 52}%` }));
  }
}

/* -- Integrations (MQTT) -- */

function pageIntegrations(body) {
  const on = !!cfg.mqtt.enabled;
  body.append(el('div', { class: 'page' },
    el('div', { class: 'toggle-row' },
      el('span', { class: 'name' }, 'Publish to MQTT'),
      el('button', { class: 'toggle-switch' + (on ? ' on' : ''), onclick: () => { set('mqtt.enabled', !on, true); renderPage(); } },
        el('span', { class: 'knob' }))),
    el('div', { id: 'mqtt-body', style: on ? '' : 'opacity:.4' }),
  ));
  if (!on) return;
  const b = $('#mqtt-body');
  b.append(
    el('div', { class: 'field' }, el('label', {}, 'Broker address'),
      el('input', { class: 'input', id: 'mqtt-host', value: mqttHostValue(), placeholder: '192.168.1.10:1883' }),
      el('div', { class: 'field-hint', id: 'mqtt-test-row' })),
    el('div', { class: 'field' }, el('label', {}, 'Username'), el('input', { class: 'input', id: 'mqtt-user', value: cfg.mqtt.username })),
    el('div', { class: 'field' }, el('label', {}, 'Password'),
      el('div', { style: 'display:flex;gap:8px;align-items:center' },
        el('input', { class: 'input', id: 'mqtt-pass', type: 'password', placeholder: pwEdit ? 'Enter new password' : '••••••••••', readonly: pwEdit ? undefined : 'readonly', style: `flex:1;${pwEdit ? '' : 'opacity:.6'}` }),
        el('button', { class: 'pill-btn', onclick: () => { pwEdit = !pwEdit; renderPage(); } }, pwEdit ? 'Cancel' : 'Replace')),
      el('div', { class: 'field-hint' }, 'Write-only. The server never sends it back to this page.')),
    el('div', { class: 'field-hint' }, "If you already run Home Assistant's own WLED integration, that's the fuller remote — this is for setups without it."),
  );
  const host = $('#mqtt-host'), user = $('#mqtt-user'), pass = $('#mqtt-pass');
  host.addEventListener('change', () => setMqttHostPort(host.value));
  user.addEventListener('change', () => set('mqtt.username', user.value.trim(), true));
  if (pwEdit) pass.addEventListener('change', () => { set('mqtt.password', pass.value, true); pwEdit = false; renderPage(); });
  testMqttInline(host.value);
}
async function testMqttInline(hostval) {
  const row = $('#mqtt-test-row');
  if (!row || !hostval) { if (row) row.textContent = ''; return; }
  row.textContent = 'Checking…';
  const [host, portStr] = String(hostval).split(':');
  try {
    const r = await fetch('/api/mqtt/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host, port: parseInt(portStr || '1883', 10), username: cfg.mqtt.username, password: '********' }),
    });
    const data = await r.json();
    row.textContent = data.ok ? 'Reachable' : data.reason === 'auth' ? 'Broker refused that login' : "Can't reach that broker";
    row.style.color = data.ok ? '' : 'var(--err)';
  } catch (err) { row.textContent = ''; }
}

/* -- Config / system -- */

function pageConfig(body) {
  body.append(el('div', { class: 'page' },
    el('div', { style: 'display:flex;flex-direction:column;gap:10px' },
      el('a', { class: 'pill-btn block', href: '/api/config?download=1', download: 'ambiwled-config.json', style: 'text-decoration:none;display:block' }, 'Export settings as JSON'),
      el('button', { class: 'pill-btn block', onclick: () => confirmImport() }, 'Import settings…'),
      el('button', { class: 'pill-btn block', onclick: () => startWizard() }, 'Run setup again')),
    el('div', {}, el('div', { class: 'section-title' }, 'Diagnostics'), el('div', { class: 'diag-box', id: 'diag-box' }, diagText())),
    el('div', { class: 'danger-box' },
      el('div', { class: 'title' }, 'Reset to defaults'),
      el('div', { style: 'font-size:13px;opacity:.7;line-height:1.5' }, "Wipes the TV pairing, the mapping and every setting. You'd run setup again from scratch."),
      el('button', { class: 'cta-btn danger', onclick: () => confirmReset() }, 'Reset AmbiWled')),
  ));
}
function confirmReset() {
  confirmAction('Reset to defaults?', "The TV pairing, the mapping and every setting go back to factory. You'd run setup again.", 'Reset', true, async () => {
    const r = await fetch('/api/config/reset', { method: 'POST' });
    const data = await r.json();
    cfg = data.config;
    renderAll();
    toast('Reset to defaults');
  });
}
function confirmImport() {
  confirmAction('Import settings?', 'This replaces the whole config with the one in the file. It cannot be undone.', 'Choose file', true, () => {
    $('#import-file').click();
  });
}
async function onImportFile(ev) {
  const file = ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const text = await file.text();
    const body = JSON.parse(text);
    const r = await fetch('/api/config/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await r.json();
    if (!r.ok) { toast((data.errors || ['import failed'])[0], true); return; }
    cfg = data.config;
    renderAll();
    toast('Imported');
  } catch (err) {
    toast('Could not read that file: ' + err.message, true);
  }
}

/* ---------- location (Use my location / paste coordinates) ---------- */

function locationAvailable() {
  return !!(navigator.geolocation && (window.isSecureContext
    || ['localhost', '127.0.0.1'].includes(location.hostname)));
}
function applyLocation(lat, lon, how) {
  lat = Math.round(lat * 10000) / 10000;
  lon = Math.round(lon * 10000) / 10000;
  set('dimming.latitude', lat);
  set('dimming.longitude', lon, true);
  const input = $('#loc-input');
  if (input) input.value = `${lat}, ${lon}`;
  toast(`Location set to ${lat}, ${lon}${how ? ' (' + how + ')' : ''}`);
  fetch('/api/state').then((r) => r.json()).then((d) => { metrics = Object.assign({}, metrics, d.metrics); updateMasterNote(); drawDimChart(); }).catch(() => {});
}
function looksLikeUrl(text) { return /^https?:\/\//i.test(String(text).trim()); }
function parseCoordinates(text) {
  if (!text) return null;
  const s = decodeURIComponent(String(text)).replace(/\s+/g, ' ').trim();
  const patterns = [
    /!3d(-?\d{1,3}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)/,
    /#map=\d+\/(-?\d{1,3}(?:\.\d+)?)\/(-?\d{1,3}(?:\.\d+)?)/,
    /[?&]m?lat=(-?\d{1,3}(?:\.\d+)?)[^0-9-]+m?lon=(-?\d{1,3}(?:\.\d+)?)/,
    /[?&]q=(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)/,
    /[@](-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)/,
    /(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)/,
  ];
  for (const re of patterns) {
    const m = s.match(re);
    if (m) {
      const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
      if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon };
    }
  }
  const hemi = s.match(/(-?\d{1,3}(?:\.\d+)?)\s*([NSns])[ ,;]*(-?\d{1,3}(?:\.\d+)?)\s*([EWew])/);
  if (hemi) {
    const lat = parseFloat(hemi[1]) * (hemi[2].toUpperCase() === 'S' ? -1 : 1);
    const lon = parseFloat(hemi[3]) * (hemi[4].toUpperCase() === 'W' ? -1 : 1);
    if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon };
  }
  return null;
}
async function resolveLinkAndApply(box, text) {
  box.disabled = true;
  const original = box.value;
  box.value = 'Resolving link…';
  try {
    const r = await fetch('/api/resolve-link', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: text.trim() }) });
    const data = await r.json();
    if (!r.ok || !data.resolved_url) throw new Error(data.error || 'could not resolve the link');
    const hit = parseCoordinates(data.resolved_url);
    if (!hit) throw new Error('resolved, but no coordinates were in it');
    applyLocation(hit.lat, hit.lon, 'pasted, resolved');
  } catch (err) {
    toast('Could not resolve that link: ' + err.message, true);
    box.value = original;
  } finally {
    box.disabled = false;
  }
}
function submitLocationText(box) {
  const text = box.value;
  const hit = parseCoordinates(text);
  if (hit) { applyLocation(hit.lat, hit.lon, 'pasted'); return; }
  if (looksLikeUrl(text)) { resolveLinkAndApply(box, text); return; }
  toast('Could not find coordinates in that', true);
}
function useMyLocation(btn) {
  if (!locationAvailable()) {
    toast('Browser location needs https or localhost on this page — paste coordinates instead', true);
    return;
  }
  btn.disabled = true; btn.textContent = 'Locating…';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      applyLocation(pos.coords.latitude, pos.coords.longitude, `±${Math.round(pos.coords.accuracy)} m`);
      btn.disabled = false; btn.textContent = 'Use my location';
    },
    (err) => {
      const why = err.code === 1 ? 'permission denied' : err.code === 2 ? 'position unavailable' : 'timed out';
      toast('Could not get location: ' + why, true);
      btn.disabled = false; btn.textContent = 'Use my location';
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 },
  );
}

/* ---------- auto-dim 24h chart: a line-for-line port of daylight.py ---------- */

const ZENITH = (-0.833) * Math.PI / 180;
const OBLIQUITY = 23.4397 * Math.PI / 180;
const J2000 = 2451545.0;
const DAY_MS = 86400000;
function rad(d) { return d * Math.PI / 180; }
function deg(r) { return r * 180 / Math.PI; }
function fromJulian(j) { return (j - 2440587.5) * DAY_MS; }
function sunTimesRaw(lat, lon, dayUtcMidnightMs) {
  const n = (dayUtcMidnightMs - Date.UTC(2000, 0, 1)) / DAY_MS + 0.0008;
  const jStar = n - lon / 360.0;
  const m = rad(((357.5291 + 0.98560028 * jStar) % 360 + 360) % 360);
  const c = 1.9148 * Math.sin(m) + 0.02 * Math.sin(2 * m) + 0.0003 * Math.sin(3 * m);
  const lam = rad(((deg(m) + c + 180.0 + 102.9372) % 360 + 360) % 360);
  const jTransit = J2000 + jStar + 0.0053 * Math.sin(m) - 0.0069 * Math.sin(2 * lam);
  const declination = Math.asin(Math.sin(lam) * Math.sin(OBLIQUITY));
  const phi = rad(lat);
  const denom = Math.cos(phi) * Math.cos(declination);
  if (Math.abs(denom) < 1e-12) return { polar: null };
  const cosOmega = (Math.sin(ZENITH) - Math.sin(phi) * Math.sin(declination)) / denom;
  if (cosOmega >= 1.0) return { polar: 'polar_night' };
  if (cosOmega <= -1.0) return { polar: 'polar_day' };
  const omega = deg(Math.acos(cosOmega));
  return { sunrise: fromJulian(jTransit - omega / 360.0), sunset: fromJulian(jTransit + omega / 360.0) };
}
const _sunCache = new Map();
function sunTimesForDay(lat, lon, ms) {
  const d = new Date(ms);
  const dayStart = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const key = lat + ',' + lon + ',' + dayStart;
  if (!_sunCache.has(key)) {
    if (_sunCache.size > 60) _sunCache.clear();
    _sunCache.set(key, sunTimesRaw(lat, lon, dayStart));
  }
  return _sunCache.get(key);
}
function domeShape(s) { s = clamp(s, 0, 1); return 4.0 * s * (1.0 - s); }
function dimmingLevelAt(ms, dim) {
  if (!dim.enabled) return dim.day_level;
  const times = sunTimesForDay(dim.latitude, dim.longitude, ms);
  let frac;
  if (times.polar) frac = times.polar === 'polar_night' ? 0.0 : 1.0;
  else {
    const span = times.sunset - times.sunrise;
    frac = (span <= 0 || ms < times.sunrise || ms > times.sunset) ? 0.0 : domeShape((ms - times.sunrise) / span);
  }
  return dim.night_level + (dim.day_level - dim.night_level) * frac;
}

function drawDimChart() {
  const wrap = $('#curve-wrap');
  if (!wrap) return;
  const now = Date.now();
  const dim = cfg.dimming;
  const W = 320, H = 104;
  const pts = [];
  for (let i = 0; i <= 96; i++) {
    const t = now + i * 15 * 60000;
    const level = dimmingLevelAt(t, dim);
    const x = (i / 96) * W;
    const y = H - level * (H - 8);
    pts.push([x, y]);
  }
  const path = 'M' + pts.map((p) => p[0].toFixed(1) + ',' + p[1].toFixed(1)).join('L');
  const area = path + `L${W},${H}L0,${H}Z`;
  const dayY = (H - dim.day_level * (H - 8)).toFixed(1);
  const nightY = (H - dim.night_level * (H - 8)).toFixed(1);
  const nowLevel = dimmingLevelAt(now, dim);
  const nowY = (H - nowLevel * (H - 8)).toFixed(1);

  const svgns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(svgns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('style', 'display:block;width:100%;height:auto;overflow:visible');
  const defs = document.createElementNS(svgns, 'defs');
  const grad = document.createElementNS(svgns, 'linearGradient');
  grad.setAttribute('id', 'dimFill'); grad.setAttribute('x1', '0'); grad.setAttribute('y1', '0'); grad.setAttribute('x2', '0'); grad.setAttribute('y2', '1');
  const s1 = document.createElementNS(svgns, 'stop'); s1.setAttribute('offset', '0'); s1.setAttribute('stop-color', 'var(--color-accent)'); s1.setAttribute('stop-opacity', '0.34');
  const s2 = document.createElementNS(svgns, 'stop'); s2.setAttribute('offset', '1'); s2.setAttribute('stop-color', 'var(--color-accent)'); s2.setAttribute('stop-opacity', '0.02');
  grad.append(s1, s2); defs.append(grad); svg.append(defs);
  const mkLine = (y, colour) => { const l = document.createElementNS(svgns, 'line'); l.setAttribute('x1', '0'); l.setAttribute('y1', y); l.setAttribute('x2', W); l.setAttribute('y2', y); l.setAttribute('stroke', colour); l.setAttribute('stroke-opacity', '0.45'); l.setAttribute('stroke-dasharray', '2 5'); return l; };
  svg.append(mkLine(dayY, 'var(--color-accent)'), mkLine(nightY, 'var(--color-accent-2)'));
  const areaPath = document.createElementNS(svgns, 'path'); areaPath.setAttribute('d', area); areaPath.setAttribute('fill', 'url(#dimFill)');
  const linePath = document.createElementNS(svgns, 'path'); linePath.setAttribute('d', path); linePath.setAttribute('fill', 'none'); linePath.setAttribute('stroke', 'var(--color-accent)'); linePath.setAttribute('stroke-width', '2.5'); linePath.setAttribute('stroke-linejoin', 'round'); linePath.setAttribute('stroke-linecap', 'round');
  svg.append(areaPath, linePath);
  const dot = document.createElementNS(svgns, 'circle'); dot.setAttribute('cx', '0'); dot.setAttribute('cy', nowY); dot.setAttribute('r', '3.6'); dot.setAttribute('fill', 'var(--color-accent)');
  svg.append(dot);

  wrap.innerHTML = '';
  wrap.append(svg);
  const nowLabel = $('#curve-now');
  if (nowLabel) nowLabel.textContent = 'now ' + Math.round(nowLevel * 100) + '%';

  const ticks = $('#curve-ticks');
  if (ticks) {
    ticks.innerHTML = '';
    for (let h = 0; h <= 24; h += 6) {
      ticks.append(el('span', {}, new Date(now + h * 3600000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })));
    }
  }

  updateMasterNote();
}

/* ================= WIZARD ================= */

const WIZ_STEPS = [
  { k: 'Find your TV', t: 'Find your TV', s: 'Type the address of the television the lights should follow.' },
  { k: 'Find your lights', t: 'Find your lights', s: 'This is the little box the LED strip plugs into.' },
  { k: 'Your ceiling', t: 'Which sides have strip?', s: 'Tap every side that has LEDs. One is fine — most Ambilight builds use three.' },
  { k: 'Count the LEDs', t: '', s: 'Not sure which side this is? Tap flash and watch which one lights up.' },
  { k: 'Where you are', t: 'Where are you?', s: 'So the lights know when it gets dark outside.' },
  { k: 'Home Assistant', t: 'Home Assistant', s: 'Optional. Most people skip this.' },
  { k: "All done", t: "That's it — you're set.", s: 'Turn the TV on and the ceiling will follow it.' },
];

function startWizard() {
  wiz = { step: 0, countIdx: 0, mqttTest: '', mqttErr: '' };
  confirmState = null; renderConfirm();
  $('#simple-view').hidden = true;
  $('#advanced-view').hidden = true;
  $('#wizard').hidden = false;
  renderWizard();
}
function endWizard() {
  wiz = null;
  $('#wizard').hidden = true;
  setAdvanced(advanced);
}

function renderWizard() {
  const dots = $('#wiz-dots');
  dots.innerHTML = '';
  for (let i = 0; i < WIZ_STEPS.length; i++) dots.append(el('i', { class: i <= wiz.step ? 'done' : '' }));

  const step = WIZ_STEPS[wiz.step];
  const activeSlot = presentSlots()[Math.min(wiz.countIdx, presentSlots().length - 1)];
  const title = wiz.step === 3 ? SLOT_QUESTION[activeSlot] : step.t;
  const body = $('#wiz-body');
  body.innerHTML = '';
  body.append(
    el('div', {},
      el('div', { class: 'wiz-kicker' }, `${step.k} · ${wiz.step + 1} of ${WIZ_STEPS.length}`),
      el('div', { class: 'wiz-title' }, title),
      el('div', { class: 'wiz-sub' }, step.s)),
  );

  if (wiz.step === 0) body.append(wizFindTv());
  else if (wiz.step === 1) body.append(wizFindLights());
  else if (wiz.step === 2) { body.append(wizSides()); renderWizOrderList(); }
  else if (wiz.step === 3) body.append(wizCount(activeSlot));
  else if (wiz.step === 4) {
    body.append(wizPlace());
    wireSlider($('#wiz-night-track'), $('#wiz-night-thumb'), $('#wiz-night-fill'), 30,
      () => Math.round((cfg.dimming.night_level || 0) * 100),
      (pct) => { const v = Math.min(pct, Math.round(cfg.dimming.day_level * 100) - 1) / 100; $('#wiz-night-label').textContent = Math.round(v * 100) + '%'; set('dimming.night_level', v); });
  }
  else if (wiz.step === 5) body.append(wizHa());
  else if (wiz.step === 6) body.append(wizDone());

  const dusk = $('#dusk-caption');
  dusk.hidden = wiz.step !== 4;
  dusk.textContent = 'Daytime brightness applies during the day; the night level after sunset.';
  $('#wiz-svg').style.animation = wiz.step === 4 ? 'none' : 'none';

  const skip = $('#wiz-skip');
  skip.hidden = !(wiz.step === 5 && !cfg.mqtt.enabled);
  const back = $('#wiz-back');
  back.style.opacity = wiz.step === 0 ? '0.3' : '1';
  const next = $('#wiz-next');
  const footerHidden = wiz.step === 5 && cfg.mqtt.enabled && wiz.mqttTest === 'failed';
  next.hidden = footerHidden;
  next.textContent = wiz.step === WIZ_STEPS.length - 1 ? 'Take me to the lights'
    : wiz.step === 3 && wiz.countIdx < presentSlots().length - 1 ? 'Next side'
      : wiz.step === 5 && cfg.mqtt.enabled ? (wiz.mqttTest === 'testing' ? 'Testing…' : wiz.mqttTest === 'failed' ? 'Test again' : 'Test and continue')
        : 'Next';

  paintPreview('wz');
}

function wizFindTv() {
  return el('div', { style: 'display:flex;flex-direction:column;gap:12px' },
    el('div', { class: 'field' }, el('label', {}, 'TV address'),
      el('input', { class: 'input', id: 'wiz-tv-ip', value: cfg.source.tv_ip, placeholder: '192.168.1.44',
        onchange: (ev) => set('source.tv_ip', ev.target.value.trim(), true) })),
    el('div', { class: 'field-hint' }, 'The IP address of the Philips TV on your network. Check its network settings if you\'re not sure.'));
}
function wizFindLights() {
  const t = cfg.output.targets[0] || {};
  return el('div', { style: 'display:flex;flex-direction:column;gap:12px' },
    el('div', { class: 'field' }, el('label', {}, 'WLED controller address'),
      el('input', { class: 'input', id: 'wiz-wled-host', value: t.host || '', placeholder: 'wled-ceiling.local',
        onchange: (ev) => {
          const targets = cfg.output.targets.slice();
          if (!targets.length) targets.push({ port: 21324, http_port: 80, pixel_offset: 0, pixel_count: cfg.led.count, enabled: false });
          targets[0] = Object.assign({}, targets[0], { host: ev.target.value.trim(), enabled: !!ev.target.value.trim(), pixel_count: cfg.led.count });
          set('output.targets', targets, true);
        } })),
    el('div', { class: 'field-hint' }, 'The hostname or IP address WLED shows on its own network settings page.'));
}
function wizSides() {
  const box = el('div', { class: 'two-col' });
  for (const slot of SLOTS) {
    const present = !!edgeBySlot(slot);
    box.append(el('button', {
      class: 'side-btn' + (present ? ' active' : ''),
      onclick: () => { toggleSlot(slot); renderWizard(); },
    }, el('span', { class: 'dot', style: present ? `background:${SLOT_COLOUR[slot]};box-shadow:0 0 9px ${SLOT_COLOUR[slot]}` : '' }),
       el('span', { class: 'name' }, SLOT_LABEL[slot])));
  }
  const slots = presentSlots();
  const wrap = el('div', { style: 'display:flex;flex-direction:column;gap:10px' }, box,
    el('div', { class: 'field-hint' }, slots.length === 1 ? 'One run of strip. Everything else stays dark.'
      : `${slots.length} sides selected.`));
  if (slots.length > 1) {
    wrap.append(
      el('div', { class: 'field-hint' }, "Now put them in wiring order — the order your strip's data cable actually reaches them, starting from the controller. This decides each side's start pixel."),
      el('div', { id: 'wiz-order-list', style: 'display:flex;flex-direction:column;gap:8px' }),
    );
  }
  return wrap;
}
function renderWizOrderList() {
  const list = $('#wiz-order-list');
  if (!list) return;
  const slots = presentSlots();
  list.innerHTML = '';
  slots.forEach((slot, i) => {
    list.append(el('div', { class: 'side-btn active', style: 'cursor:default' },
      el('span', { class: 'dot', style: `background:${SLOT_COLOUR[slot]};box-shadow:0 0 9px ${SLOT_COLOUR[slot]}` }),
      el('span', { class: 'name' }, `${i + 1}. ${SLOT_LABEL[slot]}`),
      el('div', { style: 'display:flex;gap:4px' },
        el('button', { class: 'round-btn', style: `width:34px;height:34px;font-size:14px;${i === 0 ? 'opacity:.3' : ''}`, disabled: i === 0, onclick: (ev) => { ev.stopPropagation(); moveEdge(slot, -1); renderWizard(); } }, '↑'),
        el('button', { class: 'round-btn', style: `width:34px;height:34px;font-size:14px;${i === slots.length - 1 ? 'opacity:.3' : ''}`, disabled: i === slots.length - 1, onclick: (ev) => { ev.stopPropagation(); moveEdge(slot, 1); renderWizard(); } }, '↓'))));
  });
}
function wizCount(slot) {
  const e = edgeBySlot(slot);
  const total = presentSlots().slice(0, wiz.countIdx + 1).reduce((n, s) => n + edgeBySlot(s).pixel_count, 0);
  return el('div', { style: 'display:flex;flex-direction:column;gap:14px' },
    el('div', { class: 'wiz-counter' },
      el('button', { class: 'round-btn lg', onclick: () => { editEdge(slot, { pixel_count: Math.max(1, e.pixel_count - 1) }); renderWizard(); } }, '−'),
      el('div', { class: 'value' }, el('b', {}, e.pixel_count), el('small', {}, 'LEDs on this side')),
      el('button', { class: 'round-btn lg', onclick: () => { editEdge(slot, { pixel_count: e.pixel_count + 1 }); renderWizard(); } }, '+')),
    el('div', { style: 'display:flex;gap:10px' },
      el('button', { class: 'pill-btn soft', style: 'flex:1', onclick: () => flashSlot(slot) }, 'Flash this side'),
      el('button', { class: 'pill-btn soft', style: 'flex:1', onclick: () => { editEdge(slot, { reversed: !e.reversed }); renderWizard(); } }, e.reversed ? 'Flip back' : 'Flip')),
    el('div', { style: 'font-size:13px;opacity:.7;line-height:1.45' }, `Look at the real strip: it should be chasing ${(slot === 'left' || slot === 'right') ? 'back to front' : 'left to right'}, like the picture. If it runs the other way, tap Flip.`),
    el('div', { style: 'font-size:12.5px;opacity:.55' }, `Side ${wiz.countIdx + 1} of ${presentSlots().length} · ${total} LEDs so far`));
}
function wizPlace() {
  return el('div', { style: 'display:flex;flex-direction:column;gap:15px' },
    el('button', { class: 'pill-btn soft', style: 'min-height:52px', onclick: (ev) => useMyLocation(ev.target) }, 'Use my location'),
    el('div', { class: 'field' }, el('label', {}, 'Or paste coordinates / a map link'),
      el('input', { class: 'input', id: 'wiz-loc', placeholder: '60.45, 22.27', value: locationText(),
        onchange: (ev) => submitLocationText(ev.target) })),
    el('div', {},
      el('div', { class: 'row-label' }, el('span', { class: 'name' }, 'How dim at night'), el('span', { class: 'value' }, el('b', { id: 'wiz-night-label' }, Math.round(cfg.dimming.night_level * 100) + '%'))),
      el('div', { class: 'slider', id: 'wiz-night-track' }, el('div', { class: 'fill-track' }, el('div', { class: 'fill', id: 'wiz-night-fill' })), el('div', { class: 'thumb', id: 'wiz-night-thumb' }))));
}
function wizHa() {
  const on = !!cfg.mqtt.enabled;
  const wrap = el('div', { style: 'display:flex;flex-direction:column;gap:12px' },
    el('div', { class: 'toggle-row' }, el('span', { class: 'name' }, 'Show up in Home Assistant'),
      el('button', { class: 'toggle-switch' + (on ? ' on' : ''), onclick: () => { set('mqtt.enabled', !on, true); wiz.mqttTest = ''; renderWizard(); } }, el('span', { class: 'knob' }))));
  if (!on) { wrap.append(el('div', { class: 'field-hint' }, "Leave it off if you don't use Home Assistant. You can turn it on later.")); return wrap; }
  if (wiz.mqttTest === 'testing') {
    wrap.append(el('div', { class: 'spinner-row' }, el('span', { class: 'spinner' }), el('span', {}, `Logging in to ${mqttHostValue()}…`)));
    return wrap;
  }
  if (wiz.mqttTest === 'failed') {
    wrap.append(el('div', { class: 'fail-card' },
      el('div', { class: 'head' }, el('b', {}, wiz.mqttErr === 'unreachable' ? "Can't reach that broker" : 'The broker refused that login')),
      el('div', { style: 'font-size:14px;opacity:.8;line-height:1.5' }, wiz.mqttErr === 'unreachable'
        ? `Nothing answered at ${mqttHostValue()}. Check the address and that the broker is running.`
        : `Connected to ${mqttHostValue()}, but it rejected the username or password.`),
      el('div', { style: 'display:flex;gap:10px' },
        el('button', { class: 'cta-btn', style: 'flex:1', onclick: () => { wiz.mqttTest = ''; renderWizard(); } }, 'Fix the details'),
        el('button', { class: 'pill-btn', style: 'flex:1', onclick: () => { set('mqtt.enabled', false, true); wiz.step = 6; renderWizard(); } }, 'Set up later'))));
    return wrap;
  }
  wrap.append(
    el('div', { class: 'field-hint' }, 'AmbiWled announces itself over your MQTT broker — the same one Home Assistant uses.'),
    el('div', { class: 'field' }, el('label', {}, 'Broker address'),
      el('input', { class: 'input', id: 'wiz-mqtt-host', value: mqttHostValue(), placeholder: '192.168.1.10:1883',
        onchange: (ev) => setMqttHostPort(ev.target.value) })),
    el('div', { class: 'field' }, el('label', {}, 'Username'),
      el('input', { class: 'input', value: cfg.mqtt.username, placeholder: 'Leave blank if the broker is open',
        onchange: (ev) => set('mqtt.username', ev.target.value.trim(), true) })),
    el('div', { class: 'field' }, el('label', {}, 'Password'),
      el('input', { class: 'input', type: 'password', id: 'wiz-mqtt-pass', placeholder: 'Leave blank if the broker is open' })),
    el('div', { class: 'field-hint' }, "We'll try to log in before moving on, so you find out now rather than later."),
  );
  return wrap;
}
function wizDone() {
  const total = presentSlots().reduce((n, s) => n + edgeBySlot(s).pixel_count, 0);
  return el('div', { class: 'done-grid' },
    el('div', { class: 'cell' }, el('div', { class: 'k' }, 'TV'), el('div', { class: 'v' }, cfg.source.tv_ip || '—')),
    el('div', { class: 'cell' }, el('div', { class: 'k' }, 'Ceiling'), el('div', { class: 'v' }, `${total} LEDs`)));
}

function wireWizardFooter() {
  $('#wiz-back').addEventListener('click', () => {
    if (wiz.step === 3 && wiz.countIdx > 0) { wiz.countIdx -= 1; renderWizard(); return; }
    wiz.step = Math.max(0, wiz.step - 1);
    renderWizard();
  });
  $('#wiz-skip').addEventListener('click', () => wizNext());
  $('#wiz-next').addEventListener('click', () => wizNext());
}
async function wizNext() {
  if (wiz.step === 3 && wiz.countIdx < presentSlots().length - 1) { wiz.countIdx += 1; renderWizard(); return; }
  if (wiz.step === 5 && cfg.mqtt.enabled) {
    if (wiz.mqttTest === 'testing') return;
    wiz.mqttTest = 'testing'; renderWizard();
    const passInput = $('#wiz-mqtt-pass');
    const password = passInput ? passInput.value : '';
    try {
      const r = await fetch('/api/mqtt/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host: cfg.mqtt.host, port: cfg.mqtt.port, username: cfg.mqtt.username, password }),
      });
      const data = await r.json();
      if (data.ok) {
        if (password) set('mqtt.password', password, true);
        wiz.mqttTest = 'ok'; wiz.step = 6; renderWizard();
      } else {
        wiz.mqttTest = 'failed'; wiz.mqttErr = data.reason || 'unreachable'; renderWizard();
      }
    } catch (err) { wiz.mqttTest = 'failed'; wiz.mqttErr = 'unreachable'; renderWizard(); }
    return;
  }
  if (wiz.step === WIZ_STEPS.length - 1) { endWizard(); return; }
  // Arriving at "Where you are" is the one place the wizard asks for a
  // location and a night level - so it is also the one place that should
  // actually turn auto-dim on. Without this, finishing the wizard leaves
  // both configured and silently inactive until someone finds the toggle
  // in Advanced.
  if (wiz.step === 3) set('dimming.enabled', true, true);
  wiz.step += 1; wiz.countIdx = 0; renderWizard();
}

/* ---------- full render ---------- */

function renderAll() {
  $('#simple-view').hidden = advanced;
  $('#advanced-view').hidden = !advanced;
  renderSimple();
  if (advanced) { renderTabs(); renderPage(); }
  renderPreviewFromPixels();
}

document.addEventListener('DOMContentLoaded', boot);
