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

function applyTransform() {
  const img = $('vimg');
  img.style.transform =
    `translate(${V.x}px, ${V.y}px) scale(${V.zoom})`;
  // Below 1:1 the browser's smooth scaling is right; above it, show the pixels.
  img.classList.toggle('smooth', V.zoom * (V.imgW / (V.fullW || V.imgW)) < 1);
  $('vzoom').textContent = Math.round(V.zoom * (V.imgW / (V.fullW || V.imgW)) * 100) + '%';
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
    const r = await window.pywebview.api.render(
      V.exposure, V.gamma, V.channel, V.layer);
    if (token !== V.token) return;
    if (r.error) {
      $('vmeta').textContent = r.error;
      return;
    }
    const img = $('vimg');
    const first = !V.imgW;
    img.src = r.uri;
    V.imgW = r.width;
    V.imgH = r.height;
    V.fullW = r.full_width;
    V.fullH = r.full_height;
    $('vmeta').textContent =
      `${r.full_width} × ${r.full_height}   ${r.layer === '' ? 'R,G,B' : r.layer}`;
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
  stage.addEventListener('pointerdown', (e) => {
    panning = true;
    sx = e.clientX - V.x;
    sy = e.clientY - V.y;
    stage.classList.add('is-panning');
    stage.setPointerCapture(e.pointerId);
  });
  stage.addEventListener('pointerup', (e) => {
    panning = false;
    stage.classList.remove('is-panning');
    try {
      stage.releasePointerCapture(e.pointerId);
    } catch (_) {
      /* capture may already be gone */
    }
  });
  stage.addEventListener('pointermove', async (e) => {
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

  $('vfit').onclick = () => fit();
  $('v100').onclick = () => actualPixels();
  $('vconvert').onclick = () => openMenu();

  // exposure / gamma
  const exp = $('vexp');
  const gam = $('vgam');
  exp.oninput = () => {
    V.exposure = parseFloat(exp.value);
    $('vexp-val').textContent = (V.exposure > 0 ? '+' : '') + V.exposure.toFixed(1);
    scheduleRender();
  };
  gam.oninput = () => {
    V.gamma = parseFloat(gam.value);
    $('vgam-val').textContent = V.gamma.toFixed(2);
    scheduleRender();
  };
  $('vreset').onclick = () => {
    exp.value = '0';
    gam.value = '1';
    exp.oninput();
    gam.oninput();
  };

  for (const b of $('vchannels').querySelectorAll('button')) {
    b.onclick = () => {
      V.channel = b.dataset.ch;
      for (const o of $('vchannels').querySelectorAll('button')) {
        o.classList.toggle('is-active', o === b);
      }
      scheduleRender();
    };
  }

  $('vlayer').onchange = () => {
    V.layer = $('vlayer').value;
    V.imgW = 0; // dimensions may change with the layer
    scheduleRender(true);
  };

  $('vtheme').onclick = async () => {
    const next =
      document.documentElement.getAttribute('data-theme') === 'light'
        ? 'dark'
        : 'light';
    document.documentElement.setAttribute('data-theme', next);
    window.pywebview.api.set_theme(next);
  };

  // Keys an image viewer is expected to have.
  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    const k = e.key.toLowerCase();
    if (k === 'f') fit();
    else if (k === '1') actualPixels();
    else if (k === '+' || k === '=') zoomAt(innerWidth / 2, innerHeight / 2, 1.25);
    else if (k === '-') zoomAt(innerWidth / 2, innerHeight / 2, 1 / 1.25);
    else if (k === 'escape') {
      if (menuEl) closeMenu();
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
function probeAt(e) {
  const img = $('vimg');
  if (!img.src) return;
  const r = img.getBoundingClientRect();
  const u = (e.clientX - r.left) / r.width;
  const v = (e.clientY - r.top) / r.height;
  if (u < 0 || u > 1 || v < 0 || v > 1) {
    $('vprobe').textContent = '';
    return;
  }
  clearTimeout(probeTimer);
  probeTimer = setTimeout(async () => {
    const p = await window.pywebview.api.probe(u, v);
    if (!p || p.r === undefined) return;
    const a = p.a === null || p.a === undefined ? '—' : p.a.toFixed(3);
    $('vprobe').textContent =
      `${p.x},${p.y}   ${p.r.toFixed(4)} ${p.g.toFixed(4)} ${p.b.toFixed(4)}   a ${a}`;
  }, 35);
}

window.addEventListener('pywebviewready', async () => {
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
});
