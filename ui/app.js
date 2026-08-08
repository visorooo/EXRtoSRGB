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

  // 32-bit is float, and only TIFF has float here
  const bit32 = $('bits').querySelector('option[value="32"]');
  if (bit32) bit32.disabled = fmt !== 'tiff';
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
    const r = await window.pywebview.api.preview(state.selected, settings());
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
window.onFilesChanged = async (entries) => {
  state.entries = entries;
  if (state.selected >= entries.length) state.selected = 0;
  renderFiles();
  await refreshLayers();
  await refreshCrypto();
  schedulePreview();
};

window.onDragState = (on) => {
  $('files-panel').classList.toggle('dragging', !!on);
};

/* ---------------------------------------------------------------------------
 * Cryptomatte
 *
 * Selection is held per cryptomatte type, so switching between CryptoObject and
 * CryptoMaterial and back does not lose what was already ticked.
 * ------------------------------------------------------------------------ */

const crypto = { types: [], selected: new Map() };

function currentCrypto() {
  return crypto.types.find((t) => t.id === $('crypto-type').value) || null;
}

function selectionFor(id) {
  if (!crypto.selected.has(id)) crypto.selected.set(id, new Set());
  return crypto.selected.get(id);
}

async function refreshCrypto() {
  const panel = $('crypto-panel');
  crypto.types = [];
  crypto.selected.clear();

  if (!state.entries.length) {
    panel.hidden = true;
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
    return;
  }
  const wasHidden = panel.hidden;
  panel.hidden = false;
  // A panel that appears below the fold may as well not exist; bring it into
  // view the first time a file turns out to have cryptomattes.
  if (wasHidden) {
    requestAnimationFrame(() =>
      panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
  }
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

function wireCrypto() {
  $('crypto-type').onchange = renderObjects;
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
  };
  $('crypto-none').onclick = () => {
    const type = currentCrypto();
    if (type) selectionFor(type.id).clear();
    renderObjects();
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

  // arrow keys move through the file list
  document.addEventListener('keydown', (e) => {
    if (!state.entries.length) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
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
  $('outdir').value = '';
}

window.addEventListener('pywebviewready', async () => {
  wire();
  wireDrag();
  wireCrypto();
  applyDefaults();
  syncFormat();
  // Swap the native <select>s for the animated ones. The originals stay in the
  // DOM and keep driving .value / onchange, so everything above is unaffected.
  upgradeSelects();
  const init = await window.pywebview.api.init();
  setTheme(init.theme || 'dark');
  $('version').textContent = 'v' + init.version;
  await reloadConfigList(init.config);
  state.ready = true;
  await refreshLayers();
  await refreshCrypto();
  renderFiles();
  log('Ready. ' + init.hint, 'dim');
});
