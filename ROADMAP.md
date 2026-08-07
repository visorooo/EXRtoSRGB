# Roadmap

Where this tool is and where it goes next. Last updated 2026-08-07.

---

## Shipped — v2.0

### Colour correctness (v1.1)

**Layer selection.** `B3D_cycles_beauty_demo_srgb.png` came out grey because the
tool converted the **Ambient Occlusion** pass, not the beauty. The old channel
matcher looked at the text after the last dot, so in a file whose channels are
`Ambient_Occlusion.R`, `Beauty_Denoised.R`, `Diffuse_Color.R`… the first channel
ending in `R` won — and Blender writes layers alphabetically, so AO always won.
Nothing about the output looked like an error, which is the worst kind of bug.

Channels are now grouped into layers first, then chosen deliberately. Measured
against the Nuke reference: mean 8-bit error **82.2 → 1.09**.

**Premultiplied output.** The alpha channel was always correct — it matches Nuke to
within 1/255. What was wrong was RGB, on 929 partial-alpha pixels averaging ~2× too
bright: a bright fringe on every antialiased edge. The tool un-premultiplied before
the display transform (correct) but never re-premultiplied after. Max error
**244 → 28**.

**Stability.** Redshift beauty EXRs worked before only because they are trivially
simple — three channels named `R,G,B`, no AOVs, no alpha. **The C4D file that failed
previously was almost certainly the same layer-selection bug**; if you find it, send
it over, since a real counter-example is worth more than the synthetic ones.

### Architecture (v2.0)

**`core.py` split out.** All pixel work lives in a module with no UI imports.

**pytest suite — 48 tests.** Both v1.0 bugs would have been caught by three
assertions. Writing it immediately found two more: bare `N` (how Redshift names
normals, present in `extra_demo.exr`) scored 0 rather than negative and could have
been auto-picked as a beauty; and the thumbnail's floor division overshot its size
cap (1920 // 256 = 7 → 274px).

**Eight OCIO configs + custom config picker.** CG and Studio × v1.0 / v2.1 / v2.2 /
v4.0, including the two v1.0 entries After Effects lists. **ACES 1.2 is deliberately
absent** — it predates OCIO's built-in registry and exists only as a downloadable
`config.ocio`, so compiling it in is not possible without shipping files alongside
the exe. `Custom config.ocio…` covers it and every studio config besides.

**pywebview UI.** The interface is now HTML/CSS in the OS's own webview. Python
remains the application. `theme.css` is used verbatim, transitions and easing come
from its `--ease-ui` / `--ease-out` tokens, and file drop is native rather than a
Tcl shim. Cost: **+1.9 MB** on the exe (35.7 → 37.6 MB), because WebView2 belongs to
Windows and no browser is bundled.

**Live preview.** The selected file renders through the exact same layer, transform
and alpha path as the real output — downsampled in linear *before* the display
transform, so it is a true preview and not an approximation. A test asserts the
preview and the full conversion agree.

**Sequence detection.** `shot_010_beauty.0001.exr … .0240.exr` collapses to one row
reading `shot_010_beauty.####.exr · 240 frames · 1–240`.

**Renamed** to EXR → sRGB throughout: module, window, exe, spec, docs — and the
repo itself, `visorooo/EXRtoPNG` → `visorooo/EXRtoSRGB`, on 2026-08-07. GitHub
redirects the old URL, so old clones and shared links still resolve. The v1.0.0
release asset stays `EXRtoPNG.exe`, which is correct: it *is* the old tkinter
binary.

**Light / dark toggle.** `theme.css` always carried a full light scale; the app now
uses it. The choice persists in `%LOCALAPPDATA%\EXRtoSRGB\prefs.json` rather than
localStorage — WebView2's profile is what silently restored stale form state
between launches, so it is not somewhere to keep anything that matters.

Dark remains the default, and is still the right choice when you are actually
judging a render: a light surround biases how you read exposure and saturation.

**Dropdowns matched to the VISOR invoice app.** That app gets its select from
`@radix-ui/react-select`, which needs React, so `ui/select.js` rebuilds the same
behaviour and motion in plain DOM — 140ms open from `scale(0.95)`, transform-origin
anchored to the trigger, rotating chevron, `scale(0.97)` press, pop-in check mark,
and one highlight state shared by pointer and keyboard. Full keyboard support
including type-ahead.

**Folder tidied.** `EXRtoSRGB.exe` is written to the repo root instead of `dist/`,
PyInstaller's scratch goes to `%TEMP%`, and `exrs_tests/` is gitignored.

**Drag-and-drop actually fixed.** It was still broken after the pywebview move, for
a reason that had nothing to do with the port: `dragover` never called
`preventDefault()`, so the browser did not treat the window as a drop target and
the `drop` event never fired at all. That has to happen synchronously in JS —
pywebview dispatches DOM events to a worker thread, far too late to prevent a
default — so `wireDrag()` in `ui/app.js` now owns dragenter/dragover/dragleave and
Python keeps only the drop, where the real paths are. See CLAUDE.md for the three
conditions that must all hold, and why a real drop cannot be simulated in a test.

---

## Shipped — v2.1 (V3 in progress)

**TIFF output, and scene-linear.** TIFF writes at 8, 16 and 32-bit float. More
interesting is the third entry now in the Look dropdown: **scene-linear**, which
applies no display transform at all and hands back the render's original values.

The two are one feature because 32-bit only earns its place if it carries linear
data — nobody needs 32 bits to hold 0–1. So picking scene-linear pulls the
container and depth along with it, greys out the colour controls that no longer
act on anything, and switches the suffix to `_linear`.

Naming and tagging turned out to matter as much as the pixels. TIFF can only
express sRGB natively, and OIIO *silently drops* an unrecognised "Linear", so the
colourspace also goes in `ImageDescription`, which survives. A linear file named
`_srgb` or tagged sRGB gets a transfer function applied twice downstream — the
same class of bug as the two v1.0 ones, caught this time before shipping.

The guarantee is bit-exactness, and it is asserted rather than asserted-at:
converting to scene-linear TIFF and reading back gives pixels identical to the
source layer, values above 1.0 included.

---

## V3

### 1. EXR viewer *(the interesting one)*

Double-click an `.exr` and see it, correctly. tev is the closest existing thing but
has no OCIO, so it cannot show an ACES render the way the renderer meant it —
which is the entire problem this repo already solves.

Most of it already exists. `core.read_layer` does fast partial-channel reads,
`group_layers` handles AOVs, `apply_transform` is the colour pipeline, and the
pywebview shell is built. A viewer is roughly:

- **File association** for `.exr`, launching with a path argument. Needs a per-user
  registry write, so it belongs behind an explicit "Set as default EXR viewer"
  button rather than an installer side effect.
- **Zoom / pan** on a canvas, 1:1 and fit.
- **Exposure and gamma** above the ACES transform — the two controls anyone
  reviewing a render reaches for first.
- **Layer and channel switching**, including isolating R / G / B / A. The layer
  work is done; channel isolation is a few lines.
- **Pixel probe** showing linear scene values *and* display values under the
  cursor. This is the thing tev is genuinely good at and the reason people tolerate
  its UI.
- **Sequence playback** using the grouping that already exists.

The honest risk: a viewer is a *latency* problem where the converter is a
throughput one. Every interaction re-runs the transform, and a 4K EXR through OCIO
on CPU is not instantly interactive. Two ways out — cache the linear layer in
memory and re-run only the display transform on each change (easy, gets most of the
way), or move the transform to the GPU via OCIO's GPU path and a WebGL canvas
(fast, considerably more work). Start with the first.

Worth splitting into its own repo if it grows past a single window; the two tools
share `core.py` and little else.

### 2. Cryptomatte picking and matte export *(large — depends on the viewer)*

**Feasible, and verified.** A synthetic spec-compliant cryptomatte was written and
read back: the metadata parsed, picking a pixel resolved to the right object name,
and coverage extraction handled a mixed edge pixel correctly. The proof that it is
right is that every matte in the image summed to exactly 1.0 per pixel.

Cryptomatte stores the manifest in EXR metadata — `cryptomatte/<id>/name`, `/hash`,
`/conversion`, `/manifest` — where the manifest is JSON mapping object names to
MurmurHash3_32 values. The channels come in pairs: `CryptoObject00.R` is an ID and
`.G` its coverage, `.B`/`.A` the next rank, continuing into `CryptoObject01`. To
extract a matte you sum coverage across every rank where the ID matches.
`MurmurHash3_32` and the spec's `uint32_to_float32` conversion (with its exponent
clamp) both have to be implemented exactly, or IDs will not match.

Three things decide whether this is good or merely working:

- **Mattes must not go through the display transform.** Coverage is data, not
  colour. Running it through an ACES view would be as wrong as converting a normal
  pass. This bypasses `apply_transform` entirely — the same lesson as
  `_DATA_LAYER_TOKENS`.
- **"Flat white with alpha" needs defining.** RGB 1.0 with coverage in alpha is the
  obvious reading, but that file is *unassociated*, which contradicts the rule the
  rest of the tool follows. Associated would put coverage in RGB too, making the
  matte readable without an alpha-aware viewer. Probably offer both, defaulting to
  associated for consistency with everything else here.
- **Picking needs somewhere to click**, which is the viewer in item 1. That is the
  real dependency, and the reason this is item 2 rather than item 1.

Real files will not all be spec-compliant. The Redshift `extra_demo.exr` used
during the v2.0 work carried a `Cryptomatte_` layer with only three channels and no
rank numbering — not usable as a cryptomatte at all. Detect and say so rather than
producing silent garbage. Also expect several types per file (`CryptoObject`,
`CryptoMaterial`, `CryptoAsset`), and manifests that live in a sidecar file via
`manifest_file` instead of inline.

### 3. Parallel conversion *(medium)*

One worker thread today. OIIO and OCIO both release the GIL, so a `ThreadPool` over
frames should scale close to linearly — which matters most for the sequences now
that a 240-frame render is one click.

### 4. Smaller things

- **Per-file layer override.** The dropdown applies one choice to the batch, falling
  back to auto-detect per file. Mixed batches would benefit from remembering a
  choice per entry.
- **Convert all layers at once**, one file per AOV. The grouping makes it nearly
  free.
- **Multi-part EXR.** Only the first subimage is read. Rare from Blender and
  Redshift, but Nuke writes them.
- **Presets** — save a named set of settings, since studios use one combination for
  months at a time.
- **WebP / JPEG-XL** output, if a smaller display-ready format is ever wanted.
  Lower priority than TIFF (item 3), which has an actual pipeline reason to exist.
- **CLI entry point.** `core.py` is already importable; a thin `argparse` wrapper
  would make it render-farm usable.

---

## Known limitations

- Only the first subimage of a multi-part EXR is read.
- The layer dropdown is populated from the selected entry. Other files fall back to
  auto-detect, with a warning.
- Conversion is single-threaded.
- Cryptomatte layers are correctly *excluded* from beauty auto-detection, but are
  not otherwise understood — no picking, no matte export. See V3 item 2.
