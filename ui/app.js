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
    format: $('format').value,
    quality: parseInt($('quality').value, 10),
    bits: parseInt($('bits').value, 10),
    alpha_mode: $('alpha').value,
    layer: $('layer').value,
    unpremult: $('unpremult').checked,
    suffix: $('suffix').checked ? '_srgb' : '',
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

/* JPEG carries neither 16-bit nor alpha; say so in the controls rather than
   silently ignoring the values. */
function syncFormat() {
  const jpeg = $('format').value === 'jpeg';
  $('quality').disabled = !jpeg;
  $('bits').disabled = jpeg;
  if (jpeg) setValue($('bits'), '8');
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
  schedulePreview();
};

window.onDragState = (on) => {
  $('files-panel').classList.toggle('dragging', !!on);
};

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
  for (const id of ['input-cs', 'display', 'look', 'alpha', 'layer', 'bits',
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
  setValue($('bits'), '8');
  setValue($('quality'), '95');
  setValue($('alpha'), 'keep');
  $('unpremult').checked = true;
  $('suffix').checked = false;
  $('outdir').value = '';
}

window.addEventListener('pywebviewready', async () => {
  wire();
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
  renderFiles();
  log('Ready. ' + init.hint, 'dim');
});
