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

**Parallel conversion.** Batches run across a thread pool instead of one frame
at a time. OIIO and OCIO both release the GIL for decoding and the transform, so
threads genuinely overlap: **5.07× on 16 × 1080p frames** (12.50s → 2.46s at eight
workers). Workers are capped at eight because the work is memory-bandwidth bound
well before it is core bound — each holds a full float frame.

Results are reported in submission order rather than completion order, so the log
still reads like the file list, and a test asserts threaded output is pixel-identical
to serial.

**Double-click an `.exr` and it opens.** A per-user `HKCU` file association,
behind an explicit toggle, plus `--view <path>` on the exe — which is what the
shell runs. The converter's **⧉** button opens the same window in-process for the
selected file, so both routes render `ui/viewer.html` and there is one thing to
keep working.

The viewer window has zoom and pan (scroll to zoom at the cursor, F to fit, 1 for
actual pixels), exposure, gamma, channel isolation, layer switching and the pixel
probe. Zoom and pan are CSS transforms and never reach Python, so they stay smooth
at any image size; only pixel changes cross the bridge. Percentages are reported
against the *source* resolution, so 100% means actual pixels of the original.

**Newly added files are selected automatically**, so a drop previews the thing you
just dropped.

**The same convert presets inside the viewer**, from a Convert button in its top
bar. Both menus are built from `CONVERT_VERBS`, so they cannot drift apart.

**Right-click → Convert to sRGB.** Five verbs (PNG 8/16, JPEG, TIFF 16, TIFF
32-bit scene-linear) registered under `SystemFileAssociations\.exr`, so they show
up whatever application owns the file type. `--convert` runs headless — no window
is ever created. On Windows 11 they appear under "Show more options", because the
short menu only accepts packaged apps with a COM handler.

**A proper `.exr` document icon** — an aperture with an EXR label, separate from
the application icon so a file and the app that opens it are distinguishable in
Explorer. Below 32px it drops the label and gives the aperture the whole tile,
since the text is unreadable at that size. The registry points at a copy under
`%LOCALAPPDATA%` rather than `BASE_DIR`, which in a one-file build is a temp
directory that disappears on exit and would leave a blank icon.

**Window geometry.** Windows now open centred on the primary display and sized to
the image, up to 1:1 and capped to the screen — pywebview's own centring picks a
display that is not reliably the one being used. Size and position are remembered,
saved on a debounce after each move or resize rather than only on close, since
that event never fires if the process is killed.

**Viewer, stage one.** Exposure, gamma, channel isolation (R/G/B/A/luma) and a
pixel probe, all beside the preview. The point is `ViewerSession`: it decodes a
layer once and keeps it, plus the downsampled copy per output size, so nothing
re-reads the file. Measured **4.1× at 1080p — 23 fps, genuinely interactive — and
5.6× on a 2160² 80-channel frame**.

The probe reports **linear scene values from the full-resolution layer**, not the
preview, so the number is the real pixel rather than something resampled.

Profiling what is left, at 900px on the heavy file: downsample 82 ms (now cached),
transform 40 ms, **PNG encode 108 ms**. Encoding is the next bottleneck, and
`png:compressionLevel` turns out to be ignored by OIIO — file sizes are identical
at every level. JPEG encodes ~4× faster but cannot carry alpha, so the honest fix
is stage two, the GPU path.

**Draggable preview, with S/M/L as quick jumps.** Layout is freeform; the render
resolution is not. It snaps to three tiers (384 / 512 / 900) behind the drag, and
nothing re-renders until the drag ends — a render costs tens of milliseconds and
scales with area, so re-rendering per pixel of drag would thrash. The browser
scales the image between tiers, which makes them invisible. Presets stay as one-
click jumps and light up when the width matches, the way Nuke and Resolve do it.

**Coloured cryptomatte view, with ctrl-click picking.** The preview switches
between the render and the Nuke/AE-style coloured ID view. Ctrl-clicking an object
toggles it, selected objects stay lit while the rest dim, and picking is free — it
reads the ID plane the preview was already built from rather than touching the
466 MB file again.

Two things had to be right. **ID planes are subsampled, never filtered**: averaging
two IDs produces a third that matches no object, so the preview would invent
objects that do not exist. And the click is measured against the `<img>` rect, not
the box, because `object-fit: contain` leaves letterbox bands that belong to no
pixel.

**Output and Cryptomatte became tabs.** Stacked, they overflowed the column. The
actual cause of the scrollbar turned out to be subtler than the panel count:
`[hidden]` only gets `display: none` from the UA stylesheet, so `.settings` (grid)
and `.crypto-body` (flex) beat it on specificity and *both* bodies stayed laid out.
`el.hidden` still reported `true` throughout, which is why it looked like a sizing
problem rather than a visibility one.

**Cryptomatte matte export.** The manifest is read out of EXR metadata and every
object or material listed for selection; ticked objects export as white
silhouettes with alpha, one file each or combined.

Validated against a real Blender 5.2 render rather than only the synthetic
fixture, which is what surfaced the things that actually matter: a single rank's
coverage can exceed 1.0 (2.633 measured — the pixel filter accumulates), mattes do
*not* sum to 1.0 in practice because rank count is capped by the render's Levels
setting, and object names are arbitrary Unicode. Ten of that file's own manifest
hashes are now known-answer tests for our MurmurHash3_32.

The one real constraint: OIIO's PNG writer always associates alpha, so "flat white
RGB + alpha" is indistinguishable from "coverage in RGB" in a PNG. That mode
therefore forces TIFF. The default — coverage in RGB and alpha — is a correct
white silhouette anyway, with properly premultiplied edges.

Picking by clicking the image still wants the viewer; selecting from a list does
not, which is why this landed first.

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

### 1. Viewer, stage two — the GPU path

Stage one shipped (see above): the layer is decoded once and kept, so exposure,
gamma, channel and layer changes are interactive. What is left is the part that
makes it feel like [tev](https://github.com/Tom94/tev) rather than a good preview.

Profiled at 900px on a 2160² 80-channel frame, with decoding already cached:

| | |
|---|---|
| display transform | 40 ms |
| **PNG encode** | **108 ms** |

Encoding is now the bottleneck, and there is no cheap fix: `png:compressionLevel`
is ignored by OIIO (identical file sizes at every level, measured), and JPEG —
about 4× faster — cannot carry alpha.

The real answer is to stop producing an image at all. OCIO has a GPU path; feeding
the cached linear layer to a WebGL canvas as a float texture and running the
display transform in a shader removes both the transform and the encode from every
interaction, leaving only the upload. That is how tev is fast, and it also gets
sequence playback (item 2) essentially for free, since frames stop round-tripping
through PNG.

Worth doing after enough real use to know whether 23 fps at 1080p is actually
limiting. For reviewing stills it may not be.

Still missing from the viewer regardless of the above:

- **A/B comparison** between two images, with a difference mode. tev's other
  genuinely good idea.
- **Higher-resolution render when zoomed past 1:1.** The window renders at a fixed
  1600px and shows real pixels beyond that; a 4K plate at 200% is therefore
  showing interpolated source. Re-rendering the visible crop at full resolution
  would fix it and is not hard, just not done.

### 2. Sequence player by the preview *(small — and measured)*

Step and scrub through a sequence's frames next to the existing preview. Much
smaller than the viewer above and worth doing first, because `make_thumbnail`
already does the work; only *which* frame it is handed has to change.

Measured cost of one preview frame, which is what sets the ceiling:

| Source | 512px preview | 256px preview |
|---|---|---|
| 1920×1080 RGBA half (16 MB) | **125 ms** | 92 ms |
| 2160², 80 channels (466 MB) | **860 ms** | 776 ms |

Halving the preview barely helps, which is the important finding: **the cost is
reading and decoding the EXR, not the transform or the resize**. That is fixed per
frame and cannot be optimised away — only avoided by not re-reading.

So the work splits cleanly:

- **Stepping and scrubbing** — a frame slider, prev/next, and a frame counter,
  re-rendering the preview on change. Straightforward. Feels fine on ordinary
  plates at ~8 fps; a heavy multi-AOV frame at 860 ms will feel like stepping, not
  scrubbing, and should show the spinner it already has.
- **Smooth playback** — needs the frames pre-rendered, because 125 ms/frame is 8 fps
  at best and 24 is not reachable by re-reading. Pre-render the sequence's previews
  into a cache using the thread pool from parallel conversion, then play from
  memory. A 512px preview is ~1 MB raw or ~150 KB as the PNG data URI already
  produced, so a 240-frame sequence caches in roughly 35 MB — cheap enough to hold.

Only the second part is real work, and it is the same latency problem the viewer
has, solved once for both.

### 3. Smaller things

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
- Cryptomatte objects are selected from a list. Picking by clicking the image
  needs the viewer (item 1).
