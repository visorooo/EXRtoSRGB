# EXR → sRGB

Batch converter: ACES-linear EXR → display-ready PNG/JPEG, using OpenColorIO's
built-in ACES configs so the result matches the renderer's viewport rather than a
gamma guess. Public on `visorooo/EXRtoPNG`, MIT. The README is the user
documentation; `ROADMAP.md` tracks what's next and why.

The app, module and exe are all named **EXR → sRGB** as of v2.0. **The repo is
still `EXRtoPNG`** — renaming needs the visorooo account, since `gabe-xyz` is a
collaborator without admin rights.

## Shape of the code

```
EXRtoSRGB.exe     the build, written to the repo root so it is easy to find.
core.py           conversion. no UI imports. this is what the tests exercise.
exr2srgb.py       pywebview window + the Api bridge exposed to JavaScript.
ui/theme.css      VISOR warm-neutral palette, verbatim from invoice-app.
ui/app.css        application styles, built only on theme.css tokens.
ui/index.html     markup.
ui/app.js         front end. gathers settings, renders state. no pixel work.
ui/select.js      custom dropdown, matching invoice-app's Radix select.
tests/test_core.py
```

The split is the point: anything touching pixels goes in `core.py` so it can be
tested without opening a window.

**`core.py`**
- **Colour pipeline** — `get_config` / `get_processor` / `view_for` /
  `list_displays` / `list_input_spaces`. Where any colour-correctness question is
  answered. `get_config` takes either a built-in registry name or a path to a
  `config.ocio`.
- **Layer resolution** — `split_channel` / `group_layers` / `score_layer` /
  `pick_layer` / `probe_layers`. See the invariants below.
- **I/O** — `read_layer` (reads only the channels the chosen layer needs),
  `write_image`.
- **`apply_transform` / `compose`** — the transform and the alpha modes, split out
  so `convert_one` and `make_thumbnail` cannot drift apart.
- **`convert_one(path, settings)`** — returns `(out_path, info)`; `info` carries
  the layer used and any warning.
- **`group_sequences`** — collapses `name.0001.exr` runs into one entry.

**`exr2srgb.py`** — `class Api` is the JS bridge. Two non-obvious constraints:

- **Every public attribute of `Api` gets introspected by pywebview.** `self.window`
  as a public name sends it recursing into the .NET window object forever, spewing
  `Error while processing window.native...` and breaking the bridge so silently
  that the UI just renders with empty dropdowns. It is `self._window` for that
  reason. Keep non-method state underscore-prefixed.
- **Drag-drop binds with `element.on('drop', ...)`, not `element.events.drop +=`.**
  The events container is generated from properties the DOM node advertises, and a
  plain `<div>` does not advertise a drop event, so the `+=` form raises
  `AttributeError`. The handler runs in Python because that is where pywebview
  injects `pywebviewFullPath`; the browser alone never sees a real path.

**`ui/app.js`** — `applyDefaults()` sets every control from code at startup.
WebView2 restores form state from its profile across launches, which silently
flipped un-premultiply off between runs. Do not rely on `checked` in the markup.

**`ui/select.js`** — the invoice app gets its animated dropdown from
`@radix-ui/react-select`, which needs React. This is the same behaviour and motion
in plain DOM. The real `<select>` stays in the document as the source of truth and
is visually hidden, so `$('format').value` and `onchange` keep working and nothing
else has to know the component exists.

Two things it cannot observe on its own: **setting `.value` from code fires no
change event and mutates no attribute**, so every programmatic write goes through
`setValue()` in app.js; and repopulating options is caught by a `MutationObserver`,
which is why `fillSelect` works without special handling.

**Scripts are plain, not ES modules.** The UI loads from a `file://` URL, whose
opaque origin makes `import` fail CORS. `select.js` must load before `app.js`.

## Running and building

```bash
pip install OpenImageIO OpenColorIO pywebview
python exr2srgb.py
pytest -q
```

`EXR2SRGB_DEBUG=1` opens devtools.

`build_exe.bat` runs `EXRtoSRGB.spec`. Three things in that spec are load-bearing:
`collect_all` for OpenImageIO and PyOpenColorIO (the DLLs, without which the exe
builds fine and dies at launch), `datas=[('ui','ui')]` (the interface itself,
resolved through `sys._MEIPASS`), and pywebview's own hooks under
`webview/__pyinstaller` which PyInstaller discovers automatically.

The ACES configs are compiled into OpenColorIO, so nothing ships alongside the exe.
The web UI costs about 2 MB over the old tkinter build because WebView2 belongs to
the OS — there is no bundled browser.

## Verifying a colour change

`pytest -q` asserts all of this; run it before and after anything touching the
transform chain. The ladder is also in the README as a user-facing table:

| ACEScg in | ACES 1.3 → sRGB 8-bit | ACES 2.0 → sRGB 8-bit |
|---|---|---|
| 0.18 | 91 | 89 |
| 1.0 | 207 | 180 |
| 4.0 | 244 | 229 |

If 1.0 lands on 255, the ACES curve is not being applied — most likely the view
resolved to a plain transfer function instead of the tone-mapped one.

`exrs_tests/` holds Blender Cycles and C4D Redshift renders alongside Nuke
conversions of the same frames; mean 8-bit error is 1.09 / 0.13 / 0.05 and the
tests assert those bounds.

Two defaults are deliberate and easy to break by accident: **ACES 1.3 CG v2.2**,
because Blender/Octane/Redshift ACES setups are still on 1.x, and **un-premultiply
on**, because renders write associated alpha.

**ACES 1.2 is not an OCIO built-in.** It predates the built-in registry and exists
only as a downloadable `config.ocio`, so it cannot be compiled in. That is what the
`Custom config.ocio…` entry is for; don't try to add ACES 1.2 to `ACES_CONFIGS`.

## Two invariants that have already been violated once

**Never match EXR channels on the component suffix alone.** Channels are
`<layer>.<component>`, so a Blender AOV dump contains fifteen channels ending in
`R`. The pre-1.1 code took the first one, which — because Blender writes layers
alphabetically — was reliably `Ambient_Occlusion`. The tool exported the AO pass as
the beauty for its entire v1.0 lifetime, and the output looked plausible enough
that nobody caught it. Group into layers, then choose by `score_layer`.

Note `_DATA_LAYER_EXACT` alongside the substring list: Redshift writes normals as
bare `N` and depth as `Z`, which no substring rule can catch without also
condemning every layer containing an n or a z.

**Un-premultiply and re-premultiply must stay paired.** Dividing alpha out before
the display transform is correct and must not be removed — the transfer function
needs true surface colour. But the alpha has to go back on afterwards, or the file
carries straight-alpha RGB while every compositor reads PNG edge pixels as
associated. The symptom is a bright fringe on antialiased edges, visible only on
partial-alpha pixels, which is easy to mistake for a broken alpha channel. The
`unpremulted` flag in `apply_transform` is what keeps the pair honest.

## Release

`EXRtoSRGB.exe` (~37.6 MB) ships as a GitHub Release asset and is gitignored.
`build_exe.bat` writes it to the **repo root** rather than `dist/`, so the folder
shows the app; PyInstaller's scratch goes to `%TEMP%`. `dist/EXRtoPNG.exe`
(35.7 MB) is the superseded v1.0.0 asset, kept locally for re-upload — safe to
delete, since it is already published on Releases. Never commit either binary.
