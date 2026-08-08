# EXR → sRGB

Batch converter: ACES-linear EXR → display-ready PNG/JPEG, using OpenColorIO's
built-in ACES configs so the result matches the renderer's viewport rather than a
gamma guess. Public on `visorooo/EXRtoSRGB`, MIT. The README is the user
documentation; `ROADMAP.md` tracks what's next and why.

App, module, exe and repo are all named **EXR → sRGB** as of v2.0. The repo was
renamed from `EXRtoPNG` on 2026-08-07; GitHub redirects the old URL, so old clones
and links still resolve. Anything that needs settings, visibility or collaborator
changes still requires the **visorooo** account — `gabe-xyz` has push but not
admin.

## Shape of the code

```
EXRtoSRGB.exe     the build, written to the repo root so it is easy to find.
app.ico           VISOR mark, multi-size. wired into the spec and the window.
core.py           conversion. no UI imports. this is what the tests exercise.
exr2srgb.py       pywebview window + the Api bridge exposed to JavaScript.
ui/theme.css      VISOR warm-neutral palette, verbatim from invoice-app.
ui/app.css        application styles, built only on theme.css tokens.
ui/index.html     markup.
ui/app.js         front end. gathers settings, renders state. no pixel work.
ui/select.js      custom dropdown, matching invoice-app's Radix select.
ui/visor-mark.svg the mark on its own, for anywhere it is needed standalone.
tests/test_core.py
```

**Branding.** The mark's geometry is lifted from the vector paths in
`visor.logo-principal.pdf` (VISOR brand drive → Logo), not traced from a bitmap,
so it is exact at any size. Brand blue is **`#1f20f1`**, taken from the artwork
itself. `app.ico` puts a warm-50 mark on a blue rounded square rather than the
reverse: at 16px in a taskbar, blue-on-near-black collapses into an unreadable
blob (1.99:1 against 7.53:1 inverted).

In the UI the mark is inline SVG using `currentColor`. Two tokens, deliberately
separate: **`--visor-blue`** is the brand and never varies by theme, while
**`--brand-mark`** is what the mark is drawn in — brand blue on light, **pure
white on dark**. Brand blue on the dark surface is 1.9:1, and lifting the hue far
enough to be legible stops reading as the brand colour anyway, so the mark goes
monochrome rather than approximate.

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
  so `convert_one` and `make_thumbnail` cannot drift apart. `transfer="linear"`
  makes `apply_transform` a passthrough: no OCIO, no alpha juggling, **no clamp**.
- **`resolve_output`** — the single place that decides the real container and
  pixel format. The UI mirrors these rules in `syncFormat()` but does not own
  them, so a settings blob from anywhere still gets checked.
- **`convert_one(path, settings)`** — returns `(out_path, info)`; `info` carries
  the layer used and any warning.
- **`group_sequences`** — collapses `name.0001.exr` runs into one entry.
- **`convert_many`** — the thread pool. Results come back in submission order,
  and the callback runs in the calling thread, so callers need no lock.
- **`ViewerSession`** — the viewer's reason to exist. See below.

**`ViewerSession` and why it is not just `make_thumbnail`.** The converter is a
throughput problem; a viewer is a latency one. One preview frame costs **125 ms at
1080p and 860 ms on a 2160² 80-channel file**, and lowering the preview resolution
barely moves either — the cost is *decoding*, not the transform. So the session
decodes once and keeps it, and also caches the downsampled copy per output size,
because re-scaling the full-resolution layer was another 82 ms every render.
Result: **4.1× at 1080p (23 fps) and 5.6× on the 2160² file**.

Two things that will bite anyone editing `render()`:

- **`apply_transform` writes in place.** The cached array must never be handed to
  it directly; exposure produces a new array, and the zero-exposure path copies.
  A test asserts repeated renders do not mutate the cache.
- **Exposure goes before the transform, gamma after.** Exposure in linear is what
  makes it behave like a camera stop; gamma on display values is what a
  compositor's viewer gamma does. Swapping them changes what both controls mean.

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

### Why drag-drop breaks, and how to tell

Three separate things must hold. Any one missing looks identical from outside —
you drag a file on and nothing happens at all.

1. **`dragover` must call `preventDefault()` in JavaScript.** Without it the page
   is not a drop target and the `drop` event never fires. This cannot be done from
   Python: pywebview dispatches DOM events to a worker thread, long after the
   default would have applied. `wireDrag()` in `ui/app.js` owns this, and owns the
   hover state too, because `dragover` fires continuously and routing it over the
   bridge is a stream of pointless IPC. **This is what was broken in v2.0.**
2. **The `drop` listener must be registered through pywebview**, which increments
   `webview.dom._dnd_state['num_listeners']`. WebView2 only forwards
   `CoreWebView2File` objects when that count is above zero — no listener, no
   paths, even if the event fires.
3. **`DOMEventHandler(on_drop, prevent_default=True)`** on the drop itself, or
   WebView2 navigates away to the dropped file.

Verify with: `_dnd_state['num_listeners'] == 1`, `window.dom._elements` containing
one element whose `_event_handlers['drop']` is non-empty, and a synthetic
`dragover` reporting `defaultPrevented == true`.

**A real drop cannot be simulated.** `postMessageWithAdditionalObjects` rejects
constructed `File` objects — *"additional File object is not a file on the disk"* —
and that throw aborts the bridge call before Python is reached. To exercise the
Python side, call the registered handler directly with
`{"dataTransfer": {"files": [{"name": ..., "pywebviewFullPath": ...}]}}`. The final
mile needs a human dragging from Explorer.

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

**Preview sizing has two independent axes.** The column width is freeform
(`--preview-w`, dragged or jumped to by the S/M/L presets, persisted as
`preview_width`); the *render* resolution snaps to `RENDER_TIERS` and only changes
when a tier boundary is crossed. Keep them separate — tying the render to the
width re-renders on every pixel of a drag, and a render is tens of milliseconds.

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

Four defaults are deliberate. Two are colour-critical: **ACES 1.3 CG v2.2**,
because Blender/Octane/Redshift ACES setups are still on 1.x, and **un-premultiply
on**, because renders write associated alpha. Two are workflow choices made in
v2.0.1: **16-bit**, which keeps gradients intact for anything going back into a
comp, and **`_srgb` suffix on**, so an in-place convert never leaves a `.png`
beside its `.exr` with the same stem. All four live in `applyDefaults()` in
`ui/app.js` — not in the markup, for the WebView2 reason above.

**ACES 1.2 is not an OCIO built-in.** It predates the built-in registry and exists
only as a downloadable `config.ocio`, so it cannot be compiled in. That is what the
`Custom config.ocio…` entry is for; don't try to add ACES 1.2 to `ACES_CONFIGS`.

## Cryptomatte

`probe_cryptomattes` / `extract_matte` / `convert_mattes`. Verified against a real
Blender 5.2 render (2160², 80 channels, two crypto types); ten of that file's own
manifest hashes are known-answer tests, so our `murmur3_32` is checked against
another implementation rather than itself. One of them is non-ASCII on purpose —
object names come from the DCC and are arbitrary Unicode, which is also why
`matte_filename` sanitises and why the object list uses `textContent`.

Things real files do that synthetic ones do not:

- **A single rank's coverage can exceed 1.0** — the Blender file reached 2.633,
  because the pixel filter accumulates. The clamp in `extract_matte` is required,
  not defensive.
- **Mattes do not sum to 1.0.** Only 27% of pixels did in that render: rank count
  is capped by the render's Levels setting, so overlap beyond that is simply not
  in the file. The synthetic fixture does sum to 1.0 and asserts it; do not extend
  that assertion to real files.
- **Channel layers carry the view layer prefix** — `ViewLayer.CryptoObject00.r`,
  lowercase components, while the sibling AOVs use uppercase. `label` strips the
  prefix for display.

**`[hidden]` loses to any class that sets `display`.** The attribute only gets
`display: none` from the UA stylesheet, so `.settings` (grid) and `.crypto-body`
(flex) kept both tab bodies laid out at once — which is what put a scrollbar in
the left column. `app.css` sets `[hidden] { display: none !important }` for this.
`el.hidden` still reports `true` in that state, so the attribute is not evidence
the element is gone; check the computed height.

**ID planes must be subsampled, never filtered.** Averaging two IDs produces a
third that matches no object in the manifest, so `_subsample` takes nearest
neighbours. A test asserts the preview invents no IDs.

**OIIO's PNG writer always associates alpha.** Writing RGB 1.0 with a coverage
alpha reads back as RGB = coverage, and no `oiio:UnassociatedAlpha` or
`png:unassociatedAlpha` attribute changes it (measured). TIFF preserves it by
default — and *inverts* if that attribute is set. So `resolve_matte_output` forces
TIFF for `matte_mode="straight"` rather than writing a file that silently is not
what was asked for.

## Scene-linear output

`transfer="linear"` is a different job from everything else here: it moves data
rather than making a picture. Three things it must never do — all asserted in
`tests/test_core.py`:

- **Clamp.** Linear values run past 1.0 and that is the point. Only the display
  path clips.
- **Touch alpha.** Un-premultiply is skipped entirely, so the toggle has no effect
  and the output is bit-identical to the source layer.
- **Claim to be sRGB.** The file is tagged `colorspace=Linear` and named `_linear`.
  TIFF can only express sRGB natively — via the EXIF ColorSpace tag — and OIIO
  silently *drops* an unrecognised "Linear", so it also goes in `ImageDescription`,
  which always survives. A linear file labelled sRGB gets a transfer function
  applied twice downstream, which is the same class of bug as the two below.

The preview stays display-referred in this mode (raw linear renders as a near-black
smear) and returns `preview_only=True` so the UI can say so.

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
shows the app; PyInstaller's scratch goes to `%TEMP%`.

`dist/EXRtoPNG.exe` (35.7 MB) is the superseded v1.0.0 asset, kept locally for
re-upload — safe to delete, since it is already published on Releases. Its name
is deliberately *not* updated: it is the old tkinter binary and `EXRtoPNG.exe` is
what that release actually shipped. Never commit either binary.
