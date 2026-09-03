'use strict';

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

let cfg = null;             // live config mirror
let pixels = new Uint8Array(0);
let saveTimer = null;

const MODE_NOTES = {
  smoothing: 'Exponential filter. No added latency beyond the filter’s own lag, and no overshoot.',
  interpolation: 'Smoothest result, but costs one full source frame of latency (typically 50–100 ms).',
  passthrough: 'No temporal processing — shows exactly what the TV sends. For debugging.',
};

/* ---------- helpers ---------- */

function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? o : o[k]), obj);
}
function setPath(obj, path, value) {
  const keys = path.split('.');
  const last = keys.pop();
  let node = obj;
  for (const k of keys) { if (node[k] == null) node[k] = {}; node = node[k]; }
  node[last] = value;
}
function toast(msg, bad) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.className = 'toast'; }, bad ? 6000 : 2200);
}

/* ---------- config transport ---------- */

async function loadConfig() {
  const r = await fetch('/api/state');
  const data = await r.json();
  cfg = data.config;
  renderAll();
  showValidation(data.validation);
  applyMetrics(data.metrics);
}

/* Build a nested patch object from a dotted path, e.g.
   ('source.tv_ip', '10.0.0.1') -> { source: { tv_ip: '10.0.0.1' } } */
function patchFrom(path, value) {
  const keys = path.split('.');
  const out = {};
  let node = out;
  keys.forEach((k, i) => {
    if (i === keys.length - 1) node[k] = value;
    else { node[k] = {}; node = node[k]; }
  });
  return out;
}

function mergeInto(base, patch) {
  for (const [k, v] of Object.entries(patch)) {
    if (v && typeof v === 'object' && !Array.isArray(v) && base[k] && typeof base[k] === 'object' && !Array.isArray(base[k])) {
      mergeInto(base[k], v);
    } else {
      base[k] = v;
    }
  }
  return base;
}

let pendingPatch = null;
let pendingFields = new Set();

function setSaveState(state, detail) {
  const el = $('#m-save');
  if (!el) return;
  el.className = 'state ' + state;
  el.textContent = state === 'saving' ? 'saving…'
    : state === 'unsaved' ? (detail || 'rejected') : 'saved';
  el.title = detail || '';
}

function markFields(cls) {
  pendingFields.forEach((el) => {
    el.classList.remove('saved', 'rejected');
    // Restart the animation rather than letting a repeat edit skip it.
    void el.offsetWidth;
    el.classList.add(cls);
    if (cls === 'saved') setTimeout(() => el.classList.remove('saved'), 1200);
  });
  pendingFields = new Set();
}

/* Send only what changed. Sending the whole config would let this tab's stale
   mirror clobber settings changed elsewhere — another tab, or the API. */
function pushConfig(patch, immediate, field) {
  pendingPatch = mergeInto(pendingPatch || {}, patch);
  if (field) pendingFields.add(field);
  setSaveState('saving');
  clearTimeout(saveTimer);
  const send = async () => {
    const body = pendingPatch;
    pendingPatch = null;
    if (!body || !Object.keys(body).length) { setSaveState('saved'); return; }
    try {
      const r = await fetch('/api/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ patch: body }),
      });
      const data = await r.json();
      if (!r.ok) {
        showValidation(data.errors || ['rejected']);
        markFields('rejected');
        setSaveState('unsaved', (data.errors || ['rejected'])[0]);
        toast('Not applied: ' + (data.errors || ['rejected'])[0], true);
        return;
      }
      cfg = data.config;
      showValidation([]);
      markFields('saved');
      setSaveState('saved');
    } catch (err) {
      markFields('rejected');
      setSaveState('unsaved', err.message);
      toast('Not applied: ' + err.message, true);
    }
  };
  if (immediate) send(); else saveTimer = setTimeout(send, 350);
}

function showValidation(errors) {
  const box = $('#validation');
  if (!errors || !errors.length) {
    box.innerHTML = '<div class="ok">Mapping valid — no gaps, no overlaps, totals match.</div>';
    return;
  }
  box.innerHTML = errors.map((e) => `<div class="err">${e}</div>`).join('');
}

/* ---------- simple field binding ---------- */

function bindFields() {
  $$('[data-path]').forEach((el) => {
    const path = el.dataset.path;
    const type = el.dataset.type || 'string';
    const handler = () => {
      let v;
      if (type === 'bool') v = el.checked;
      else if (type === 'int') v = parseInt(el.value, 10);
      else if (type === 'float') v = parseFloat(el.value);
      else if (type === 'apiver') v = el.value === '' ? null : parseInt(el.value, 10);
      else v = el.value;
      if ((type === 'int' || type === 'float') && Number.isNaN(v)) return;
      setPath(cfg, path, v);
      if (path === 'frames.mode') $('#mode-note').textContent = MODE_NOTES[v] || '';
      if (path === 'mode') updatePresetVisibility();
      if (path === 'led.count') { fitToLedCount(); renderEdges(); drawStatic(); pushConfig(edgePatch(), true); }
      // Redraw instantly as any dimming setting (or the brightness it stacks
      // with) is dragged, before the change is even saved - the whole point
      // is seeing the effect of a slider without waiting on a round trip.
      if (path.startsWith('dimming.') || path === 'colour.brightness') drawDimmingChart();
      pushConfig(patchFrom(path, v), el.tagName === 'SELECT' || type === 'bool', el);
    };
    // Text fields commit on blur/Enter: typing an IP a character at a time
    // must not push half-finished addresses at the poller.
    const evt = el.type === 'text' || el.type === 'checkbox' || el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(evt, handler);
  });
}

function updatePresetVisibility() {
  const hide = !(cfg && cfg.mode === 'preset');
  $$('.preset-row').forEach((row) => { row.hidden = hide; });
}

/* The picker appears twice (Controls, and the fuller Mode panel below it) so
   both copies need the same options kept in sync. */
function populatePresetSelect(m) {
  const byHost = (m && m.presets) || {};
  const entries = Object.values(byHost).find((p) => p && Object.keys(p).length) || {};
  const ids = Object.keys(entries).sort((a, b) => +a - +b);
  const current = String((cfg && cfg.preset_slot) || '');
  const html = ids.length
    ? ids.map((id) => `<option value="${id}"${id === current ? ' selected' : ''}>${id} — ${entries[id]}</option>`).join('')
    : `<option value="${current || 0}">No presets found on the controller yet</option>`;
  $$('.preset-select').forEach((sel) => {
    if (document.activeElement === sel) return;   // do not fight an open picker
    sel.innerHTML = html;
  });
}

function fillFields() {
  updatePresetVisibility();
  $$('[data-path]').forEach((el) => {
    const v = getPath(cfg, el.dataset.path);
    if (el.type === 'checkbox') el.checked = !!v;
    else if (el.dataset.type === 'apiver') el.value = v == null ? '' : String(v);
    else el.value = v == null ? '' : v;
  });
  $('#mode-note').textContent = MODE_NOTES[cfg.frames.mode] || '';
  drawDimmingChart();
}

/* ---------- edges table ---------- */

let detectedSides = [];

function sourceOptions(current) {
  const sides = detectedSides.length ? detectedSides : ['top', 'left', 'right'];
  const opts = sides.concat(['synth_gradient', 'mirror_top', 'average', 'off']);
  if (current && !opts.includes(current)) opts.unshift(current);
  return opts.map((o) => `<option value="${o}"${o === current ? ' selected' : ''}>${o}</option>`).join('');
}

function refToText(ref) { return Array.isArray(ref) ? `${ref[0]}:${ref[1]}` : ''; }
function textToRef(text) {
  const m = String(text).split(':');
  if (m.length !== 2) return null;
  const n = parseInt(m[1], 10);
  return Number.isNaN(n) ? null : [m[0].trim(), n];
}

let manualRanges = false;

/* Edges are packed head-to-tail in list order. With starts derived rather than
   typed, an overlap or a gap is not expressible — the only thing left to get
   wrong is the total, and the budget bar shows that with a one-click fix. */
function repack() {
  if (manualRanges) return;
  let cursor = 0;
  for (const e of cfg.mapping.edges) {
    e.pixel_start = cursor;
    cursor += Math.max(1, e.pixel_count | 0);
  }
}

function assigned() {
  return cfg.mapping.edges.reduce((n, e) => n + Math.max(0, e.pixel_count | 0), 0);
}

/* Absorb any shortfall or excess into the last edge, so the config is valid. */
function fitToLedCount() {
  const edges = cfg.mapping.edges;
  if (!edges.length) return;
  const last = edges[edges.length - 1];
  const others = assigned() - Math.max(0, last.pixel_count | 0);
  last.pixel_count = Math.max(1, cfg.led.count - others);
  repack();
}

function renderBudget() {
  const total = cfg.led.count;
  const used = assigned();
  const diff = total - used;
  const box = $('#budget');
  const edges = cfg.mapping.edges.slice();
  const bar = edges.map((e, i) =>
    `<i style="background:${EDGE_COLOURS[i % EDGE_COLOURS.length]};width:${(Math.max(0, e.pixel_count) / Math.max(total, used)) * 100}%"></i>`
  ).join('') + (diff > 0 ? `<i style="background:#2a2f38;width:${(diff / Math.max(total, used)) * 100}%"></i>` : '');

  box.className = 'budget ' + (diff === 0 ? 'ok' : 'off');
  box.innerHTML =
    `<span>Assigned <b>${used}</b> of ${total} pixels</span>` +
    `<span class="bar">${bar}</span>` +
    (diff === 0
      ? '<b>exact</b>'
      : `<b>${diff > 0 ? diff + ' unassigned' : (-diff) + ' over'}</b>` +
        `<button class="tiny" id="budget-fix">Fit last edge</button>`);

  const fix = $('#budget-fix');
  if (fix) fix.addEventListener('click', () => { fitToLedCount(); renderEdges(); drawStatic(); pushConfig(edgePatch(), true); });
}

function edgePatch() { return { mapping: { edges: cfg.mapping.edges } }; }
function targetPatch() { return { output: { targets: cfg.output.targets } }; }

function syncStarts() {
  document.querySelectorAll('#edge-rows [data-field="pixel_start"]').forEach((el) => {
    const e = cfg.mapping.edges[+el.dataset.edge];
    if (e && document.activeElement !== el) el.value = e.pixel_start;
  });
}

function renderEdges() {
  repack();
  const body = $('#edge-rows');
  body.innerHTML = '';
  cfg.mapping.edges.forEach((e, i) => {
    const isSynth = e.source === 'synth_gradient';
    const isMirror = e.source === 'mirror_top';
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td class="order">
        <button class="tiny" data-move="${i}" data-dir="-1" ${i === 0 ? 'disabled' : ''}>&uarr;</button>
        <button class="tiny" data-move="${i}" data-dir="1" ${i === cfg.mapping.edges.length - 1 ? 'disabled' : ''}>&darr;</button>
      </td>
      <td><input type="text" value="${e.name || ''}" data-edge="${i}" data-field="name"></td>
      <td><input type="number" value="${e.pixel_start}" data-edge="${i}" data-field="pixel_start"
                 ${manualRanges ? '' : 'readonly title="Derived from the edge order and counts. Tick “Manual pixel ranges” to edit."'}></td>
      <td><input type="number" min="1" value="${e.pixel_count}" data-edge="${i}" data-field="pixel_count"></td>
      <td><select data-edge="${i}" data-field="source">${sourceOptions(e.source)}</select></td>
      <td><input type="text" value="${isSynth ? refToText(e.synth_from) : (isMirror ? (e.mirror_of || '') : '')}"
                 data-edge="${i}" data-field="${isMirror ? 'mirror_of' : 'synth_from'}"
                 ${isSynth || isMirror ? '' : 'disabled'} placeholder="${isMirror ? 'edge name' : 'right:-1'}"></td>
      <td><input type="text" value="${isSynth ? refToText(e.synth_to) : ''}" data-edge="${i}" data-field="synth_to"
                 ${isSynth ? '' : 'disabled'} placeholder="left:0"></td>
      <td><input type="checkbox" ${e.source_reversed ? 'checked' : ''} data-edge="${i}" data-field="source_reversed"></td>
      <td><input type="checkbox" ${e.output_reversed ? 'checked' : ''} data-edge="${i}" data-field="output_reversed"></td>
      <td><input type="number" min="0" max="2" step="0.05"
                 value="${e.brightness == null ? 1 : e.brightness}"
                 data-edge="${i}" data-field="brightness" title="1 = normal, 0.5 = half, 0 = off"></td>
      <td class="row-actions">
        <button class="tiny" data-identify="${i}">Identify</button>
        <button class="tiny danger" data-remove-edge="${i}">&times;</button>
      </td>`;
    body.appendChild(tr);
  });

  body.querySelectorAll('[data-edge]').forEach((el) => {
    const e = cfg.mapping.edges[+el.dataset.edge];
    const f = el.dataset.field;

    /* Returns false while the field is mid-edit (empty box, half-typed ref),
       so a transient value never reaches the config. */
    const read = () => {
      if (el.type === 'checkbox') { e[f] = el.checked; return true; }
      if (f === 'pixel_count' || f === 'pixel_start') {
        if (el.value.trim() === '') return false;
        const n = parseInt(el.value, 10);
        if (Number.isNaN(n)) return false;
        e[f] = f === 'pixel_count' ? Math.max(1, n) : Math.max(0, n);
        return true;
      }
      if (f === 'synth_from' || f === 'synth_to') {
        const r = textToRef(el.value);
        if (!r) return false;
        e[f] = r; return true;
      }
      if (f === 'brightness') {
        if (el.value.trim() === '') return false;
        const n = parseFloat(el.value);
        if (Number.isNaN(n)) return false;
        e[f] = Math.max(0, Math.min(n, 2));
        return true;
      }
      e[f] = el.value; return true;
    };

    // Live feedback while typing a number, WITHOUT rebuilding the table —
    // a rebuild would destroy the input you are typing into after one digit.
    if (el.type === 'number') {
      el.addEventListener('input', () => {
        if (!read()) return;
        repack(); syncStarts(); renderBudget(); renderLegend(); drawStatic();
      });
    }

    // Commit on blur/Enter (or immediately for checkboxes and selects).
    el.addEventListener('change', () => {
      if (!read()) { renderEdges(); return; }
      repack(); renderEdges(); drawStatic();
      pushConfig(edgePatch(), true);
    });
  });

  body.querySelectorAll('[data-move]').forEach((b) => {
    b.addEventListener('click', () => {
      const i = +b.dataset.move, j = i + (+b.dataset.dir);
      const edges = cfg.mapping.edges;
      if (j < 0 || j >= edges.length) return;
      [edges[i], edges[j]] = [edges[j], edges[i]];
      repack(); renderEdges(); drawStatic(); pushConfig(edgePatch(), true);
    });
  });

  body.querySelectorAll('[data-identify]').forEach((b) => {
    b.addEventListener('click', () => {
      const e = cfg.mapping.edges[+b.dataset.identify];
      // Send the range explicitly: identify must work on the edge being edited,
      // whether or not the config has been accepted yet.
      sendWs({ type: 'identify', start: e.pixel_start, count: e.pixel_count,
               seconds: 10, colour: [255, 255, 255] });
      toast(`Lighting pixels ${e.pixel_start}–${e.pixel_start + e.pixel_count - 1} (“${e.name}”) for 10 s`);
    });
  });

  body.querySelectorAll('[data-remove-edge]').forEach((b) => {
    b.addEventListener('click', () => {
      cfg.mapping.edges.splice(+b.dataset.removeEdge, 1);
      fitToLedCount();
      renderEdges(); drawStatic(); pushConfig(edgePatch(), true);
    });
  });

  renderBudget();
  renderLegend();
}

function renderTargets() {
  const body = $('#target-rows');
  body.innerHTML = '';
  cfg.output.targets.forEach((t, i) => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input type="text" value="${t.host || ''}" data-target="${i}" data-field="host"></td>
      <td><input type="number" value="${t.port}" data-target="${i}" data-field="port"></td>
      <td><input type="number" value="${t.http_port == null ? 80 : t.http_port}" data-target="${i}" data-field="http_port"></td>
      <td><input type="number" value="${t.pixel_offset}" data-target="${i}" data-field="pixel_offset"></td>
      <td><input type="number" value="${t.pixel_count}" data-target="${i}" data-field="pixel_count"></td>
      <td><input type="checkbox" ${t.enabled !== false ? 'checked' : ''} data-target="${i}" data-field="enabled"></td>
      <td><button class="tiny danger" data-remove-target="${i}">×</button></td>`;
    body.appendChild(tr);
  });
  body.querySelectorAll('[data-target]').forEach((el) => {
    const ev = el.type === 'text' || el.type === 'checkbox' ? 'change' : 'input';
    el.addEventListener(ev, () => {
      const t = cfg.output.targets[+el.dataset.target];
      const f = el.dataset.field;
      if (el.type === 'checkbox') t[f] = el.checked;
      else if (f === 'host') t[f] = el.value;
      else t[f] = parseInt(el.value, 10) || 0;
      pushConfig(targetPatch(), ev === 'change', el);
    });
  });
  body.querySelectorAll('[data-remove-target]').forEach((b) => {
    b.addEventListener('click', () => {
      cfg.output.targets.splice(+b.dataset.removeTarget, 1);
      renderTargets(); pushConfig(targetPatch(), true);
    });
  });
}

const EDGE_COLOURS = ['#4da3ff', '#35c46b', '#f0b429', '#c46be0', '#f0553f', '#2ec4c4'];

function renderLegend() {
  $('#edge-legend').innerHTML = cfg.mapping.edges
    .slice()
    .sort((a, b) => a.pixel_start - b.pixel_start)
    .map((e, i) => `<span><span class="swatch" style="background:${EDGE_COLOURS[i % EDGE_COLOURS.length]}"></span>${e.name} · ${e.pixel_start}–${e.pixel_start + e.pixel_count - 1} · ${e.source}</span>`)
    .join('');
  $('#strip-max').textContent = String(cfg.led.count - 1);
}

/* ---------- preview rendering ---------- */

const ceiling = $('#ceiling');
const cctx = ceiling.getContext('2d');
const strip = $('#strip');
const sctx = strip.getContext('2d');

function sizeCanvas(cv, cssW, cssH) {
  const dpr = window.devicePixelRatio || 1;
  cv.style.height = cssH + 'px';
  if (cv.width !== Math.round(cssW * dpr) || cv.height !== Math.round(cssH * dpr)) {
    cv.width = Math.round(cssW * dpr);
    cv.height = Math.round(cssH * dpr);
  }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return ctx;
}

/* Rectangle geometry: 222 cm x 164 cm, drawn to fit the canvas. */
function ceilingGeometry(w, h) {
  const pad = 26;
  const aspect = 222 / 164;
  let rw = w - pad * 2;
  let rh = rw / aspect;
  if (rh > h - pad * 2) { rh = h - pad * 2; rw = rh * aspect; }
  const x = (w - rw) / 2;
  const y = (h - rh) / 2;
  return { x, y, w: rw, h: rh };
}

/* Point on the rectangle perimeter, clockwise from the top-left corner. */
function perimeterPoint(g, t) {
  const per = 2 * (g.w + g.h);
  let d = ((t % 1) + 1) % 1 * per;
  if (d < g.w) return [g.x + d, g.y];
  d -= g.w;
  if (d < g.h) return [g.x + g.w, g.y + d];
  d -= g.h;
  if (d < g.w) return [g.x + g.w - d, g.y + g.h];
  d -= g.w;
  return [g.x, g.y + g.h - d];
}

/* With exactly four edges, honour each edge's own pixel count per side
   (the sides have different pixel densities). Otherwise walk the perimeter. */
function pixelPositions() {
  const g = ceilingGeometry(ceiling.clientWidth, ceiling.clientWidth * 0.62);
  const edges = cfg.mapping.edges.slice().sort((a, b) => a.pixel_start - b.pixel_start);
  const total = cfg.led.count;
  const pos = new Array(total);

  if (edges.length === 4) {
    const sides = [
      { from: [g.x, g.y], to: [g.x + g.w, g.y] },                     // TV wall
      { from: [g.x + g.w, g.y], to: [g.x + g.w, g.y + g.h] },         // right
      { from: [g.x + g.w, g.y + g.h], to: [g.x, g.y + g.h] },         // rear
      { from: [g.x, g.y + g.h], to: [g.x, g.y] },                     // left
    ];
    // Anchor the schematic to whichever edge carries the screen's top zones,
    // so the drawing is not rotated when the strip starts on a side run.
    let anchor = edges.findIndex((e) => e.source === 'top');
    if (anchor < 0) anchor = 0;
    edges.forEach((e, i) => {
      const s = sides[(i - anchor + 4) % 4];
      for (let k = 0; k < e.pixel_count; k++) {
        const t = (k + 0.5) / e.pixel_count;
        const idx = e.pixel_start + k;
        if (idx < total) pos[idx] = [s.from[0] + (s.to[0] - s.from[0]) * t, s.from[1] + (s.to[1] - s.from[1]) * t];
      }
    });
  } else {
    for (let i = 0; i < total; i++) pos[i] = perimeterPoint(g, (i + 0.5) / total);
  }
  for (let i = 0; i < total; i++) if (!pos[i]) pos[i] = [g.x, g.y];
  return { g, pos };
}

let geomCache = null;
function drawStatic() { geomCache = null; drawCeiling(); drawStrip(); }

function drawCeiling() {
  const w = ceiling.clientWidth || 640;
  const h = Math.round(w * 0.62);
  const ctx = sizeCanvas(ceiling, w, h);
  if (!cfg) return;
  if (!geomCache || geomCache.w !== w) { geomCache = { w, ...pixelPositions() }; }
  const { g, pos } = geomCache;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b0d10';
  ctx.fillRect(0, 0, w, h);

  // Room outline and the TV, so orientation is never ambiguous.
  ctx.strokeStyle = '#2a2f38';
  ctx.lineWidth = 1;
  ctx.strokeRect(g.x, g.y, g.w, g.h);
  ctx.fillStyle = '#8b93a1';
  ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('TV wall', g.x + g.w / 2, g.y - 10);
  ctx.fillText('rear (behind viewer)', g.x + g.w / 2, g.y + g.h + 18);
  ctx.save();
  ctx.translate(g.x - 12, g.y + g.h / 2); ctx.rotate(-Math.PI / 2);
  ctx.fillText('left', 0, 0); ctx.restore();
  ctx.save();
  ctx.translate(g.x + g.w + 14, g.y + g.h / 2); ctx.rotate(Math.PI / 2);
  ctx.fillText('right', 0, 0); ctx.restore();
  ctx.fillStyle = '#3a4150';
  ctx.fillRect(g.x + g.w / 2 - 34, g.y - 6, 68, 4);

  const n = Math.min(cfg.led.count, pixels.length / 3 | 0);
  const r = Math.max(2.2, g.w / cfg.led.count * 1.6);
  for (let i = 0; i < n; i++) {
    const p = pos[i];
    if (!p) continue;
    const o = i * 3;
    ctx.fillStyle = `rgb(${pixels[o]},${pixels[o + 1]},${pixels[o + 2]})`;
    ctx.beginPath();
    ctx.arc(p[0], p[1], r, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawStrip() {
  const w = strip.clientWidth || 640;
  const ctx = sizeCanvas(strip, w, 34);
  if (!cfg) return;
  ctx.clearRect(0, 0, w, 34);
  const total = cfg.led.count;
  const bw = w / total;
  const n = Math.min(total, pixels.length / 3 | 0);
  for (let i = 0; i < n; i++) {
    const o = i * 3;
    ctx.fillStyle = `rgb(${pixels[o]},${pixels[o + 1]},${pixels[o + 2]})`;
    ctx.fillRect(i * bw, 0, Math.max(bw, 1), 26);
  }
  // Edge boundaries, so a reversal or off-by-one is visible at a glance.
  cfg.mapping.edges.slice().sort((a, b) => a.pixel_start - b.pixel_start).forEach((e, i) => {
    ctx.fillStyle = EDGE_COLOURS[i % EDGE_COLOURS.length];
    ctx.fillRect(e.pixel_start * bw, 27, Math.max(e.pixel_count * bw - 1, 1), 5);
  });
}

/* ---------- metrics ---------- */

function applyMetrics(m) {
  if (!m) return;
  $('#m-state').textContent = m.tv_state === 'streaming' ? 'streaming'
    : m.tv_state === 'tv_off' ? 'TV off'
    : m.tv_state === 'unconfigured' ? 'set a TV address'
    : m.tv_state === 'ambilight_off' ? 'Ambilight off' : m.tv_state;
  $('#m-state').className = 'state ' + m.tv_state;
  $('#m-sfps').textContent = m.source_fps ? m.source_fps.toFixed(1) : '–';
  $('#m-ofps').textContent = m.output_fps ? m.output_fps.toFixed(0) : '–';
  $('#m-lat').textContent = m.source_latency_ms ? m.source_latency_ms.toFixed(0) + ' ms' : '–';
  $('#m-fail').textContent = `${m.failed_polls} / ${m.total_polls}`;
  $('#m-api').textContent = m.api_version ? 'v' + m.api_version : '–';

  const amb = m.ambilight_power;
  $('#m-amb').textContent = amb === true ? 'on' : amb === false ? 'off' : 'unknown';
  $('#m-amb').className = 'state ' + (amb === true ? 'streaming' : amb === false ? 'tv_off' : '');

  const targets = m.targets || {};
  const hosts = Object.keys(targets);
  const down = hosts.filter((h) => targets[h] === false);
  $('#m-wled').textContent = !hosts.length ? '–'
    : down.length === 0 ? 'reachable'
    : down.length === hosts.length ? 'offline' : `${hosts.length - down.length}/${hosts.length} up`;
  $('#m-wled').className = 'state ' + (down.length === 0 ? 'streaming' : 'error');

  if (m.brightness_applied != null) {
    const lvl = m.brightness_level, ap = m.brightness_applied;
    $('#bri-applied').textContent = `${(ap * 100).toFixed(1)}% of full` +
      (Math.abs(ap - lvl) > 0.001 ? ` (level ${lvl}, perceptual curve applied)` : '');
  }

  const d = m.dimming || {};
  const sun = $('#sun-readout');
  if (sun) {
    if (!d.enabled) {
      sun.innerHTML = 'Auto-dim is <b>off</b> — brightness follows the Colour panel only.';
    } else if (d.polar) {
      sun.innerHTML = `<b>${d.polar === 'polar_night' ? 'Polar night' : 'Polar day'}</b> — ` +
        `holding level <b>${d.level}</b>.`;
    } else {
      const t = (iso) => iso ? new Date(iso).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) : '?';
      sun.innerHTML = `Sunrise <b>${t(d.sunrise)}</b>, sunset <b>${t(d.sunset)}</b> — ` +
        `curve at <b>${(d.day_fraction * 100).toFixed(0)}%</b> of peak, level <b>${d.level}</b>.`;
    }
  }

  const ctl = Object.entries(m.controllers || {});
  const first = ctl.length ? ctl[0][1] : null;
  $('#m-cfps').textContent = first && first.fps != null ? first.fps : '–';
  $('#m-pwr').textContent = first && first.power_ma != null ? first.power_ma + ' mA' : '–';

  const cr = $('#controller-readout');
  if (cr) {
    if (!ctl.length) {
      cr.textContent = 'No controller telemetry yet.';
    } else {
      cr.innerHTML = ctl.map(([host, i]) => {
        const limited = i.brightness_limited
          ? ' <b style="color:var(--warn)">ABL limiting</b>' : '';
        const src = i.realtime ? `realtime from <b>${i.realtime_source || '?'}</b>` : 'idle';
        return `<div><b>${host}</b> — WLED ${i.version || '?'}, ${i.led_count || '?'} LEDs, ` +
               `rendering <b>${i.fps != null ? i.fps : '?'}</b> fps, drawing <b>${i.power_ma != null ? i.power_ma : '?'}</b> mA, ` +
               `${src}${limited}</div>`;
      }).join('');
    }
    if (m.output_conflict) {
      cr.innerHTML += `<div class="err" style="margin-top:6px">Conflict: ${m.output_conflict}. ` +
                      `Two sources are driving the strip; expect flicker.</div>`;
    }
  }

  if (m.mode) {
    $('#m-mode').textContent = m.mode;
    $('#m-mode').className = 'state ' + (m.mode === 'off' ? 'tv_off' : 'streaming');
  }
  const q = m.mqtt || {};
  const mr = $('#mqtt-readout');
  if (mr) {
    if (!q.enabled) mr.textContent = 'MQTT is off. Home Assistant will not see this device.';
    else if (q.connected) {
      mr.innerHTML = `Connected to <b>${q.host}</b> — published <b>${q.publishes}</b> updates, ` +
                     `received <b>${q.commands}</b> commands. Topics under <code>${q.base_topic}/</code>.`;
    } else {
      mr.innerHTML = `<span style="color:var(--bad)">Not connected to ${q.host}` +
                     (q.last_error ? ` — ${q.last_error}` : '') + '</span>';
    }
  }

  populatePresetSelect(m);
  const presetReadout = $('#preset-readout');
  if (presetReadout) {
    if (m.mode !== 'preset') {
      presetReadout.textContent = '';
    } else if (m.preset_applied) {
      presetReadout.innerHTML = `Active: <b>${m.preset_name || ('preset ' + m.preset_slot)}</b>`;
    } else {
      presetReadout.innerHTML = `<span style="color:var(--warn)">Applying preset ${m.preset_slot}…</span>` +
        (m.preset_error ? ` &mdash; ${m.preset_error}` : '');
    }
  }

  $('#m-emit').textContent = m.emitting ? 'yes' : 'no';
  $('#m-emit').className = 'state ' + (m.emitting ? 'streaming' : 'tv_off');

  const sides = m.layout || {};
  const text = Object.keys(sides).length
    ? Object.entries(sides).map(([k, v]) => `${k} = ${v}`).join(', ') +
      ` (${Object.values(sides).reduce((a, b) => a + b, 0)} zones)`
    : 'unknown — TV not reachable yet';
  $('#layout').textContent = text;
  const known = Object.keys(sides);
  if (known.length && known.join() !== detectedSides.join()) {
    detectedSides = known;
    renderEdges();
  }
  if (m.mapping_problems && m.mapping_problems.length) showValidation(m.mapping_problems);
}

/* ---------- websocket ---------- */

let ws = null;
function sendWs(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { $('#m-ws').textContent = 'live'; $('#m-ws').className = 'state open'; };
  ws.onclose = () => {
    $('#m-ws').textContent = 'reconnecting'; $('#m-ws').className = 'state closed';
    setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'metrics') applyMetrics(msg);
      else if (msg.type === 'hello' && !cfg) { cfg = msg.config; renderAll(); }
      return;
    }
    pixels = new Uint8Array(ev.data);
    drawCeiling();
    drawStrip();
  };
}

/* ---------- wiring ---------- */

function renderAll() {
  fillFields();
  renderEdges();
  renderTargets();
  drawStatic();
}

$('#add-edge').addEventListener('click', () => {
  const edges = cfg.mapping.edges;
  const leftover = Math.max(1, cfg.led.count - assigned());
  edges.push({
    name: `edge_${edges.length + 1}`, pixel_start: assigned(), pixel_count: leftover,
    source: detectedSides[0] || 'top', source_reversed: false, output_reversed: false,
  });
  repack(); renderEdges(); drawStatic(); pushConfig(edgePatch(), true);
});

$('#fit-edges').addEventListener('click', () => {
  fitToLedCount(); renderEdges(); drawStatic(); pushConfig(edgePatch(), true);
  toast('Last edge resized to fill the strip');
});

$('#manual-ranges').addEventListener('change', (ev) => {
  manualRanges = ev.target.checked;
  renderEdges();
});

$('#add-target').addEventListener('click', () => {
  cfg.output.targets.push({ host: '', port: 21324, http_port: 80, pixel_offset: 0, pixel_count: cfg.led.count, enabled: false });
  renderTargets();
});

async function pushLayout(mode, question) {
  if (!confirm(question)) return;
  const savePreset = $('#save-preset') && $('#save-preset').checked;
  const qs = 'mode=' + mode + (savePreset ? '&preset=2' : '');
  try {
    const r = await fetch('/api/wled/segments?' + qs, { method: 'POST' });
    const d = await r.json();
    if (!r.ok) { toast('Failed: ' + JSON.stringify(d.results || d.errors), true); return; }
    const saved = d.results.some((x) => x.preset && x.preset.ok);
    toast(`Pushed ${d.segments.length} segment(s)` + (saved ? ', saved as preset 2' : ''));
  } catch (err) {
    toast('Failed: ' + err.message, true);
  }
}

$('#push-frontback').addEventListener('click', () => pushLayout('front_to_back',
  'Replace the controller\'s segments with ONE mirrored segment, offset to the front centre?' +
  '\n\nA single effect then curls from the front centre down both sides to the rear. ' +
  'You lose per-run effects. The Ambilight stream is unaffected.'));

$('#push-segments').addEventListener('click', async () => {
  const names = cfg.mapping.edges.map((e) => e.name).join(', ');
  await pushLayout('edges',
    `Replace the controller's segments with one per edge (${names})?` +
    `\n\nEach run then has its own effect. The Ambilight stream is unaffected.`);
});

$('#export').addEventListener('click', () => {
  const blob = new Blob([JSON.stringify(cfg, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ambiwled-config.json';
  a.click();
  URL.revokeObjectURL(a.href);
});

$('#import').addEventListener('click', () => $('#import-file').click());
$('#import-file').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  try {
    cfg = JSON.parse(await file.text());
    pushConfig(cfg, true);   // import is deliberately whole-config
    await loadConfig();
    toast('Config imported');
  } catch (err) {
    toast('Import failed: ' + err.message, true);
  }
  ev.target.value = '';
});

$('#reset').addEventListener('click', async () => {
  if (!confirm('Reset all settings to defaults?')) return;
  const r = await fetch('/api/config/reset', { method: 'POST' });
  cfg = (await r.json()).config;
  renderAll();
  toast('Reset to defaults');
});

/* Browser geolocation needs a secure context: over plain http on a LAN address
   the API is simply absent, so say why rather than letting the button fail
   mysteriously. Opening the UI over localhost or https makes it available. */
function locationAvailable() {
  return !!(navigator.geolocation && (window.isSecureContext ||
            ['localhost', '127.0.0.1'].includes(location.hostname)));
}

function refreshLocationNote() {
  const note = $('#location-note');
  const btn = $('#use-location');
  if (!note || !btn) return;
  const lat = cfg && cfg.dimming ? cfg.dimming.latitude : null;
  const lon = cfg && cfg.dimming ? cfg.dimming.longitude : null;
  const set = lat != null && (lat !== 0 || lon !== 0);
  // Always show what is actually applied right now, persistently - not just a
  // toast that can be missed. This is the thing to check if a change seems
  // not to have taken.
  const current = set ? `<b>Current location: ${lat}, ${lon}.</b> ` : '<b>No location set yet.</b> ';

  // The button stays clickable either way, so clicking it is never silent -
  // when geolocation is unavailable it explains why instead of doing nothing.
  btn.disabled = false;
  if (locationAvailable()) {
    note.innerHTML = current + (set
      ? 'Press the button, or paste new coordinates below, to change it.'
      : 'Press the button, or paste coordinates from any map app below. Only latitude matters '
        + 'much &mdash; a degree of longitude is four minutes of sunset.');
  } else {
    note.innerHTML = current +
      'Paste coordinates from any map app below &mdash; long-press a spot and it shows something ' +
      'like <code>60.4518, 22.2666</code>; a shared map link works too. ' +
      '<i>Use my location</i> needs https or localhost, a browser rule this page cannot get around ' +
      'on a plain LAN address; click it to see that explained.';
  }
}

/* Accepts anything a phone or desktop map app puts on the clipboard:
   "60.45, 22.27", "60.45N 22.27E", or a maps URL with @lat,lon / ?q=lat,lon /
   !3dlat!4dlon. Typing two numbers by hand works too. Offline, no geocoding
   service, no account, works wherever the page is served from. */
function parseCoordinates(text) {
  if (!text) return null;
  const s = decodeURIComponent(String(text)).replace(/\s+/g, ' ').trim();

  // Order matters: a Google link carries both the pin (!3d!4d) and the map
  // centre (@lat,lon), and the pin is the one the user actually chose.
  const patterns = [
    /!3d(-?\d{1,3}(?:\.\d+)?)!4d(-?\d{1,3}(?:\.\d+)?)/,        // google place pin
    /#map=\d+\/(-?\d{1,3}(?:\.\d+)?)\/(-?\d{1,3}(?:\.\d+)?)/, // openstreetmap
    /[?&]m?lat=(-?\d{1,3}(?:\.\d+)?)[^0-9-]+m?lon=(-?\d{1,3}(?:\.\d+)?)/, // osm share
    /[?&]q=(-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)/,    // ?q=lat,lon
    /[@](-?\d{1,3}(?:\.\d+)?),\s*(-?\d{1,3}(?:\.\d+)?)/,       // maps @lat,lon
    /(-?\d{1,3}(?:\.\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:\.\d+)?)/,  // plain pair
  ];
  for (const re of patterns) {
    const m = s.match(re);
    if (m) {
      const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
      if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon };
    }
  }

  // Hemisphere letters: "60.45 N, 22.27 E" (either order).
  const hemi = s.match(/(-?\d{1,3}(?:\.\d+)?)\s*([NSns])[ ,;]*(-?\d{1,3}(?:\.\d+)?)\s*([EWew])/);
  if (hemi) {
    const lat = parseFloat(hemi[1]) * (hemi[2].toUpperCase() === 'S' ? -1 : 1);
    const lon = parseFloat(hemi[3]) * (hemi[4].toUpperCase() === 'W' ? -1 : 1);
    if (Math.abs(lat) <= 90 && Math.abs(lon) <= 180) return { lat, lon };
  }
  return null;
}

function applyLocation(lat, lon, how) {
  lat = Math.round(lat * 10000) / 10000;
  lon = Math.round(lon * 10000) / 10000;
  cfg.dimming.latitude = lat;
  cfg.dimming.longitude = lon;
  fillFields();
  refreshLocationNote();               // shows "Current location: ..." right away

  const latField = document.querySelector('[data-path="dimming.latitude"]');
  const lonField = document.querySelector('[data-path="dimming.longitude"]');
  if (latField) pendingFields.add(latField);
  if (lonField) pendingFields.add(lonField);
  pushConfig({ dimming: { latitude: lat, longitude: lon } }, true);
  toast(`Location set to ${lat}, ${lon}${how ? ' (' + how + ')' : ''}`);

  // The Sunrise/Sunset readout comes from the server's metrics, which
  // otherwise only refreshes on the next periodic websocket tick - fetch it
  // once right now so the effect is visible immediately, not "eventually".
  fetch('/api/state').then((r) => r.json()).then((d) => applyMetrics(d.metrics)).catch(() => {});
}

function looksLikeUrl(text) {
  return /^https?:\/\//i.test(String(text).trim());
}

function showApplied(box, lat, lon) {
  box.value = `${lat}, ${lon} — applied`;
  box.classList.add('saved');
  setTimeout(() => { box.value = ''; box.classList.remove('saved'); }, 1600);
}

// A shortened map link (Google's maps.app.goo.gl Share default on mobile)
// carries no coordinates itself - only the page it redirects to does, and a
// browser will not let JS read where a cross-origin redirect like that
// lands. The server can, without that restriction; this asks it to, then
// runs the SAME parser on wherever it landed - one parser, one source of
// truth for URL formats, regardless of whether a link needed resolving.
async function resolveLinkAndApply(box, text) {
  const original = box.value;
  box.disabled = true;
  box.value = 'Resolving link…';
  try {
    const r = await fetch('/api/resolve-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: text.trim() }),
    });
    const data = await r.json();
    if (!r.ok || !data.resolved_url) throw new Error(data.error || 'could not resolve the link');
    const hit = parseCoordinates(data.resolved_url);
    if (!hit) throw new Error('resolved, but no coordinates were in it');
    applyLocation(hit.lat, hit.lon, 'pasted, resolved');
    showApplied(box, hit.lat, hit.lon);
  } catch (err) {
    toast('Could not resolve that link: ' + err.message, true);
    box.value = original;
  } finally {
    box.disabled = false;
    box.focus();                       // disabling it mid-resolve drops focus; put it back
  }
}

$('#paste-location').addEventListener('input', (ev) => {
  const hit = parseCoordinates(ev.target.value);
  const note = $('#location-note');
  const text = ev.target.value;
  if (!text.trim()) { refreshLocationNote(); return; }
  if (hit) {
    note.innerHTML = `Reads as <b>${hit.lat}, ${hit.lon}</b> — press OK to use it.`;
  } else if (looksLikeUrl(text)) {
    note.innerHTML = 'Looks like a link — press OK to resolve it and read the coordinates.';
  } else {
    note.textContent = 'Not recognised yet. Paste a map link, or type "60.45, 22.27".';
  }
});

// One shared action for the OK button and the Enter/Go key on a phone
// keyboard - neither is more "correct" than the other, and a mobile keyboard
// may not even have a literal Enter key, so the button is the one path that
// works everywhere regardless of what a given keyboard calls its action key.
function submitLocationText(box) {
  const text = box.value;
  const hit = parseCoordinates(text);
  if (hit) {
    applyLocation(hit.lat, hit.lon, 'pasted');
    // Show what was actually read, in place, before clearing - proof it
    // worked right where you typed, not just a toast that can be missed.
    showApplied(box, hit.lat, hit.lon);
    return;
  }
  if (looksLikeUrl(text)) {
    resolveLinkAndApply(box, text);
    return;
  }
  toast('Could not find coordinates in that', true);
}

$('#paste-location').addEventListener('keydown', (ev) => {
  // Enter, or a mobile keyboard's action key however it reports itself.
  if (ev.key !== 'Enter' && ev.key !== 'Go' && ev.keyCode !== 13) return;
  ev.preventDefault();                 // never let it move focus or do anything else
  submitLocationText(ev.target);
});

$('#apply-location').addEventListener('click', () => {
  submitLocationText($('#paste-location'));
});

$('#use-location').addEventListener('click', () => {
  const btn = $('#use-location');
  // Always clickable, so clicking it is never silent: when geolocation is not
  // available here it says so instead of doing nothing.
  if (!locationAvailable()) {
    toast('Browser location needs https or localhost on this page — paste coordinates below instead', true);
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Locating…';
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      const lat = Math.round(pos.coords.latitude * 10000) / 10000;
      const lon = Math.round(pos.coords.longitude * 10000) / 10000;
      applyLocation(lat, lon, `±${Math.round(pos.coords.accuracy)} m`);
      btn.textContent = 'Use my location';
      btn.disabled = false;
    },
    (err) => {
      const why = err.code === 1 ? 'permission denied'
        : err.code === 2 ? 'position unavailable' : 'timed out';
      toast('Could not get location: ' + why, true);
      btn.textContent = 'Use my location';
      btn.disabled = false;
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 });
});

refreshLocationNote();

/* ---------- auto-dim 24h forecast chart ----------
   A line-for-line port of ambiwled/daylight.py's sunrise equation and dome
   curve, so the graph matches the server exactly rather than approximating
   it - and so it can redraw instantly as a slider moves, with no round trip.
   Verified against the Python implementation for known dates/locations. */

const ZENITH = (-0.833) * Math.PI / 180;
const OBLIQUITY = 23.4397 * Math.PI / 180;
const J2000 = 2451545.0;
const DAY_MS = 86400000;

function sunTimesRaw(lat, lon, dayUtcMidnightMs) {
  const n = (dayUtcMidnightMs - Date.UTC(2000, 0, 1)) / DAY_MS + 0.0008;
  const jStar = n - lon / 360.0;
  const m = rad(((357.5291 + 0.98560028 * jStar) % 360 + 360) % 360);
  const c = 1.9148 * Math.sin(m) + 0.0200 * Math.sin(2 * m) + 0.0003 * Math.sin(3 * m);
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
  return {
    sunrise: fromJulian(jTransit - omega / 360.0),
    sunset: fromJulian(jTransit + omega / 360.0),
  };
}
function rad(d) { return d * Math.PI / 180; }
function deg(r) { return r * 180 / Math.PI; }
function fromJulian(j) { return (j - 2440587.5) * DAY_MS; }

const _sunTimesCache = new Map();
function sunTimesForDay(lat, lon, ms) {
  const dayStart = Date.UTC(new Date(ms).getUTCFullYear(), new Date(ms).getUTCMonth(), new Date(ms).getUTCDate());
  const key = lat + ',' + lon + ',' + dayStart;
  if (!_sunTimesCache.has(key)) {
    if (_sunTimesCache.size > 60) _sunTimesCache.clear();   // cheap unbounded-growth guard
    _sunTimesCache.set(key, sunTimesRaw(lat, lon, dayStart));
  }
  return _sunTimesCache.get(key);
}

function domeShape(s, exponent) {
  s = Math.min(Math.max(s, 0), 1);
  const base = 4.0 * s * (1.0 - s);
  return exponent === 1.0 ? base : Math.pow(base, exponent);
}

/* Mirrors DimmingSchedule.day_fraction() / level() exactly. */
function dimmingLevelAt(ms, dim) {
  const times = sunTimesForDay(dim.latitude, dim.longitude, ms);
  let frac;
  if (times.polar) {
    frac = times.polar === 'polar_night' ? 0.0 : 1.0;
  } else {
    const sunrise = times.sunrise + dim.sunrise_offset_minutes * 60000;
    const sunset = times.sunset + dim.sunset_offset_minutes * 60000;
    const span = sunset - sunrise;
    frac = (span <= 0 || ms < sunrise || ms > sunset) ? 0.0
      : domeShape((ms - sunrise) / span, dim.curve_exponent);
  }
  return dim.night_level + (dim.day_level - dim.night_level) * frac;
}

/* Mirrors ColourStage.effective_brightness exactly. */
function appliedFraction(level, col) {
  const product = (col.brightness == null ? 1 : col.brightness) * level;
  if (col.perceptual_brightness === false || product > 1.0) return product;
  return Math.pow(Math.max(product, 0), col.perceptual_gamma || 2.2);
}

function drawCurve(ctx, values, x0, y0, plotW, plotH, colour, dash, fill) {
  ctx.save();
  ctx.beginPath();
  values.forEach((v, i) => {
    const x = x0 + plotW * (i / (values.length - 1));
    const y = y0 + plotH * (1 - Math.min(Math.max(v, 0), 1));
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  if (fill) {
    ctx.lineTo(x0 + plotW, y0 + plotH);
    ctx.lineTo(x0, y0 + plotH);
    ctx.closePath();
    ctx.fillStyle = colour + '26';   // ~15% alpha fill under the primary curve
    ctx.fill();
    ctx.beginPath();
    values.forEach((v, i) => {
      const x = x0 + plotW * (i / (values.length - 1));
      const y = y0 + plotH * (1 - Math.min(Math.max(v, 0), 1));
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
  }
  ctx.setLineDash(dash || []);
  ctx.strokeStyle = colour;
  ctx.lineWidth = fill ? 2 : 1.5;
  ctx.stroke();
  ctx.restore();
}

function drawDimmingChart() {
  const cv = $('#dimming-chart');
  if (!cv || !cfg) return;
  const w = cv.clientWidth || 640, h = 170;
  const ctx = sizeCanvas(cv, w, h);
  const pad = { l: 36, r: 8, t: 10, b: 20 };
  const x0 = pad.l, y0 = pad.t, plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#0b0d10';
  ctx.fillRect(x0, y0, plotW, plotH);

  const dim = cfg.dimming, col = cfg.colour;
  const now = Date.now();
  const POINTS = 145;                        // one every ~10 minutes across 24h
  const stepMs = (24 * 3600000) / (POINTS - 1);
  const levels = [], applied = [];
  for (let i = 0; i < POINTS; i++) {
    const t = now + i * stepMs;
    const lvl = dim.enabled === false ? 1.0 : dimmingLevelAt(t, dim);
    levels.push(lvl);
    applied.push(appliedFraction(lvl, col));
  }

  // gridlines + y labels
  ctx.strokeStyle = '#20242c'; ctx.lineWidth = 1;
  ctx.font = '10px ui-sans-serif, system-ui, sans-serif';
  ctx.fillStyle = '#5c6472'; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  [0, 25, 50, 75, 100].forEach((pct) => {
    const y = y0 + plotH * (1 - pct / 100);
    ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(x0 + plotW, y); ctx.stroke();
    ctx.fillText(pct + '%', x0 - 5, y);
  });

  // sunrise/sunset markers within the visible window
  ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  const seenDays = new Set();
  for (let i = 0; i < POINTS; i++) {
    const t = now + i * stepMs;
    const dayKey = new Date(t).toISOString().slice(0, 10);
    if (seenDays.has(dayKey)) continue;
    seenDays.add(dayKey);
    const times = sunTimesForDay(dim.latitude, dim.longitude, t);
    if (times.polar) continue;
    for (const [label, at] of [['sunrise', times.sunrise], ['sunset', times.sunset]]) {
      if (at < now || at > now + 24 * 3600000) continue;
      const x = x0 + plotW * ((at - now) / (24 * 3600000));
      ctx.strokeStyle = '#3a4150'; ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(x, y0); ctx.lineTo(x, y0 + plotH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#5c6472';
      ctx.fillText(label === 'sunrise' ? '↑' : '↓', x, y0 + plotH + 2);
    }
  }

  drawCurve(ctx, levels, x0, y0, plotW, plotH, '#5c6472', [3, 3], false);
  drawCurve(ctx, applied, x0, y0, plotW, plotH, '#4da3ff', [], true);

  // "now" marker at the left edge, on the final-output curve
  ctx.fillStyle = '#4da3ff';
  ctx.beginPath();
  ctx.arc(x0, y0 + plotH * (1 - Math.min(Math.max(applied[0], 0), 1)), 3, 0, Math.PI * 2);
  ctx.fill();

  // x-axis hour labels
  ctx.fillStyle = '#5c6472'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  [0, 6, 12, 18, 24].forEach((hrs) => {
    const x = x0 + plotW * (hrs / 24);
    const label = hrs === 0 ? 'now'
      : new Date(now + hrs * 3600000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    ctx.fillText(label, x, y0 + plotH + 2);
  });
}

setInterval(drawDimmingChart, 60000);   // keep "now" accurate if the page is left open

window.addEventListener('resize', () => { geomCache = null; drawCeiling(); drawStrip(); drawDimmingChart(); });

bindFields();
loadConfig().then(connect).catch((e) => toast('Could not load config: ' + e.message, true));
