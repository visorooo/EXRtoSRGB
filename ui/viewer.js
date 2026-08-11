/*
 * Viewer window front end.
 *
 * Zoom and pan are pure CSS transforms - no round trip to Python - so they stay
 * smooth regardless of image size. Only exposure, gamma, channel and layer
 * changes ask Python for new pixels, and those come off the cached
 * ViewerSession rather than re-reading the file.
 *
 * The render is done at a fixed generous resolution and scaled by the browser.
 * Zooming past that shows real pixels (image-rendering: pixelated) rather than
 * a smoothed guess, which is what an image viewer should do.
 */

const $ = (id) => document.getElementById(id);

/*
 * Bind a handler only if the element exists.
 *
 * A single missing element used to throw out of wire(), which runs before
 * init() - so one stale control blanked the entire window: no name, no layers,
 * no image, and no clue why. Version skew between a cached page and fresh
 * script is exactly how that happens, so nothing here is allowed to be fatal.
 */
function on(id, event, handler) {
  const el = $(id);
  if (el) el.addEventListener(event, handler);
  return el;
}

function fail(message) {
  const el = $('vmeta');
  if (el) el.textContent = message;
  console.error(message);
}

const V = {
  path: null,
  exposure: 0,
  gamma: 1,
  channel: 'rgb',
  layer: null,
  zoom: 1,
  x: 0,
  y: 0,
  imgW: 0,
  imgH: 0,
  fullW: 0,
  fullH: 0,
  token: 0,
};

/* ---------------------------------------------------------------------------
 * Transform
 * ------------------------------------------------------------------------ */

/* Screen pixels per source pixel. 1.0 is true 1:1 against the original. */
function sourceScale() {
  return V.zoom * (V.imgW / (V.fullW || V.imgW));
}

function applyTransform() {
  const img = $('vimg');
  const t = `translate(${V.x}px, ${V.y}px) scale(${V.zoom})`;
  img.style.transform = t;
  // Below 1:1 the browser's smooth scaling is right; above it, show the pixels.
  img.classList.toggle('smooth', sourceScale() < 1);
  $('vzoom').textContent = Math.round(sourceScale() * 100) + '%';
  // B rides the same transform, so it cannot drift out of register.
  const b = $('vimgb');
  if (b) {
    b.style.transform = t;
    b.classList.toggle('smooth', sourceScale() < 1);
  }
  applyWipe();
  // Any movement invalidates the overlay before the new one arrives; leaving a
  // stale crop on screen would be worse than the upscale it replaces.
  hideCrop();
  scheduleCrop();
}

/* ---------------------------------------------------------------------------
 * A/B comparison
 *
 * B is rendered with A's settings and laid on top under the same transform, so
 * wiping is a clip and flipping is a class - neither costs a render. Only Diff
 * asks Python for anything, because |A - B| is pixel work.
 *
 * The two images must be the same size. Comparing different sizes would mean
 * resampling one of them, and then the difference is partly the resampler.
 * ------------------------------------------------------------------------ */

const AB = { has: false, mode: 'a', wipe: 50, name: null };

function applyWipe() {
  const line = $('vwipe-line');
  const b = $('vimgb');
  if (!line || !b) return;
  if (!AB.has || AB.mode !== 'wipe') {
    line.hidden = true;
    b.style.clipPath = '';
    return;
  }
  const stage = $('vstage').getBoundingClientRect();
  const x = (AB.wipe / 100) * stage.width;
  /*
   * The line is a child of the stage and untransformed, so it lives in screen
   * pixels. The image is transformed, and clip-path is applied in the element's
   * OWN coordinate space *before* that transform - so the same number means two
   * different places. Convert, or the seam only lines up with the line at zoom
   * 1 with no pan, and slides away the moment you touch either.
   */
  const local = (x - V.x) / (V.zoom || 1);
  b.style.clipPath = `inset(0 0 0 ${local}px)`;
  line.style.left = x + 'px';
  line.hidden = false;
}

/*
 * Drag the seam itself.
 *
 * A wipe you can only move from a slider in the footer is a wipe nobody uses -
 * the gesture is grabbing the line. The handle stops the event reaching the
 * stage, so dragging the seam does not also pan the image; dragging anywhere
 * else still pans as before.
 */
function wireWipeDrag() {
  const line = $('vwipe-line');
  if (!line) return;
  let dragging = false;

  const setFrom = (clientX) => {
    const stage = $('vstage').getBoundingClientRect();
    const pct = ((clientX - stage.left) / stage.width) * 100;
    AB.wipe = Math.max(0, Math.min(100, pct));
    $('vwipe').value = String(AB.wipe);
    applyWipe();
  };

  line.addEventListener('pointerdown', (e) => {
    dragging = true;
    line.classList.add('is-dragging');
    try {
      line.setPointerCapture(e.pointerId);
    } catch (_) {
      /* capture is an optimisation, not a requirement */
    }
    // Keep it away from the stage, or the pan starts underneath the drag.
    e.stopPropagation();
    e.preventDefault();
  });

  line.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    setFrom(e.clientX);
    e.stopPropagation();
  });

  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    line.classList.remove('is-dragging');
    try {
      line.releasePointerCapture(e.pointerId);
    } catch (_) {
      /* may already be gone */
    }
    e.stopPropagation();
  };
  line.addEventListener('pointerup', end);
  line.addEventListener('pointercancel', end);
}

async function setABMode(mode) {
  if (!AB.has && mode !== 'a') return;
  AB.mode = mode;
  for (const btn of $('vabmodes').querySelectorAll('button')) {
    btn.classList.toggle('is-active', btn.dataset.ab === mode);
  }
  const wipeOn = mode === 'wipe';
  $('vwipe').hidden = !wipeOn;
  $('vwipe-lbl').hidden = !wipeOn;

  const b = $('vimgb');
  if (mode === 'diff') {
    b.hidden = true;
    await render();          // render() routes to the difference in this mode
  } else {
    b.hidden = mode === 'a';
    // Coming back from diff, A holds the difference image and must be redrawn.
    await render();
  }
  applyTransform();
}

async function loadCompare(res) {
  if (!res || !res.ok) {
    if (res && res.error) toast(res.error, true);
    return;
  }
  AB.has = true;
  AB.name = res.name;
  $('vabmodes').hidden = false;
  $('vcompare').textContent = 'B: ' + res.name;
  $('vcompare').title = 'Click to choose a different comparison image';
  await refreshB();
  await setABMode('wipe');
  toast('Comparing against ' + res.name);
}

async function refreshB() {
  if (!AB.has) return;
  const r = await window.pywebview.api.render_b(V.exposure, V.gamma, V.channel);
  if (!r || r.error) {
    if (r && r.error) toast(r.error, true);
    return;
  }
  $('vimgb').src = r.uri;
}

/* ---------------------------------------------------------------------------
 * Full-resolution overlay
 *
 * The base render is a fixed 1600px scaled by CSS - sharp to 1:1, interpolated
 * beyond it. So a 4K plate at 200% was showing invented pixels, with
 * `image-rendering: pixelated` making them look like real ones, which is the
 * worst of both. Past 1:1 the visible region is re-rendered at source
 * resolution and laid exactly over the top.
 *
 * The request is bounded by the stage rather than the image: at scale >= 1 the
 * visible region is at most the stage's own size in source pixels, so a 16K
 * plate costs no more than a 2K one.
 * ------------------------------------------------------------------------ */

let cropTimer = null;
let cropToken = 0;

function hideCrop() {
  const c = $('vcrop');
  if (c) c.hidden = true;
}

function scheduleCrop() {
  clearTimeout(cropTimer);
  cropTimer = setTimeout(renderCrop, 140);
}

async function renderCrop() {
  const c = $('vcrop');
  const img = $('vimg');
  if (!c || !img || !img.src || !V.fullW) return;
  const scale = sourceScale();
  /*
   * The test is whether the *render* is being magnified, not whether we are
   * past 1:1 against the source. Those are different numbers: a 2400px image
   * renders at 1200px, so at "100%" the base is already stretched 2x and shows
   * interpolated pixels. Keying off 1:1 would skip the overlay precisely where
   * it is most needed - on the large plates it exists for.
   */
  if (V.zoom <= 1.01 || V.fullW <= V.imgW) return;
  // The overlay only knows about A, so it would sit on top of B or a
  // difference and quietly show the wrong image.
  if (AB.has && AB.mode !== 'a') return;

  const stage = $('vstage').getBoundingClientRect();
  // Visible rectangle, in source pixels.
  const sx = Math.floor(Math.max(0, -V.x / scale));
  const sy = Math.floor(Math.max(0, -V.y / scale));
  const sw = Math.ceil(Math.min(V.fullW - sx, stage.width / scale) + 2);
  const sh = Math.ceil(Math.min(V.fullH - sy, stage.height / scale) + 2);
  if (sw < 2 || sh < 2) return;

  const token = ++cropToken;
  const r = await window.pywebview.api.render_crop(
    sx, sy, sw, sh, V.exposure, V.gamma, V.channel);
  // A newer view won, or the user moved while this was in flight.
  if (token !== cropToken || !r || r.error) return;
  if (Math.abs(sourceScale() - scale) > 0.001) return;

  c.src = r.uri;
  c.style.width = r.width * scale + 'px';
  c.style.height = r.height * scale + 'px';
  c.style.transform =
    `translate(${V.x + r.x * scale}px, ${V.y + r.y * scale}px)`;
  c.hidden = false;
}

function fit() {
  const stage = $('vstage').getBoundingClientRect();
  if (!V.imgW || !V.imgH) return;
  const pad = 24;
  const z = Math.min((stage.width - pad) / V.imgW, (stage.height - pad) / V.imgH);
  V.zoom = Math.min(z, 8);
  V.x = (stage.width - V.imgW * V.zoom) / 2;
  V.y = (stage.height - V.imgH * V.zoom) / 2;
  applyTransform();
}

function actualPixels() {
  // 1:1 against the *source* resolution, not the render resolution.
  const stage = $('vstage').getBoundingClientRect();
  const scale = (V.fullW || V.imgW) / V.imgW;
  const cx = stage.width / 2;
  const cy = stage.height / 2;
  const before = V.zoom;
  V.zoom = scale;
  // keep the centre of the view fixed
  V.x = cx - ((cx - V.x) / before) * V.zoom;
  V.y = cy - ((cy - V.y) / before) * V.zoom;
  applyTransform();
}

function zoomAt(clientX, clientY, factor) {
  const stage = $('vstage').getBoundingClientRect();
  const px = clientX - stage.left;
  const py = clientY - stage.top;
  const next = Math.min(64, Math.max(0.02, V.zoom * factor));
  // Anchor the point under the cursor so zooming feels attached to it.
  V.x = px - ((px - V.x) / V.zoom) * next;
  V.y = py - ((py - V.y) / V.zoom) * next;
  V.zoom = next;
  applyTransform();
}

/* ---------------------------------------------------------------------------
 * Rendering
 * ------------------------------------------------------------------------ */

let renderTimer = null;

function scheduleRender(refit = false) {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(() => render(refit), 90);
}

async function render(refit = false) {
  if (!V.path) return;
  const token = ++V.token;
  $('vloading').classList.add('on');
  try {
    const diff = AB.has && AB.mode === 'diff';
    const r = diff
      ? await window.pywebview.api.render_diff(V.exposure, V.gamma)
      : await window.pywebview.api.render(
          V.exposure, V.gamma, V.channel, V.layer);
    if (token !== V.token) return;
    if (r.error) {
      $('vmeta').textContent = r.error;
      return;
    }
    // B follows A's exposure, gamma and channel, or the comparison is between
    // two different treatments rather than two images.
    if (AB.has && !diff) await refreshB();
    const img = $('vimg');
    const first = !V.imgW;
    img.src = r.uri;
    V.imgW = r.width;
    V.imgH = r.height;
    // The difference render reports no source size - it is a derived image, not
    // a file - so keep the ones A already established.
    if (r.full_width) {
      V.fullW = r.full_width;
      V.fullH = r.full_height;
    }
    $('vmeta').textContent = diff
      ? `${V.fullW} × ${V.fullH}   |A − B|  ·  raise exposure to read small differences`
      : `${V.fullW} × ${V.fullH}   ${r.layer === '' ? 'R,G,B' : r.layer}`;
    if (first || refit) fit();
    else applyTransform();
  } finally {
    if (token === V.token) $('vloading').classList.remove('on');
  }
}

/* ---------------------------------------------------------------------------
 * Wiring
 * ------------------------------------------------------------------------ */

function wire() {
  const stage = $('vstage');

  // pan
  let panning = false;
  let sx = 0;
  let sy = 0;
  // How far the pointer travelled while down, so a pick can be told from the
  // end of a pan - both finish with a pointerup in the same place otherwise.
  let travel = 0;
  let px = 0;
  let py = 0;
  stage.addEventListener('pointerdown', (e) => {
    panning = true;
    sx = e.clientX - V.x;
    sy = e.clientY - V.y;
    travel = 0;
    px = e.clientX;
    py = e.clientY;
    stage.classList.add('is-panning');
    try {
      stage.setPointerCapture(e.pointerId);
    } catch (_) {
      /* capture is an optimisation - panning still works without it */
    }
  });
  stage.addEventListener('pointerup', async (e) => {
    panning = false;
    stage.classList.remove('is-panning');
    try {
      stage.releasePointerCapture(e.pointerId);
    } catch (_) {
      /* capture may already be gone */
    }
    if (travel < 4) await pickAt(e);
  });
  stage.addEventListener('pointermove', async (e) => {
    travel += Math.abs(e.clientX - px) + Math.abs(e.clientY - py);
    px = e.clientX;
    py = e.clientY;
    if (panning) {
      V.x = e.clientX - sx;
      V.y = e.clientY - sy;
      applyTransform();
      return;
    }
    probeAt(e);
  });

  // zoom
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });

  on('vfit', 'click', () => fit());
  on('v100', 'click', () => actualPixels());
  on('vconvert', 'click', () => openMenu());

  on('vswatch', 'click', async () => {
    if (!lastHex) return;
    await window.pywebview.api.copy_text(lastHex);
    toast(`Copied ${lastHex}`);
  });

  on('vcompare', 'click', async () => {
    loadCompare(await window.pywebview.api.pick_compare());
  });
  for (const btn of ($('vabmodes') || { querySelectorAll: () => [] })
       .querySelectorAll('button')) {
    btn.onclick = () => setABMode(btn.dataset.ab);
  }
  on('vwipe', 'input', () => {
    AB.wipe = parseFloat($('vwipe').value);
    applyWipe();
  });
  wireWipeDrag();

  on('vpick', 'click', () => setPicking(!picking));
  on('vkeys', 'click', () => toggleSheet());
  on('vsheet-close', 'click', () => toggleSheet(false));
  on('vsheet', 'click', (e) => {
    if (e.target === $('vsheet')) toggleSheet(false);
  });

  // exposure / gamma
  const exp = $('vexp');
  const gam = $('vgam');
  if (exp) exp.oninput = () => {
    V.exposure = parseFloat(exp.value);
    $('vexp-val').textContent = (V.exposure > 0 ? '+' : '') + V.exposure.toFixed(1);
    scheduleRender();
  };
  if (gam) gam.oninput = () => {
    V.gamma = parseFloat(gam.value);
    $('vgam-val').textContent = V.gamma.toFixed(2);
    scheduleRender();
  };
  on('vreset', 'click', () => {
    if (exp) exp.value = '0';
    if (gam) gam.value = '1';
    if (exp) exp.oninput();
    if (gam) gam.oninput();
  });

  for (const b of ($('vchannels') || document.createDocumentFragment())
                   .querySelectorAll('button')) {
    b.onclick = () => {
      V.channel = b.dataset.ch;
      for (const o of $('vchannels').querySelectorAll('button')) {
        o.classList.toggle('is-active', o === b);
      }
      scheduleRender();
    };
  }

  on('vlayer', 'change', () => {
    V.layer = $('vlayer').value;
    V.imgW = 0; // dimensions may change with the layer
    scheduleRender(true);
  });

  on('vtheme', 'click', async () => {
    const next =
      document.documentElement.getAttribute('data-theme') === 'light'
        ? 'dark'
        : 'light';
    document.documentElement.setAttribute('data-theme', next);
    window.pywebview.api.set_theme(next);
  });

  // Keys an image viewer is expected to have.
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    const k = e.key.toLowerCase();
    if (k === 'f') fit();
    else if (k === '1') actualPixels();
    else if (k === '+' || k === '=') zoomAt(innerWidth / 2, innerHeight / 2, 1.25);
    else if (k === '-') zoomAt(innerWidth / 2, innerHeight / 2, 1 / 1.25);
    else if (k === '?' || (k === '/' && e.shiftKey)) toggleSheet();
    else if (k === 'e') setPicking(!picking);
    // \ cycles A -> B -> Wipe -> Diff. One key rather than four, because the
    // useful gesture is flipping back and forth, not jumping to a named mode.
    else if (k === '\\' && AB.has) {
      const order = ['a', 'b', 'wipe', 'diff'];
      setABMode(order[(order.indexOf(AB.mode) + 1) % order.length]);
    }
    else if (k === 'escape') {
      // Release a held colour before closing the window - Escape should undo
      // the last thing, not quit out from under a reading being copied.
      if (!$('vsheet').hidden) toggleSheet(false);
      else if (menuEl) closeMenu();
      else if (picking || locked) { setPicking(false); unlockProbe(); }
      else window.pywebview.api.close_window();
    }
    else if (['r', 'g', 'b', 'a'].includes(k)) {
      const btn = $('vchannels').querySelector(`button[data-ch="${k}"]`);
      if (btn) btn.click();
    } else if (k === 'c') {
      $('vchannels').querySelector('button[data-ch="rgb"]').click();
    } else return;
    e.preventDefault();
  });

  window.addEventListener('resize', () => {
    applyTransform();
    closeMenu();
  });
}

function toggleSheet(force) {
  const el = $('vsheet');
  if (!el) return;
  el.hidden = force === undefined ? !el.hidden : !force;
}

/* ---------------------------------------------------------------------------
 * Convert menu
 *
 * The presets come from Python (the same CONVERT_VERBS the Explorer right-click
 * menu is built from), so the two can never offer different things.
 * ------------------------------------------------------------------------ */

let menuEl = null;

function closeMenu() {
  if (!menuEl) return;
  menuEl.remove();
  menuEl = null;
  $('vconvert').setAttribute('aria-expanded', 'false');
  document.removeEventListener('mousedown', onMenuOutside, true);
  document.removeEventListener('keydown', onMenuKey, true);
}

function onMenuOutside(e) {
  if (menuEl && !menuEl.contains(e.target) && !$('vconvert').contains(e.target)) {
    closeMenu();
  }
}

function onMenuKey(e) {
  if (e.key === 'Escape') {
    e.stopPropagation();
    closeMenu();
  }
}

async function openMenu() {
  if (menuEl) return closeMenu();
  const presets = await window.pywebview.api.convert_presets();
  const btn = $('vconvert');
  const r = btn.getBoundingClientRect();

  menuEl = document.createElement('div');
  menuEl.className = 'vmenu';
  menuEl.setAttribute('role', 'menu');
  for (const p of presets) {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.textContent = p.label;
    b.onclick = () => {
      closeMenu();
      runConvert(p);
    };
    menuEl.appendChild(b);
  }
  document.body.appendChild(menuEl);
  // right-aligned to the button, flipped up if it would run off the bottom
  const mh = menuEl.offsetHeight;
  menuEl.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
  if (r.bottom + mh + 8 > window.innerHeight) {
    menuEl.style.top = Math.max(8, r.top - mh - 4) + 'px';
    menuEl.style.transformOrigin = 'bottom right';
  } else {
    menuEl.style.top = r.bottom + 4 + 'px';
  }

  btn.setAttribute('aria-expanded', 'true');
  document.addEventListener('mousedown', onMenuOutside, true);
  document.addEventListener('keydown', onMenuKey, true);
}

let toastTimer = null;
function toast(text, { error = false, onClick = null, hint = '' } = {}) {
  const el = $('vtoast');
  el.className = 'vtoast' + (error ? ' err' : '');
  el.textContent = text;
  if (hint) {
    const s = document.createElement('span');
    s.className = 'hint';
    s.textContent = hint;
    el.appendChild(s);
  }
  el.hidden = false;
  el.onclick = onClick;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.hidden = true;
  }, error ? 6000 : 4000);
}

async function runConvert(preset) {
  $('vconvert').disabled = true;
  toast(`Converting to ${preset.label}…`);
  try {
    const r = await window.pywebview.api.convert(
      preset.format, preset.bits, preset.transfer);
    if (!r.ok) {
      toast(r.error || 'Conversion failed', { error: true });
      return;
    }
    toast(`Saved ${r.name}`, {
      hint: 'click to show in Explorer',
      onClick: () => window.pywebview.api.reveal(r.path),
    });
  } finally {
    $('vconvert').disabled = false;
  }
}

let probeTimer = null;
let lastHex = null;
// picking: armed and waiting for a click. locked: a reading is being held.
let picking = false;
let locked = false;

/*
 * Every value copies on click.
 *
 * The window runs with text_select=False, so dragging across a number is
 * unreliable - and these are the values anyone actually wants to paste.
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
    toast(`Copied ${text}`);
  };
  return b;
}

function paintProbe(p) {
  const el = $('vprobe');
  const f4 = (v) => v.toFixed(4);
  const lin = [f4(p.r), f4(p.g), f4(p.b)];
  el.textContent = '';
  const add = (node) => el.appendChild(node);
  const label = (t) => {
    const s = document.createElement('span');
    s.className = 'probe-label vlabel';
    s.textContent = t;
    return s;
  };
  add(copyable(`${p.x},${p.y}`, 'Pixel coordinate'));
  add(label('lin'));
  add(copyable(lin[0], 'Red, linear'));
  add(copyable(lin[1], 'Green, linear'));
  add(copyable(lin[2], 'Blue, linear'));
  add(copyable(lin.join(' '), 'All three, linear'));
  if (p.dr !== undefined) {
    add(label('disp'));
    add(copyable([p.dr, p.dg, p.db].join(' '), 'RGB, 8-bit display'));
  }
  add(label('a'));
  add(copyable(p.a === null || p.a === undefined ? '—' : p.a.toFixed(4), 'Alpha'));
  if (p.hex) add(copyable(p.hex, 'Display colour'));

  if (!p.hex) return;
  const sw = $('vswatch');
  sw.hidden = false;
  // alpha kept on the chip so a soft edge does not read as solid colour
  const al = p.a === null || p.a === undefined ? 1 : Math.min(1, Math.max(0, p.a));
  sw.style.setProperty('--swatch', `rgb(${p.dr} ${p.dg} ${p.db} / ${al})`);
  lastHex = p.hex;
}

function uvAt(e) {
  const img = $('vimg');
  if (!img.src) return null;
  const r = img.getBoundingClientRect();
  const u = (e.clientX - r.left) / r.width;
  const v = (e.clientY - r.top) / r.height;
  return u < 0 || u > 1 || v < 0 || v > 1 ? null : [u, v];
}

function probeAt(e) {
  if (locked) return;
  const uv = uvAt(e);
  if (!uv) {
    $('vprobe').textContent = '';
    return;
  }
  clearTimeout(probeTimer);
  probeTimer = setTimeout(async () => {
    const p = await window.pywebview.api.probe(uv[0], uv[1], V.exposure, V.gamma);
    if (p && p.r !== undefined && !locked) paintProbe(p);
  }, 35);
}

/*
 * Arm or disarm the eyedropper.
 *
 * Disarming must not release a held reading - taking a sample disarms, and if
 * that unlocked too, the reading would resume following the cursor the instant
 * it was taken. Arming again does unlock, being a request for a new one.
 */
function setPicking(on) {
  picking = on;
  const b = $('vpick');
  if (b) b.setAttribute('aria-pressed', String(on));
  $('vstage').classList.toggle('picking', on);
  if (on) unlockProbe();
}

/* Let the readout follow the cursor again. */
function unlockProbe() {
  locked = false;
  const sw = $('vswatch');
  if (sw) sw.classList.remove('locked');
}

/*
 * Take and hold one reading.
 *
 * Returns true if it consumed the click, so the stage can tell a sample from
 * the start of a pan.
 */
async function pickAt(e) {
  if (!picking) return false;
  const uv = uvAt(e);
  if (!uv) return false;
  const p = await window.pywebview.api.probe(uv[0], uv[1], V.exposure, V.gamma);
  if (!p || p.r === undefined) return false;
  locked = false;                  // paintProbe is refused while locked
  paintProbe(p);
  locked = true;
  $('vswatch').classList.add('locked');
  setPicking(false);
  if (p.hex) {
    await window.pywebview.api.copy_text(p.hex);
    toast(`Copied ${p.hex}`);
  }
  return true;
}

window.addEventListener('pywebviewready', async () => {
  try {
    await startup();
  } catch (e) {
    fail('Viewer failed to start: ' + (e && e.message ? e.message : e));
  }
});

async function startup() {
  wire();
  const init = await window.pywebview.api.init();
  document.documentElement.setAttribute('data-theme', init.theme || 'dark');
  V.path = init.path;
  $('vname').textContent = init.name || '—';
  document.title = (init.name || 'EXR viewer') + ' · EXR → sRGB';

  const sel = $('vlayer');
  sel.innerHTML = '';
  for (const l of init.layers || []) {
    const o = document.createElement('option');
    o.value = l;
    o.textContent = l === '' ? '(no layer · R,G,B)' : l;
    sel.appendChild(o);
  }
  if (window.upgradeSelects) window.upgradeSelects();
  V.layer = sel.value;

  await render(true);
}
