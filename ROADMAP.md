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

**Animated dropdowns.** `@radix-ui/react-select` has the motion this wanted but
needs React, so `ui/select.js` rebuilds the same behaviour in plain DOM — 140ms open from `scale(0.95)`, transform-origin
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

## Shipped — v2.1

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

## Shipped — v3.0

**Every part of a multi-part EXR is a layer.** Blender's File Output node writes
one part per slot, so a custom AOV export arrives as N parts rather than N channel
groups — a 16-part render showed exactly one layer before this. Cryptomatte is read
from whichever part carries it.

**Single-channel and X/Y/Z passes read.** Octane writes ambient occlusion and depth
as a lone `Y`; Blender writes Normal and Position as `X,Y,Z` triples and Mist as a
lone `Z`. The same letter means different things in different files, so the whole
channel set decides.

**A/B comparison** in the viewer — flip, wipe, or difference, on one key. The
difference is taken in linear so a given gap reads the same in shadows and
highlights, with exposure as the gain control.

**Real pixels past 1:1.** The base render is capped at 1600px, so on anything larger
even "100%" was showing an upscale. The visible region is now re-rendered at source
resolution and laid over the top.

**Sequence stepping** next to the preview — slider, prev/next, `,` / `.`. Stepping
rather than playback, because one preview frame is 125 ms at 1080p and that is
decode cost.

**A command line** — `EXRtoSRGB.exe --cli`, or `cli.py` from source. Asserts the ACES
ladder through itself, since a second route to the same transform drifts silently.

**Presets**, saving everything except the output folder, and **convert every layer**
in one pass, each file named for its layer.

**The pixel probe reports the display colour** as well as the linear value, with an
eyedropper that holds a reading still, and every value copies on click.

**Settings and shortcuts** moved behind the cogwheel in the title bar.

**Shipped in the 3.0.x patches**, every one reported from real use rather than
found by looking:

- **3.0.1** — the A/B wipe was misplaced at any zoom. `clip-path` resolves in the
  element's own space *before* its transform, so the seam only agreed with the
  drawn line at zoom 1 with no pan; at a fitted 2.49x it landed over a thousand
  pixels away, which put B fully on or fully off. The seam is draggable now.
- **3.0.2** — copying a value never worked. `set_clipboard` passed a 64-bit
  `HGLOBAL` to `GlobalLock` with no `argtypes`, so ctypes marshalled it as a C
  `int` and raised on every call. `OpenClipboard` is retried as well: the clipboard
  is a single global lock, and anything else holding it failed four copies in
  eleven. Clicking a value now says "Copied", and only when the write succeeded.
- **3.0.3** — the version label sits beside the subtitle rather than across the bar.
- **3.0.4** — the exe carries its version in the filename.
- **3.0.5** — the `.exr` toggle could appear to do nothing. Windows records a chosen
  default under `FileExts\.exr\UserChoice`, and Windows 11 a second copy under
  `UserChoiceLatest`, and that outranks the class registration the app writes. The
  toggle reads it first now and clears it, and the registration repairs its own
  path at startup so a versioned filename stops breaking on upgrade.

## Shipped — v3.1

**An installer.** The app used to ship as a bare exe, and someone deleted theirs
after using it because it looked like something already installed. It now installs
per-user to `%LOCALAPPDATA%\Programs\EXRtoSRGB` with no admin prompt, appears in
Add/Remove Programs with a working uninstaller, and offers the `.exr` association
and the right-click menu as ticked-by-default choices during setup.

That also settles the versioned-filename problem for good: the installed exe sits
at a fixed path, so the association survives upgrades instead of needing the
startup repair added in 3.0.5. The version moved to the installer's filename.

**`--register` / `--unregister`**, so the installer calls the app rather than
carrying a second copy of the registry writes in Pascal. **`--diag`** prints what
the app thinks it is and what is actually registered, because every shell
integration bug looks the same from outside.

**An update check.** Asks GitHub on launch and shows a pill in the title bar only
when there is genuinely something newer. Silent on failure - a converter that
cannot reach GitHub still converts.

## Shipped — v3.1.1

**Double-click on `.exr` never worked on a machine that had Photoshop.** Reported
twice — here, and on a friend's clean install with the association box ticked. The
registration was correct the whole time; the assumption behind it was not.

Traced with the shell's own resolver rather than by reading our keys back:

- `HKCU\Software\Classes\.exr` named our ProgID, `HKCR\.exr` agreed, and there was
  no `UserChoice` anywhere — yet `AssocQueryString(ASSOCSTR_EXECUTABLE, ".exr")`
  returned `Photoshop.exe`.
- A control extension carrying nothing but the same registration resolved to our
  exe, so the keys were well formed.
- An `OpenWithList` MRU pointing elsewhere did **not** override that control, and
  nothing claimed `.exr` through `RegisteredApplications`. By elimination, `.exr`
  lost to Photoshop's machine-wide registration.

Since Windows 8, the default handler for a file type is `UserChoice`, and the hash
beside it is signed per user — no application can write one, and a forged one makes
Windows discard the association entirely. **So this was never fixable the way it was
attempted**, and it only ever appeared to work on machines where the user had picked
the app by hand in "Open with", which is what writes a real UserChoice.

What changed:

- **`association_state` asks the shell.** It compares `AssocQueryString` to the
  running exe instead of reading back what it just wrote. The old check agreed with
  itself no matter what Windows did, which is why the toggle read "on" while
  double-click opened Photoshop.
- **`choose_default()`** opens whichever UI the running Windows still allows.
  `SHOpenWithDialog` — the old one-click chooser — **no longer sets defaults on
  Windows 11**: it answers with a message box reading "To change your default
  apps, go to Settings > Apps > Default apps". So 11 gets that Settings page,
  deep-linked to our entry; 10 keeps the dialog.
- **We are now choosable, not just registered.** The Settings page needs an app
  to have declared `Capabilities` and a `RegisteredApplications` entry, and the
  "Open with" list needs `Applications\<exe>\FriendlyAppName` or it shows the raw
  filename. None of that existed, so even the manual route was worse than it had
  to be.
- **`--register assoc|context`** selects one part. Both registrations rode on the
  installer's association task, so ticking only the right-click menu did nothing.

**The registration was incomplete, not impossible.** Once `OpenWithProgids`,
`Capabilities` and `Applications\<exe>` were all in place, the same `.exr` class
default that had been losing to Photoshop started winning — no UserChoice, no
Settings trip, double-click straight into the viewer with the aperture icon.
Measured both ways on the same machine.

So `set_association` now writes the claim, asks the shell whether it took, and
withdraws it if not. A claim Explorer ignores is worse than none: Settings reads
that key to decide what to *show* as the default, so it displays this app and
greys out "Set default" — the user is locked out of fixing an association the app
broke. The Settings route stays as the fallback for machines where the claim
genuinely loses.

The honest limitation, still in the README: an app cannot *force* a file type on
Windows, and where it loses, the extra click is Microsoft's design rather than a
workaround.

## V3.2

Nothing here is a bug — v3.0 does what it set out to do. These are the four things
that would make it better, in the order I would take them.

| | | |
|---|---|---|
| **Explorer thumbnails** | large | the biggest visible win, and the only item that is not Python |
| **Ctrl+Space preview** | medium | both mechanisms already proven; the work is a tray presence |
| **Viewer GPU path** | large | wait for real use to say whether 23 fps is limiting |
| **Smaller things** | small | per-file layer override, WebP, presets polish |

**What decides the order.** Thumbnails and the hotkey preview change how the tool
feels before it is even opened, which is worth more than making an already
interactive viewer faster. The GPU path is deliberately last: the roadmap gates it
on knowing whether the current speed actually gets in the way, and until v3.0 has
been used in anger that is a guess.

### 1. Explorer thumbnails for .exr *(the first non-Python part)*

Show the actual image as the file's thumbnail in Explorer, with the aperture icon
kept as a small badge in the bottom-right corner so the file type is still
readable at a glance.

Prior art: **[hdr-thumb](https://github.com/VitalSkib/hdr-thumb)** already does
this for HDR, EXR and SVG and is what these files are previewed with today. Before
adapting any of it, check its licence — a repo without a LICENSE grants no
redistribution rights regardless of how public it is, which is the rule the rest of
the VISOR tools already follow.

**This cannot be done in Python.** Windows thumbnails come from an
`IThumbnailProvider` COM object that Explorer loads **in-process** (or into a COM
surrogate), registered at:

```
HKCU\Software\Classes\.exr\ShellEx\{e357fccd-a995-4576-b01f-234630154e96}
```

That has to be a native DLL — C++ or Rust. There is no supported extension point
that lets a separate executable answer, and shelling out to a 37 MB PyInstaller
binary per file would be far too slow even if there were. So this is the first
piece of the project that is not Python, with its own toolchain and build.

**Measured, because it decides the design:** these renders carry **no embedded
preview and no mip levels** — checked on a 2160² Blender frame and a 1080p plate,
neither has a `PreviewImage` attribute. So the provider must decode the full image
every time. Cost through this codebase's own path is **203 ms for a single-layer
2160² frame and ~780 ms for an 80-channel one**. Explorer generates thumbnails
asynchronously and caches them, so that is workable; the multi-AOV case is the one
to watch, and reading only the beauty layer's channels — which `read_layer` already
does — is what keeps it from being far worse.

Shape of the work:

- **Decode** with TinyEXR (single header, MIT) or OpenEXR.
- **Colour: decided — approximate is fine.** Thumbnails do not need to match the
  viewer, so **no OCIO in the DLL**. A Filmic or Reinhard curve with an sRGB
  transfer is enough, which removes the single heaviest dependency and most of the
  size. Thumbnails are indicative; the viewer remains the thing that is correct.
  This is what makes the item worth doing at all rather than a large build for a
  small payoff.
- **Badge.** Trivial once the bitmap exists: composite `exr.ico` into the
  bottom-right before returning the HBITMAP.
- **Registration** alongside the existing per-user association toggle.
- **64-bit only.** Modern Explorer will not load a 32-bit provider.

Two operational wrinkles worth knowing before starting: Explorer keeps the DLL
loaded, so replacing it during development needs `explorer.exe` killed or a
reboot; and the thumbnail cache has to be busted after a change, the same
`ie4uinit -show` problem the file icon already had.

### 2. Space-bar preview, QuickLook style *(feasible in Python — proven)*

Select an `.exr` in Explorer, press a key, see it. The same gesture
**[QuickLook](https://github.com/QL-Win/QuickLook)** provides, which is already how
these files get previewed day to day.

**Default to Ctrl+Space, not Space.** QuickLook owns Space, and a lot of people run
it; taking that key would break a tool they already rely on. The binding is
configurable, shown in the main window so it is discoverable rather than folklore,
and stored in `prefs.json` beside the rest.

**Both hard parts are already proven to work from Python**, tested on this machine
rather than assumed:

- **The global hotkey** is `RegisterHotKey` through `ctypes` — no dependency at
  all, and it returned success for Ctrl+Space.
- **Reading Explorer's selection** works through `Shell.Application` with
  `comtypes`: enumerate shell windows, match the foreground `HWND`, and read
  `Document.SelectedItems()`. Tested live and it returned the exact selected
  `.exr` path.

So there is no unknown in the mechanism. The actual work is that **the hotkey needs
a resident process** — a key only reaches an application that is running. That
means the piece to design is the same one QuickLook has:

- A **tray presence** with a lightweight background mode, so the converter window
  does not have to stay open.
- An **optional start-with-Windows** entry, per-user under `Run`, behind a toggle
  like the file association is. Nothing should add itself to startup silently.
- Opening the existing viewer window for the selected file, which already exists —
  `open_viewer()` takes a path and does the rest.

Two details worth settling when it is built: the Desktop is a shell window too but
reports its selection differently from a folder window, and pressing the key with
several files selected should probably open the first rather than a window each.

Note `comtypes` was installed while proving this and is not yet used by anything;
it would become a real dependency and needs adding to the spec if this ships.

### 3. Viewer, stage two — the GPU path

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

### 4. Sequence playback — **stepping done in v3.0**

Stepping and scrubbing shipped: a frame strip under the preview with prev/next, a
slider, a counter, and `,` / `.`. The preview and the pixel probe both follow the
frame. **Smooth playback did not** — it needs the pre-rendered cache described
below, which is the same latency problem the GPU path solves, so it waits for that.

The original write-up, kept because the measurements are what decide the design:

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

### 5. Smaller things

- ~~**Convert all layers at once**, one file per AOV.~~ **Done in v3.0.** A pass per
  layer with `layer_tag` in the suffix; without that they collide on one filename.
- ~~**Presets** — save a named set of settings.~~ **Done in v3.0.** Everything except
  `out_dir`, which is where rather than how.
- ~~**CLI entry point.**~~ **Done in v3.0** — `cli.py`, plus `EXRtoSRGB.exe --cli`.
  Asserts the ACES ladder through itself, because a second route to the same
  transform drifts silently otherwise.
- **Per-file layer override.** The dropdown applies one choice to the batch, falling
  back to auto-detect per file. Mixed batches would benefit from remembering a
  choice per entry. Less pressing now that "every layer" exists — that covers the
  common case of wanting all of them.
- ~~**Multi-part EXR.** Only the first subimage is read.~~ **Done in v3.0**, and
  it was not rare at all — the note above was wrong. Blender's **File Output**
  node writes one part per slot, so the custom AOV pass a comp is rebuilt from
  arrives as N parts. A real 16-part render showed exactly one layer, and a
  2-part glare/highlights pass showed only glare, with nothing to indicate the
  rest existed. Cryptomatte is now read from whichever part carries the metadata
  too.
- **WebP** output — **available**, the bundled OIIO has a WebP writer (checked).
  8-bit only, so it slots in beside JPEG rather than replacing anything.
- **JPEG-XL** — **blocked, not a choice.** This OIIO build has no `jxl` writer
  (`ImageOutput.create` returns nothing and the format is absent from
  `extension_list`). It needs an OIIO built with libjxl, which is a dependency
  change rather than a feature.

---

---

## Reported items, traced and ranked

Each of these was checked against the code rather than taken at face value. Order
is by cost of leaving it alone, not by effort.

**Items 1–6 shipped in v2.1.** They are kept here with what was actually found,
because the trace is the useful part. Items 7 and 8 were dropped by decision, not
by cost — the reasoning below still stands if either comes back.

### 1. Viewer export ignores the selected layer *(bug — do first)* — **done**

**Confirmed.** `ViewerApi.convert()` calls `convert_cli()`, which builds its
settings with `"layer": None`. That re-runs beauty auto-detection, so exporting
while Ambient Occlusion is on screen writes the beauty instead — silently, and the
file looks plausible.

This is the same shape as the bug that shipped through the whole of v1.0: wrong
output that nobody notices because nothing looks broken.

*Fixed* by threading `self._session.layer` through `convert()` into the settings,
and by naming the file after the layer via a new `core.layer_tag()`. Verified on a
three-layer EXR: the same source now yields `_Combined_srgb.png`,
`_Ambient_Occlusion_srgb.png` and `_Diffuse_Color_srgb.png` with different pixels —
AO comes out achromatic, the beauty does not. Without the tag the second export
would have overwritten the first, so both halves were needed.

### 2. Read a whole sequence from one dropped frame — **done**

**Confirmed missing.** `group_sequences` only groups files that were actually
added, so dropping `beauty.0001.exr` gave a single entry; only dropping the folder
collapsed the run.

*Fixed* with `core.find_sequence_siblings()`, called from `_add_paths`. It matches
stem, padding **and** extension in the same directory — padding because
`shot.0001.exr` and `shot.000001.exr` are two different runs, extension because a
`.png` render beside the `.exr` is not part of it.

The count is reported ("11 pulled in from the sequence"), since quietly turning one
file into 240 is surprising otherwise.

### 3. One converter instance, unlimited viewers — **done**

**Confirmed missing** — there was no guard of any kind.

*Fixed* with a named mutex, claimed in `main()` **after** the `--view` and
`--convert` early returns. That ordering is the whole design: opening several
images at once is the point of a viewer, and the shell's right-click convert has to
run whether or not a converter window is open. Only the converter is single.

### 4. Cryptomatte: Ctrl picks, Alt unpicks — **done**

Ctrl-click used to toggle. Now Ctrl adds and Alt removes, which is more predictable
when working quickly because you stop having to remember what state a given object
is already in. The hint under the preview says so.

### 5. Hex and a swatch in the pixel probe, click to copy — **done**

The probe reported linear scene values, which is right for judging a render but is
not what a hex code means.

*Fixed*: `ViewerSession.sample()` now returns the display values and the hex
alongside the linear ones, so the chip and the copied string are what is on screen.
Exposure moves the hex and leaves the linear reading alone — a test asserts both.
Copying goes through Win32 from Python, because `navigator.clipboard` is
unavailable on a `file://` origin.

### 6. A shortcuts panel — **done**

Both windows have one now, opened with **?** or the toolbar button. The markup
differs but the styles are shared from `app.css`, so the two read as the same app.
Added `Ctrl+Enter` (convert), `Ctrl+O` (add files) and `Esc` (cancel) while
documenting, since a panel listing three things is not worth opening.

### 7. Video output for sequences — *dropped* (recommend not bundling)

**The cost is licensing, not code.** OIIO cannot write video and there is no ffmpeg
on this machine, so shipping this means bundling an encoder: roughly +80 MB on a
37 MB download, and ffmpeg is LGPL or GPL depending on the build — a GPL build
cannot be redistributed under this repo's MIT licence, and LGPL brings its own
notice and relinking obligations.

Suggested instead: **use ffmpeg if it is on PATH**, and say so plainly when it is
not. Zero bundle cost, no licensing obligation, and anyone who wants video already
has Resolve, After Effects or ffmpeg. The tool keeps writing image sequences, which
is what those applications want as input anyway.

### 8. macOS — *dropped* (possible, but the best parts do not port)

`core.py` is already portable: OIIO, OCIO and numpy all run on macOS, and pywebview
uses WebKit there. The converter and viewer windows would work.

There are **51 Windows-specific references** in `exr2srgb.py` and every one is shell
integration: `winreg` for the association and convert verbs, `ctypes.windll`,
`ie4uinit`, `.ico` icons, the Explorer reveal. All of it needs a macOS equivalent —
`LSSetDefaultRoleHandlerForContentType`, `.icns`, a Quick Look generator, Finder
services — which is a second integration layer, not a port.

It also needs a Mac to build and test on, plus signing and notarisation before
anyone else can run it. Largest item here and gated on hardware.

## Known limitations

- Explorer shows the `.exr` file icon, not the image. A real thumbnail needs a
  native `IThumbnailProvider` DLL — see V3.2 item 1.
- There is no hotkey preview. The app has to be running to catch one, which needs a
  resident tray mode — see V3.2 item 2.
- The layer dropdown applies one choice to the batch, falling back to auto-detect
  per file. Mixed batches would want a choice remembered per entry.
- Sequence frames step rather than play; smooth playback needs a pre-rendered
  cache — see V3.2 item 4.
- The exe is unsigned, so SmartScreen warns on first run. A code-signing
  certificate is the only fix, and it is an annual cost rather than a code change.
- Windows only. `core.py` is portable, but every piece of shell integration is not
  — see the macOS entry above.
