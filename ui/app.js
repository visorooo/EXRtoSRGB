/*
 * Front end for EXR -> sRGB.
 *
 * All real work happens in Python (window.pywebview.api); this file only
 * gathers settings, renders state, and debounces the preview. Anything that
 * touches pixels belongs in core.py.
 */

const $ = (id) => document.getElementById(id);

const state = {
  entries: [], // grouped file entries from core.group_sequences
  selected: 0,
  // Which frame of a sequence entry is being previewed. Reset on selection,
  // because frame 87 of one run means nothing in another.
  frame: 0,
  converting: false,
  ready: false,
};

/* ---------------------------------------------------------------------------
 * Settings
 * ------------------------------------------------------------------------ */

function settings() {
  return {
    config: $('config').value,
    src: $('input-cs').value,
    display: $('display').value,
    tone: $('look').value === 'tone',
    transfer: $('look').value === 'linear' ? 'linear' : 'display',
    format: $('format').value,
    quality: parseInt($('quality').value, 10),
    bits: parseInt($('bits').value, 10),
    alpha_mode: $('alpha').value,
    layer: $('layer').value,
    unpremult: $('unpremult').checked,
    suffix: $('suffix').checked ? suffixFor() : '',
    out_dir: $('outdir').value.trim(),
    all_layers: $('all-layers').checked,
  };
}

/*
 * Setting .value from code fires no change event and mutates no attribute, so
 * the custom trigger built by select.js cannot observe it. Every programmatic
 * write goes through here.
 */
function setValue(el, value) {
  el.value = value;
  if (el._select) el._select.sync();
}

/*
 * The suffix has to describe what is in the file. Naming a scene-linear render
 * "_srgb" is the same mistake as tagging it sRGB in the header: something
 * downstream applies a transfer function that was never there.
 */
function suffixFor() {
  return $('look').value === 'linear' ? '_linear' : '_srgb';
}

/*
 * Reflect what each container can actually carry, rather than accepting a
 * setting and quietly ignoring it. core.resolve_output() enforces the same
 * rules on the Python side - this only keeps the controls honest.
 *
 *   JPEG   - 8-bit, no alpha
 *   PNG    - 8 or 16-bit integer, no float
 *   TIFF   - 8, 16 or 32-bit float
 *   linear - float only, so TIFF 32-bit, and the display settings stop meaning
 *            anything because no display transform runs
 */
function syncFormat() {
  const linear = $('look').value === 'linear';
  if (linear) {
    setValue($('format'), 'tiff');
    setValue($('bits'), '32');
  }

  const fmt = $('format').value;
  const jpeg = fmt === 'jpeg';

  $('quality').disabled = !jpeg;
  $('format').disabled = linear;
  $('bits').disabled = jpeg || linear;

  // 32-bit is float, and only TIFF has float here. select.js renders the
  // disabled option greyed with this title, so the constraint explains itself
  // instead of the choice quietly not sticking.
  const bit32 = $('bits').querySelector('option[value="32"]');
  if (bit32) {
    bit32.disabled = fmt !== 'tiff';
    bit32.title = bit32.disabled
      ? '32-bit is floating point — only TIFF can carry it'
      : '';
  }
  if (jpeg) setValue($('bits'), '8');
  else if (fmt !== 'tiff' && $('bits').value === '32') setValue($('bits'), '16');

  // Nothing is being transformed, so the colour controls are inert
  $('input-cs').disabled = linear;
  $('display').disabled = linear;
  $('alpha').disabled = false;

  $('linear-note').hidden = !linear;
  $('suffix-label').textContent = 'Add “' + suffixFor() + '”';
}

function fillSelect(el, values, current) {
  el.innerHTML = '';
  for (const v of values) {
    const o = document.createElement('option');
    if (typeof v === 'string') {
      o.value = o.textContent = v;
    } else {
      o.value = v.value;
      o.textContent = v.label;
    }
    el.appendChild(o);
  }
  if (current && values.some((v) => (v.value ?? v) === current)) el.value = current;
  if (el._select) el._select.sync();
}

/* ---------------------------------------------------------------------------
 * File list
 * ------------------------------------------------------------------------ */

function renderFiles() {
  const list = $('filelist');
  list.innerHTML = '';

  if (!state.entries.length) {
    list.innerHTML = `
      <div class="empty">
        <div class="big">Drop .exr files or folders here</div>
        <div class="small">or use Add files · sequences are grouped automatically</div>
      </div>`;
    setPreview(null);
    return;
  }

  state.entries.forEach((e, i) => {
    const row = document.createElement('div');
    row.className = 'row' + (i === state.selected ? ' selected' : '');

    const name = document.createElement('span');
    name.className = 'name';
    // &lrm; keeps the RTL ellipsis trick from reordering the leading character
    name.textContent = '‎' + e.label;
    name.title = e.dir;
    row.appendChild(name);

    if (e.kind === 'sequence') {
      const b = document.createElement('span');
      b.className = 'badge seq';
      b.textContent = `${e.count} frames · ${e.first}–${e.last}`;
      row.appendChild(b);
    }

    const x = document.createElement('button');
    x.className = 'ghost drop-x';
    x.textContent = '×';
    x.title = 'Remove';
    x.onclick = (ev) => {
      ev.stopPropagation();
      removeEntry(i);
    };
    row.appendChild(x);

    row.onclick = () => {
      state.selected = i;
      state.frame = 0;
      renderFiles();
      refreshCrypto();
      schedulePreview();
    };
    list.appendChild(row);
  });
}

async function removeEntry(i) {
  state.entries = await window.pywebview.api.remove_entry(i);
  if (state.selected >= state.entries.length) {
    state.selected = Math.max(0, state.entries.length - 1);
  }
  renderFiles();
  await refreshLayers();
  await refreshCrypto();
  schedulePreview();
}

function totalFrames() {
  return state.entries.reduce((n, e) => n + e.count, 0);
}

function setStatus(text) {
  $('status').textContent = text;
}

/* ---------------------------------------------------------------------------
 * Preview
 * ------------------------------------------------------------------------ */

let previewTimer = null;
let previewToken = 0;

/* Previews re-render on every settings change, and a full ACES transform is not
   free. Coalesce bursts (dragging through a dropdown) into one render. */
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(renderPreview, 180);
}

function setPreview(uri) {
  const box = $('preview-box');
  box.innerHTML = '';
  if (!uri) {
    box.innerHTML = '<div class="placeholder">Add a file to preview the conversion</div>';
    $('preview-layer').textContent = '';
    $('preview-dims').textContent = '';
    $('preview-note').textContent = '';
    return;
  }
  const img = new Image();
  img.src = uri;
  box.appendChild(img);
}

function setPreviewLoading() {
  const box = $('preview-box');
  box.innerHTML = '<div class="spinner"></div>';
}

async function renderPreview() {
  if (!state.ready || !state.entries.length || state.converting) return;
  const token = ++previewToken;
  setPreviewLoading();
  try {
    if (crypto.mode === 'crypto') {
      const type = currentCrypto();
      if (!type) return;
      const c = await window.pywebview.api.crypto_preview(
        state.selected, type.id, [...selectionFor(type.id)], previewPx());
      if (token !== previewToken) return;
      if (c.error) {
        $('preview-box').innerHTML =
          `<div class="placeholder">${escapeHtml(c.error)}</div>`;
        return;
      }
      setPreview(c.uri);
      $('preview-layer').textContent = type.label;
      $('preview-dims').textContent = `${c.width} × ${c.height}`;
      $('preview-note').textContent = '';
      return;
    }
    const r = await window.pywebview.api.view(
      state.selected, settings(), viewer.exposure, viewer.gamma,
      viewer.channel, previewPx(), state.frame);
    if (token !== previewToken) return; // a newer request won
    if (r.error) {
      $('preview-box').innerHTML =
        `<div class="placeholder">${escapeHtml(r.error)}</div>`;
      $('preview-layer').textContent = '';
      $('preview-dims').textContent = '';
      $('preview-note').textContent = '';
      return;
    }
    setPreview(r.uri);
    $('preview-layer').textContent = r.layer === '' ? 'R,G,B' : r.layer;
    $('preview-dims').textContent = `${r.full_width} × ${r.full_height}`;
    $('preview-note').textContent = r.note || '';
    syncFrames(r);
  } catch (err) {
    if (token === previewToken) {
      $('preview-box').innerHTML =
        `<div class="placeholder">${escapeHtml(String(err))}</div>`;
    }
  }
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

/* ---------------------------------------------------------------------------
 * Sequence frames
 *
 * Stepping only, and that is a measurement rather than a shortcut: one preview
 * frame costs 125 ms at 1080p and 860 ms on a 2160 square 80-channel file, and
 * halving the preview size barely moves either - the cost is decoding the EXR.
 * Playback would need the frames pre-rendered into a cache, which is the same
 * latency problem the viewer's GPU path solves, so it waits for that.
 * ------------------------------------------------------------------------ */

function syncFrames(r) {
  const strip = $('frames');
  const n = r && r.frames ? r.frames : 1;
  strip.hidden = n < 2;
  if (strip.hidden) return;
  const slider = $('fr-slider');
  slider.max = String(n - 1);
  slider.value = String(r.frame);
  state.frame = r.frame;
  $('fr-count').textContent = `${r.frame + 1} / ${n}`;
  $('fr-prev').disabled = r.frame <= 0;
  $('fr-next').disabled = r.frame >= n - 1;
}

function stepFrame(d) {
  const strip = $('frames');
  if (strip.hidden) return;
  const max = parseInt($('fr-slider').max, 10) || 0;
  const next = Math.max(0, Math.min(max, state.frame + d));
  if (next === state.frame) return;
  state.frame = next;
  $('fr-slider').value = String(next);
  $('fr-count').textContent = `${next + 1} / ${max + 1}`;
  schedulePreview();
}

function wireFrames() {
  $('fr-prev').onclick = () => stepFrame(-1);
  $('fr-next').onclick = () => stepFrame(1);
  // 'input' rather than 'change', so dragging scrubs; schedulePreview already
  // debounces, so a fast drag collapses into one render.
  $('fr-slider').oninput = () => {
    state.frame = parseInt($('fr-slider').value, 10) || 0;
    const max = parseInt($('fr-slider').max, 10) || 0;
    $('fr-count').textContent = `${state.frame + 1} / ${max + 1}`;
    schedulePreview();
  };
}

/* ---------------------------------------------------------------------------
 * Log
 * ------------------------------------------------------------------------ */

function log(text, cls) {
  const el = document.createElement('div');
  if (cls) el.className = cls;
  el.textContent = text;
  $('log').appendChild(el);
  $('log').scrollTop = $('log').scrollHeight;
}

function clearLog() {
  $('log').innerHTML = '';
}

/* Called from Python via evaluate_js. */
window.onProgress = (done, total) => {
  $('bar').style.width = total ? `${(done / total) * 100}%` : '0%';
  setStatus(`${done} / ${total}`);
};

window.onLog = (text, cls) => log(text, cls);

window.onDone = (ok, fail, warned) => {
  state.converting = false;
  $('btn-convert').disabled = false;
  $('btn-cancel').disabled = true;
  updateCryptoCount();
  const p = $('progress');
  p.classList.toggle('failed', fail > 0);
  p.classList.toggle('done', fail === 0);
  const extra = warned ? `, ${warned} with warnings` : '';
  log(`Done. ${ok} converted, ${fail} failed${extra}.`,
      fail ? 'err' : warned ? 'warn' : 'ok');
  setStatus(`${ok} ok · ${fail} fail`);
  renderPreview();
};

/* Python pushes the file list here after a drop, since the drop handler runs
   on the Python side (that is where the real paths are). */
window.onFilesChanged = async (entries, selectLabel) => {
  const prev = state.entries[state.selected];
  state.entries = entries;
  // Selecting whatever was just dropped is almost always what is wanted; the
  // alternative is previewing an unrelated file you added ten minutes ago.
  let next = -1;
  if (selectLabel) next = entries.findIndex((e) => e.label === selectLabel);
  if (next < 0 && prev) next = entries.findIndex((e) => e.label === prev.label);
  state.selected = next >= 0 ? next : 0;
  if (state.selected >= entries.length) state.selected = 0;
  // Compared by label, not by index: adding a file can leave the selection on
  // the same row number while that row is now a different sequence, and frame
  // 87 of one run means nothing in another. A drop also changes the selection
  // without going through the row click that would otherwise reset it.
  const now = entries[state.selected];
  if (!prev || !now || prev.label !== now.label) state.frame = 0;
  renderFiles();
  await refreshLayers();
  await refreshCrypto();
  schedulePreview();
};

window.onDragState = (on) => {
  $('files-panel').classList.toggle('dragging', !!on);
};

/* ---------------------------------------------------------------------------
 * Viewer
 *
 * Exposure and gamma do not re-read the file - Python keeps the decoded layer
 * in a ViewerSession - so these can be dragged live. The render is still tens
 * of milliseconds, so requests are coalesced the same way the preview is.
 * ------------------------------------------------------------------------ */

const viewer = { exposure: 0, gamma: 1, channel: 'rgb' };

/*
 * picking - the eyedropper is armed and waiting for a click.
 * locked  - a reading was taken and must stop following the cursor.
 * hex     - whatever the swatch is currently showing, for the copy.
 */
const probe = { picking: false, locked: false, hex: null };

function fmtStops(v) {
  return (v > 0 ? '+' : '') + v.toFixed(1);
}

function wireViewer() {
  const exposure = $('exposure');
  const gamma = $('gamma');

  const onExposure = () => {
    viewer.exposure = parseFloat(exposure.value);
    $('exposure-val').textContent = fmtStops(viewer.exposure);
    schedulePreview();
  };
  const onGamma = () => {
    viewer.gamma = parseFloat(gamma.value);
    $('gamma-val').textContent = viewer.gamma.toFixed(2);
    schedulePreview();
  };
  exposure.oninput = onExposure;
  gamma.oninput = onGamma;

  for (const b of $('pv-channels').querySelectorAll('button')) {
    b.onclick = () => {
      viewer.channel = b.dataset.ch;
      for (const o of $('pv-channels').querySelectorAll('button')) {
        o.classList.toggle('is-active', o === b);
      }
      schedulePreview();
    };
  }

  $('viewer-reset').onclick = () => {
    exposure.value = '0';
    gamma.value = '1';
    onExposure();
    onGamma();
  };

  wireProbe();
}

/*
 * The pixel under the cursor, reported twice on purpose.
 *
 * The text is the linear scene value, read from the full-resolution layer so it
 * is the real pixel rather than something resampled for the preview. The chip
 * and the hex are the display colour - what is actually on screen after
 * exposure, gamma and the view transform - because that is the only thing a hex
 * code can mean to anywhere you would paste it.
 */
function wireProbe() {
  const box = $('preview-box');
  const swatch = $('pv-swatch');
  let probeTimer = null;

  const paint = (p) => {
    const a = p.a === null || p.a === undefined ? null : p.a;
    renderProbeValues($('probe'), p, a);
    if (!p.hex) return;
    probe.hex = p.hex;
    $('pv-hex').textContent = p.hex;
    swatch.hidden = false;
    // alpha kept on the chip, so a soft edge does not read as a solid colour
    const al = a === null ? 1 : Math.min(1, Math.max(0, a));
    swatch.style.setProperty('--swatch', `rgb(${p.dr} ${p.dg} ${p.db} / ${al})`);
  };

  const at = (e) => {
    const img = box.querySelector('img');
    if (!img) return null;
    const r = img.getBoundingClientRect();
    const u = (e.clientX - r.left) / r.width;
    const v = (e.clientY - r.top) / r.height;
    return u < 0 || u > 1 || v < 0 || v > 1 ? null : [u, v];
  };

  const read = async (u, v) =>
    window.pywebview.api.probe(state.selected, u, v, settings(),
                               viewer.exposure, viewer.gamma);

  box.addEventListener('mousemove', (e) => {
    if (crypto.mode === 'crypto' || probe.locked) return;
    const uv = at(e);
    if (!uv) return;
    clearTimeout(probeTimer);
    probeTimer = setTimeout(async () => {
      const p = await read(uv[0], uv[1]);
      if (p && p.r !== undefined && !probe.locked) paint(p);
    }, 40);
  });

  box.addEventListener('mouseleave', () => {
    if (probe.locked) return;
    $('probe').textContent = 'Hover the image for pixel values';
    $('pv-hex').textContent = '';
    swatch.hidden = true;
  });

  // Arming is deliberate: hover already shows the colour, and what the
  // eyedropper adds is holding one still long enough to use it.
  $('pv-pick').onclick = () => setPicking(!probe.picking);

  box.addEventListener('click', async (e) => {
    // Ctrl / Alt belong to cryptomatte picking, which was here first.
    if (!probe.picking || e.ctrlKey || e.metaKey || e.altKey) return;
    const uv = at(e);
    if (!uv) return;
    const p = await read(uv[0], uv[1]);
    if (!p || p.r === undefined) return;
    probe.locked = false;          // paint() refuses to draw while locked
    paint(p);
    probe.locked = true;
    swatch.classList.add('locked');
    setPicking(false);
    if (p.hex) {
      await window.pywebview.api.copy_text(p.hex);
      log(`Copied ${p.hex}`, 'ok');
    }
  });

  swatch.onclick = async () => {
    if (!probe.hex) return;
    await window.pywebview.api.copy_text(probe.hex);
    log(`Copied ${probe.hex}`, 'ok');
  };
}

/*
 * Copy on click, for anything numeric in the readout.
 *
 * The window runs with text_select=False, so dragging across a number is
 * unreliable even where the CSS re-enables it - and these are exactly the values
 * anyone would want to paste into a comp or a bug report. Each one is its own
 * control, and the whole line copies together.
 */
function copyable(text, title) {
  const b = document.createElement('button');
  b.className = 'val';
  b.type = 'button';
  b.textContent = text;
  b.title = title ? `${title} — click to copy` : 'Click to copy';
  b.onclick = async (e) => {
    e.stopPropagation();
    await window.pywebview.api.copy_text(text);
    log(`Copied ${text}`, 'ok');
  };
  return b;
}

/*
 * Build the probe readout as copyable pieces.
 *
 * Shared by both the converter and, in spirit, the viewer: the values are the
 * same and so is the reason for making them clickable.
 */
function renderProbeValues(el, p, a) {
  const f4 = (v) => v.toFixed(4);
  el.textContent = '';
  const row = (label, ...kids) => {
    const r = document.createElement('div');
    r.className = 'probe-line';
    const l = document.createElement('span');
    l.className = 'probe-label';
    l.textContent = label;
    r.appendChild(l);
    for (const k of kids) r.appendChild(k);
    el.appendChild(r);
  };

  row('pos', copyable(`${p.x}, ${p.y}`, 'Pixel coordinate'));

  const lin = [f4(p.r), f4(p.g), f4(p.b)];
  row('linear',
      copyable(lin[0], 'Red, linear'),
      copyable(lin[1], 'Green, linear'),
      copyable(lin[2], 'Blue, linear'),
      copyable(lin.join(' '), 'All three, linear'));

  if (p.dr !== undefined) {
    const disp = [p.dr, p.dg, p.db];
    row('display',
        copyable(String(disp[0]), 'Red, 8-bit display'),
        copyable(String(disp[1]), 'Green, 8-bit display'),
        copyable(String(disp[2]), 'Blue, 8-bit display'),
        copyable(disp.join(' '), 'All three, 8-bit display'));
  }

  row('alpha', copyable(a === null ? '—' : a.toFixed(4), 'Alpha'));
  if (p.hex) row('hex', copyable(p.hex, 'Display colour'));
}

/*
 * Arm or disarm the eyedropper.
 *
 * Deliberately does NOT release a held reading: taking a sample disarms, and if
 * that also unlocked, the reading would start following the cursor again the
 * moment it was taken - which is the one thing the eyedropper exists to stop.
 * Arming again does unlock, because that is a request to pick a new one.
 */
function setPicking(on) {
  probe.picking = on;
  $('pv-pick').setAttribute('aria-pressed', String(on));
  $('preview-box').classList.toggle('picking', on);
  if (on) unlockProbe();
}

/* Let the readout follow the cursor again. */
function unlockProbe() {
  probe.locked = false;
  $('pv-swatch').classList.remove('locked');
}

/* ---------------------------------------------------------------------------
 * Cryptomatte
 *
 * Selection is held per cryptomatte type, so switching between CryptoObject and
 * CryptoMaterial and back does not lose what was already ticked.
 * ------------------------------------------------------------------------ */

const crypto = { types: [], selected: new Map(), mode: 'beauty' };

/* Output and Cryptomatte are tabs on one panel; stacked they overflowed. */
function showTab(which) {
  const isCrypto = which === 'crypto';
  $('tab-output').classList.toggle('is-active', !isCrypto);
  $('tab-crypto').classList.toggle('is-active', isCrypto);
  $('body-output').hidden = isCrypto;
  $('body-crypto').hidden = !isCrypto;
  // The ID view is only meaningful beside the object list, so the two follow
  // each other rather than being two things to remember to switch.
  setPreviewMode(isCrypto ? 'crypto' : 'beauty');
}

/*
 * Preview size presets.
 *
 * Each widens the column and raises the render resolution together - a wider
 * box showing the same 512px image would just be blurry. Large is mainly for
 * cryptomatte picking, where small objects are hard to hit accurately.
 */
const PREVIEW_SIZES = { s: 280, m: 360, l: 560 };
const PREVIEW_MIN = 240;
const PREVIEW_MAX = 900;

/*
 * Render tiers, deliberately coarse and separate from the column width.
 *
 * The layout is freeform, but a render costs tens of milliseconds and scales
 * with area, so it snaps to a few sizes and the browser scales the image the
 * rest of the way. Dragging therefore stays smooth: nothing re-renders until
 * the drag ends, and even then only if the tier actually changed.
 */
const RENDER_TIERS = [384, 512, 900];

function previewPx() {
  const w = state.previewW || PREVIEW_SIZES.m;
  return RENDER_TIERS.find((t) => t >= w * 1.15) ?? RENDER_TIERS[RENDER_TIERS.length - 1];
}

function applyPreviewWidth(px) {
  const w = Math.round(Math.min(PREVIEW_MAX, Math.max(PREVIEW_MIN, px)));
  const before = previewPx();
  state.previewW = w;
  document.documentElement.style.setProperty('--preview-w', w + 'px');
  // The preset buttons light up when the width happens to match one.
  for (const b of $('pv-sizes').querySelectorAll('button')) {
    b.classList.toggle('is-active', PREVIEW_SIZES[b.dataset.size] === w);
  }
  return previewPx() !== before;   // did the render tier change?
}

function setPreviewWidth(px, { persist = true, rerender = true } = {}) {
  const tierChanged = applyPreviewWidth(px);
  if (persist) window.pywebview.api.set_preview_width(state.previewW);
  if (rerender && tierChanged) schedulePreview();
}

function wireResizer() {
  const handle = $('resizer');
  let startX = 0;
  let startW = 0;

  const onMove = (e) => {
    // dragging left widens the preview, since it is the right-hand column
    applyPreviewWidth(startW - (e.clientX - startX));
  };
  const onUp = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    handle.classList.remove('is-dragging');
    document.body.classList.remove('resizing');
    // Re-render once, at the end, rather than on every pixel of the drag.
    window.pywebview.api.set_preview_width(state.previewW);
    schedulePreview();
  };

  handle.addEventListener('pointerdown', (e) => {
    startX = e.clientX;
    startW = state.previewW || PREVIEW_SIZES.m;
    handle.classList.add('is-dragging');
    document.body.classList.add('resizing');
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    e.preventDefault();
  });

  // Keyboard, because a drag handle that only takes a mouse is not a control.
  handle.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 40 : 10;
    if (e.key === 'ArrowLeft') setPreviewWidth((state.previewW || 360) + step);
    else if (e.key === 'ArrowRight') setPreviewWidth((state.previewW || 360) - step);
    else return;
    e.preventDefault();
  });

  // Double-click snaps back to the middle preset.
  handle.addEventListener('dblclick', () => setPreviewWidth(PREVIEW_SIZES.m));
}

function setPreviewMode(mode) {
  if (mode === 'crypto' && !crypto.types.length) mode = 'beauty';
  crypto.mode = mode;
  for (const b of $('pv-modes').querySelectorAll('button')) {
    b.classList.toggle('is-active', b.dataset.mode === mode);
  }
  $('pick-hint').hidden = mode !== 'crypto';
  $('viewer-controls').hidden = mode === 'crypto';
  schedulePreview();
}

function currentCrypto() {
  return crypto.types.find((t) => t.id === $('crypto-type').value) || null;
}

function selectionFor(id) {
  if (!crypto.selected.has(id)) crypto.selected.set(id, new Set());
  return crypto.selected.get(id);
}

async function refreshCrypto() {
  const panel = $('tab-crypto');
  crypto.types = [];
  crypto.selected.clear();

  if (!state.entries.length) {
    panel.hidden = true;
    if (crypto.mode === 'crypto') showTab('output');
    $('pv-modes').hidden = true;
    return;
  }
  const r = await window.pywebview.api.cryptomattes(state.selected);
  crypto.types = (r.types || []).filter((t) => !t.incomplete);

  const unusable = (r.types || []).filter((t) => t.incomplete);
  for (const t of unusable) {
    log(`Cryptomatte “${t.label}” is unusable (no manifest or rank channels).`,
        'warn');
  }
  if (!crypto.types.length) {
    panel.hidden = true;
    $('pv-modes').hidden = true;
    if (crypto.mode === 'crypto') showTab('output');
    return;
  }
  $('pv-modes').hidden = false;
  panel.hidden = false;
  fillSelect(
    $('crypto-type'),
    crypto.types.map((t) => ({ value: t.id, label: `${t.label} · ${t.objects.length}` }))
  );
  renderObjects();
}

function renderObjects() {
  const box = $('crypto-objects');
  const type = currentCrypto();
  box.innerHTML = '';
  if (!type) return;

  const sel = selectionFor(type.id);
  const needle = $('crypto-filter').value.trim().toLowerCase();
  const shown = type.objects.filter(
    (n) => !needle || n.toLowerCase().includes(needle));

  if (!shown.length) {
    box.innerHTML = '<div class="empty-msg">No objects match</div>';
    updateCryptoCount();
    return;
  }
  for (const name of shown) {
    const label = document.createElement('label');
    label.className = 'check';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = sel.has(name);
    input.onchange = () => {
      if (input.checked) sel.add(name);
      else sel.delete(name);
      updateCryptoCount();
      if (crypto.mode === 'crypto') schedulePreview();
    };
    const box_ = document.createElement('span');
    box_.className = 'box';
    const text = document.createElement('span');
    text.textContent = name;      // may be arbitrary Unicode; never innerHTML
    text.title = name;
    label.append(input, box_, text);
    box.appendChild(label);
  }
  updateCryptoCount();
}

function updateCryptoCount() {
  const type = currentCrypto();
  const n = type ? selectionFor(type.id).size : 0;
  $('crypto-count').textContent = n ? `${n} selected` : '';
  $('btn-mattes').disabled = n === 0;
}

/*
 * Ctrl-click the ID view to toggle the object under the cursor.
 *
 * The click is converted to a coordinate in the *image*, not the box: the
 * preview is object-fit:contain, so there are letterbox bands that belong to no
 * pixel. getBoundingClientRect on the <img> already excludes them.
 */
function wirePicking() {
  const box = $('preview-box');

  // Ctrl adds, Alt removes. Explicit beats toggling: working quickly you stop
  // having to remember what state each object is already in.
  const armed = (e) =>
    crypto.mode === 'crypto' && (e.ctrlKey || e.metaKey || e.altKey);
  const update = (e) => box.classList.toggle('pickable', armed(e));
  document.addEventListener('keydown', update);
  document.addEventListener('keyup', update);
  box.addEventListener('mousemove', update);

  box.addEventListener('click', async (e) => {
    if (!armed(e)) return;
    const img = box.querySelector('img');
    const type = currentCrypto();
    if (!img || !type) return;

    const r = img.getBoundingClientRect();
    const u = (e.clientX - r.left) / r.width;
    const v = (e.clientY - r.top) / r.height;
    if (u < 0 || u > 1 || v < 0 || v > 1) return;

    const res = await window.pywebview.api.pick_object(
      state.selected, type.id, u, v);
    if (!res.name) {
      log('Nothing there — that pixel has no object.', 'dim');
      return;
    }
    const sel = selectionFor(type.id);
    if (e.altKey) {
      if (sel.delete(res.name)) log(`− ${res.name}`, 'dim');
    } else {
      sel.add(res.name);
      log(`+ ${res.name}`, 'ok');
    }
    renderObjects();
    schedulePreview();
  });
}

function wireCrypto() {
  $('tab-output').onclick = () => showTab('output');
  $('tab-crypto').onclick = () => showTab('crypto');
  for (const b of $('pv-modes').querySelectorAll('button')) {
    b.onclick = () => setPreviewMode(b.dataset.mode);
  }
  for (const b of $('pv-sizes').querySelectorAll('button')) {
    b.onclick = () => setPreviewWidth(PREVIEW_SIZES[b.dataset.size]);
  }
  $('crypto-type').onchange = () => { renderObjects(); schedulePreview(); };
  $('crypto-filter').oninput = renderObjects;

  // Select all / clear act on what the filter is showing, which is what you
  // expect after typing a name fragment.
  $('crypto-all').onclick = () => {
    const type = currentCrypto();
    if (!type) return;
    const sel = selectionFor(type.id);
    const needle = $('crypto-filter').value.trim().toLowerCase();
    type.objects
      .filter((n) => !needle || n.toLowerCase().includes(needle))
      .forEach((n) => sel.add(n));
    renderObjects();
    if (crypto.mode === 'crypto') schedulePreview();
  };
  $('crypto-none').onclick = () => {
    const type = currentCrypto();
    if (type) selectionFor(type.id).clear();
    renderObjects();
    if (crypto.mode === 'crypto') schedulePreview();
  };

  $('btn-mattes').onclick = async () => {
    const type = currentCrypto();
    if (!type) return;
    const names = [...selectionFor(type.id)];
    if (!names.length) return;

    state.converting = true;
    $('btn-convert').disabled = true;
    $('btn-mattes').disabled = true;
    $('btn-cancel').disabled = false;
    $('progress').classList.remove('done', 'failed');
    $('bar').style.width = '0%';
    clearLog();
    await window.pywebview.api.export_mattes(
      state.selected,
      {
        ...settings(),
        matte_mode: $('matte-mode').value,
        matte_combine: $('matte-split').value === 'combined',
      },
      type.id,
      names
    );
  };
}

/* ---------------------------------------------------------------------------
 * Colour option plumbing
 * ------------------------------------------------------------------------ */

async function refreshColorOptions() {
  const cfg = $('config').value;
  if (cfg === '__custom__') {
    const picked = await window.pywebview.api.pick_config();
    if (!picked.ok) {
      setValue($('config'), state.lastConfig);
      return;
    }
    await reloadConfigList(picked.config);
    return;
  }
  state.lastConfig = cfg;
  const r = await window.pywebview.api.color_options(cfg);
  fillSelect($('input-cs'), r.inputs, r.default_input);
  fillSelect($('display'), r.displays, r.default_display);
  schedulePreview();
}

async function reloadConfigList(select) {
  const r = await window.pywebview.api.config_list();
  fillSelect($('config'), r.configs, select || r.current);
  state.lastConfig = $('config').value;
  await refreshColorOptions();
}

async function refreshLayers() {
  if (!state.entries.length) {
    fillSelect($('layer'), [{ value: '__auto__', label: 'Auto · detect beauty' }]);
    return;
  }
  const r = await window.pywebview.api.layers(state.selected);
  const opts = [{ value: '__auto__', label: 'Auto · detect beauty' }];
  for (const l of r.layers) {
    opts.push({ value: l, label: l === '' ? '(no layer · R,G,B)' : l });
  }
  fillSelect($('layer'), opts, $('layer').value);
}

/* ---------------------------------------------------------------------------
 * Wiring
 * ------------------------------------------------------------------------ */

/*
 * Drag feedback, and the reason drops work at all.
 *
 * A browser only fires `drop` if `dragover` called preventDefault() — otherwise
 * it treats the page as a non-drop-target and hands the file to its default
 * handler instead. That has to happen synchronously in JS: pywebview dispatches
 * DOM events to Python on a worker thread, which is far too late to prevent a
 * default. Python still owns the drop itself, because that is where the real
 * file paths are injected; this only clears the way and paints the state.
 *
 * dragenter/dragleave fire for every child element crossed, so the highlight is
 * refcounted rather than toggled, or it flickers as the cursor moves.
 */
function wireDrag() {
  let depth = 0;
  const panel = () => $('files-panel');

  const allow = (e) => {
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
  };

  window.addEventListener('dragenter', (e) => {
    allow(e);
    depth += 1;
    panel().classList.add('dragging');
  });
  window.addEventListener('dragover', allow);
  window.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (depth === 0) panel().classList.remove('dragging');
  });
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    depth = 0;
    panel().classList.remove('dragging');
  });
}

function wire() {
  $('btn-add-files').onclick = async () => {
    await window.pywebview.api.add_files_dialog();
  };
  $('btn-add-folder').onclick = async () => {
    await window.pywebview.api.add_folder_dialog();
  };
  $('btn-clear').onclick = async () => {
    await window.pywebview.api.clear();
  };
  $('btn-refresh').onclick = () => renderPreview();

  $('btn-open-window').onclick = async () => {
    if (!state.entries.length) return;
    const r = await window.pywebview.api.open_in_window(state.selected);
    if (!r.ok) log('Could not open viewer: ' + (r.error || ''), 'err');
  };

  $('assoc').onchange = async () => {
    const r = await window.pywebview.api.set_association($('assoc').checked);
    if (!r.ok) $('assoc').checked = !$('assoc').checked;
  };

  $('ctx').onchange = async () => {
    const r = await window.pywebview.api.set_context_menu($('ctx').checked);
    if (!r.ok) $('ctx').checked = !$('ctx').checked;
  };

  $('btn-theme').onclick = () => {
    const next =
      document.documentElement.getAttribute('data-theme') === 'light'
        ? 'dark'
        : 'light';
    setTheme(next);
    window.pywebview.api.set_theme(next);
  };

  $('btn-outdir').onclick = async () => {
    const d = await window.pywebview.api.pick_outdir();
    if (d) $('outdir').value = d;
  };
  $('btn-outdir-clear').onclick = () => {
    $('outdir').value = '';
  };

  $('btn-convert').onclick = async () => {
    if (!state.entries.length) {
      log('Add some .exr files first.', 'warn');
      return;
    }
    state.converting = true;
    $('btn-convert').disabled = true;
    $('btn-cancel').disabled = false;
    $('progress').classList.remove('done', 'failed');
    $('bar').style.width = '0%';
    clearLog();
    await window.pywebview.api.convert(settings());
  };

  $('btn-cancel').onclick = async () => {
    $('btn-cancel').disabled = true;
    setStatus('Cancelling…');
    await window.pywebview.api.cancel();
  };

  $('config').onchange = refreshColorOptions;
  $('format').onchange = () => {
    syncFormat();
    schedulePreview();
  };
  // 'look' shares the format handler: picking scene-linear has to pull the
  // container and bit depth along with it.
  $('look').onchange = () => {
    syncFormat();
    schedulePreview();
  };
  for (const id of ['input-cs', 'display', 'alpha', 'layer', 'bits',
                    'quality', 'unpremult']) {
    $(id).onchange = schedulePreview;
  }

  wireFrames();
  wireKeys();

  // Picking one layer and "every layer" are mutually exclusive statements.
  $('all-layers').onchange = () => {
    $('layer').disabled = $('all-layers').checked;
    schedulePreview();
  };
}

/*
 * Global keys, and the sheet that admits they exist.
 *
 * One listener rather than several: the sheet has to swallow Esc before the
 * cancel binding sees it, and two independent handlers would both fire.
 */
function wireKeys() {
  const sheet = $('prefsheet');
  const show = (on) => {
    sheet.hidden = !on;
    $('btn-prefs').setAttribute('aria-expanded', String(on));
  };

  $('btn-prefs').onclick = () => show(sheet.hidden);
  $('prefsheet-close').onclick = () => show(false);
  // Clicking the backdrop closes; clicking the card must not.
  sheet.onclick = (e) => {
    if (e.target === sheet) show(false);
  };

  document.addEventListener('keydown', (e) => {
    // A text field owns its own keystrokes - "?" in the output path is a "?".
    const typing =
      e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT';

    if (e.key === 'Escape') {
      // Most-recent thing first: the sheet, then a held colour reading, and
      // only then the convert. Cancelling a batch by accident is expensive.
      if (!sheet.hidden) {
        show(false);
      } else if (probe.picking || probe.locked) {
        setPicking(false);
        unlockProbe();
      } else if (state.converting) {
        $('btn-cancel').click();
      }
      return;
    }

    if ((e.key === 'e' || e.key === 'E') && !typing && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      setPicking(!probe.picking);
      return;
    }

    // , and . step frames, the way every NLE does it
    if ((e.key === ',' || e.key === '.') && !typing) {
      e.preventDefault();
      stepFrame(e.key === '.' ? 1 : -1);
      return;
    }

    // "?" still opens it. The button is gone, not the shortcut - and the sheet
    // it opens is the only place the shortcut is written down.
    if (e.key === '?' && !typing) {
      e.preventDefault();
      show(sheet.hidden);
      return;
    }

    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!state.converting) $('btn-convert').click();
      return;
    }

    if ((e.key === 'o' || e.key === 'O') && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      $('btn-add-files').click();
      return;
    }

    // arrow keys move through the file list
    if (typing || !state.entries.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const d = e.key === 'ArrowDown' ? 1 : -1;
      state.selected = Math.min(
        state.entries.length - 1, Math.max(0, state.selected + d));
      renderFiles();
      refreshCrypto();
      schedulePreview();
    }
  });
}

/*
 * Set every control from code rather than trusting the markup.
 *
 * WebView2 restores form state from its profile across launches, which silently
 * flipped "un-premultiply" off between runs. That default is load-bearing -
 * without it every antialiased edge converts wrong - so it cannot be left to
 * whatever the embedded browser remembers.
 */
/*
 * theme.css keys everything off data-theme on <html>, so switching is one
 * attribute write. It is always set explicitly rather than left to the OS: the
 * preference is stored per-user by Python and should not change under you
 * because Windows switched to night mode.
 */
function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = $('btn-theme');
  if (btn) {
    btn.title = theme === 'light' ? 'Switch to dark' : 'Switch to light';
  }
}

function applyDefaults() {
  setValue($('look'), 'tone');
  setValue($('format'), 'png');
  setValue($('bits'), '16');
  setValue($('quality'), '95');
  setValue($('alpha'), 'keep');
  $('unpremult').checked = true;
  $('suffix').checked = true;
  $('all-layers').checked = false;
  $('outdir').value = '';
}

/* ---------------------------------------------------------------------------
 * Presets
 * ------------------------------------------------------------------------ */

/*
 * Apply a saved settings blob.
 *
 * Order matters. The colour lists depend on the config, so that goes first and
 * is awaited before input space and display are set - writing them against the
 * previous config's options silently drops them. syncFormat() runs last because
 * it is what enforces the container rules, and a preset is just another
 * settings blob arriving from outside.
 */
async function applyPreset(blob) {
  if (!blob) return;
  if (blob.config && blob.config !== $('config').value) {
    setValue($('config'), blob.config);
    await refreshColorOptions();
  }
  const pick = (id, v) => {
    if (v === undefined || v === null) return;
    if ([...$(id).options].some((o) => o.value === String(v))) {
      setValue($(id), String(v));
    }
  };
  pick('input-cs', blob.src);
  pick('display', blob.display);
  setValue($('look'), blob.transfer === 'linear' ? 'linear'
                    : blob.tone === false ? 'plain' : 'tone');
  pick('format', blob.format);
  pick('bits', blob.bits);
  pick('quality', blob.quality);
  pick('alpha', blob.alpha_mode);
  if (blob.unpremult !== undefined) $('unpremult').checked = !!blob.unpremult;
  if (blob.suffix !== undefined) $('suffix').checked = !!blob.suffix;
  syncFormat();
  schedulePreview();
}

function fillPresets(names, keep) {
  const sel = $('preset');
  const opts = [{ value: '', label: 'Presets…' }]
    .concat(names.map((n) => ({ value: n, label: n })));
  fillSelect(sel, opts, keep && names.includes(keep) ? keep : '');
  $('preset-del').disabled = !sel.value;
}

async function wirePresets() {
  const got = await window.pywebview.api.presets();
  let saved = got.presets || {};
  fillPresets(got.names || []);

  $('preset').onchange = async () => {
    const name = $('preset').value;
    $('preset-del').disabled = !name;
    if (!name) return;
    await applyPreset(saved[name]);
    log(`Loaded preset ${name}`, 'dim');
  };

  const nameBox = $('preset-name');
  const naming = (on) => {
    nameBox.hidden = !on;
    $('preset').hidden = on;
    $('preset-save').textContent = on ? 'Cancel' : 'Save';
    if (on) {
      nameBox.value = $('preset').value || '';
      nameBox.focus();
      nameBox.select();
    }
  };

  const commit = async () => {
    const name = nameBox.value.trim();
    if (!name) {
      naming(false);
      return;
    }
    const r = await window.pywebview.api.save_preset(name, settings());
    if (!r.ok) {
      log(r.error || 'Could not save the preset.', 'err');
      return;
    }
    const fresh = await window.pywebview.api.presets();
    saved = fresh.presets || {};
    naming(false);
    fillPresets(fresh.names || [], name);
    $('preset-del').disabled = false;
  };

  $('preset-save').onclick = () => naming(nameBox.hidden);
  nameBox.onkeydown = (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      commit();
    } else if (e.key === 'Escape') {
      // stop here, or the global handler reads it as cancel-the-convert
      e.preventDefault();
      e.stopPropagation();
      naming(false);
    }
  };

  $('preset-del').onclick = async () => {
    const name = $('preset').value;
    if (!name) return;
    const r = await window.pywebview.api.delete_preset(name);
    const fresh = await window.pywebview.api.presets();
    saved = fresh.presets || {};
    fillPresets(fresh.names || []);
    log(`Deleted preset ${name}`, 'dim');
    return r;
  };
}

window.addEventListener('pywebviewready', async () => {
  wire();
  wireDrag();
  wireCrypto();
  wirePicking();
  wireViewer();
  wireResizer();
  applyDefaults();
  syncFormat();
  // Swap the native <select>s for the animated ones. The originals stay in the
  // DOM and keep driving .value / onchange, so everything above is unaffected.
  upgradeSelects();
  const init = await window.pywebview.api.init();
  setTheme(init.theme || 'dark');
  setPreviewWidth(init.preview_width || PREVIEW_SIZES.m,
                  { persist: false, rerender: false });
  $('version').textContent = 'v' + init.version;
  await reloadConfigList(init.config);

  await wirePresets();

  const assoc = await window.pywebview.api.association();
  if (assoc.supported) {
    $('assoc-wrap').hidden = false;
    $('assoc').checked = !!assoc.associated;
  }
  const ctx = await window.pywebview.api.context_menu();
  if (ctx.supported) {
    $('ctx-wrap').hidden = false;
    $('ctx').checked = !!ctx.enabled;
  }
  // The gear stays whatever the platform, because the sheet also holds the
  // shortcuts. Only the integration half drops out when it is unsupported,
  // which leaves the card single-column via the same grid.
  $('integration-section').hidden = !(assoc.supported || ctx.supported);
  $('btn-prefs').hidden = false;
  state.ready = true;
  await refreshLayers();
  await refreshCrypto();
  renderFiles();
  log('Ready. ' + init.hint, 'dim');
});
