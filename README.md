# EXR → sRGB  ·  ACES linear → PNG / JPEG / TIFF

A small batch converter so you don't have to round-trip through After Effects to
turn ACES-linear EXR renders into display-ready PNGs, JPEGs or TIFFs. Works on
output from Blender Cycles, C4D Octane and C4D Redshift. It can also write
**scene-linear 32-bit TIFF** when the destination is a comp rather than a screen.

The color is done with **OpenColorIO's built-in ACES configs** — the same engine
your renderers use — so the output matches your viewport instead of a naive gamma
guess. The ACES output transform (RRT + ODT) is baked in.

Built at [VISOR](https://github.com/visorooo) for 3D/CGI/VFX production.

---

## Download

Grab `EXRtoSRGB.exe` from the [Releases](../../releases) page. It's self-contained —
the OIIO/OCIO DLLs and the ACES configs are compiled in, so there's nothing to
install and no config file to ship alongside it.

---

## Run from source

```
pip install OpenImageIO OpenColorIO pywebview
python exr2srgb.py
```

## Build the standalone .exe (on Windows)

Double-click **`build_exe.bat`**. `EXRtoSRGB.exe` lands in this folder, next to the
script — PyInstaller's scratch goes to `%TEMP%` so the repo stays readable.

Notes:
- The spec's `collect_all` calls bundle the OIIO/OCIO DLLs — don't drop them.
- The `ui/` folder ships as data because it *is* the interface.
- The ACES configs are compiled into OpenColorIO, so there's **no config file to
  ship** — the exe is self-contained.
- Custom icon: add `icon='app.ico'` to the `EXE(...)` block in the spec.

---

## Adding files

Drag `.exr` files or whole folders anywhere onto the window, or use **Add files** /
**Add folder**. Anything that isn't an `.exr` is ignored.

**Image sequences are collapsed automatically.** A folder of
`shot_010_beauty.0001.exr … .0240.exr` shows up as one row reading
`shot_010_beauty.####.exr · 240 frames · 1–240`, so a render doesn't bury the rest
of the list. Every frame still converts. Click a row to preview it; arrow keys move
through the list.

---

## Layer — read this if your render has AOVs

A multi-layer EXR stores its channels as `<layer>.<component>`, so a Blender render
with AOVs contains fifteen things called `…R`, `…G`, `…B`. **Layer** picks which one
gets converted:

- **Auto · detect beauty** (default) — prefers a plain `R,G,B` image, then a layer
  named like `Beauty` / `Combined` / `Composite`, preferring a denoised variant.
  Data passes (cryptomatte, normals, position, depth, motion vectors) are never
  auto-picked. The chosen layer is shown under the preview and logged per file.
- **An explicit layer** — populated from the selected file. If another file doesn't
  have that layer, it falls back to auto-detect and warns rather than writing
  something wrong.

If no layer credibly looks like a beauty pass, it converts the best candidate and
says so. Check that line before trusting a batch.

---

## Settings

**OCIO configuration** — eight ACES configs are compiled in:

| | CG | Studio |
|---|---|---|
| **ACES 1.3** | v1.0 *(After Effects)*, v2.1, v2.2 *(default)* | v1.0 *(After Effects)*, v2.1, v2.2 |
| **ACES 2.0** | v4.0 | v4.0 |

Default is **ACES 1.3 · CG v2.2**, which matches the output transform in most
current Blender / Octane / Redshift ACES setups. If your render looks slightly off
versus the viewport, that's the first thing to change.

The two entries marked *(After Effects)* are the same configs AE lists. **ACES 1.2
is not included** — it predates OCIO's built-in registry and exists only as a
downloadable `config.ocio`, so it can't be compiled into the exe. Use
**Custom config.ocio…** at the bottom of the dropdown and point it at AE's copy (or
any studio config); the input, display and view lists repopulate from whatever you
load.

**Input color space** — `ACEScg` by default (the AP1-linear space your beauty EXRs
are stored in). Use `ACES2065-1` if you exported AP0.

**Output display** — `sRGB - Display` for screen, `Rec.1886 Rec.709 - Display` for
video.

**Look**
- *Tone-mapped (viewport)* — the full ACES filmic curve. What you see in the
  viewport. Default.
- *Un-tone-mapped* — gamut + transfer only, no filmic rolloff. Use if your viewport
  is set to a "standard"/raw view.
- *Scene-linear (no transform)* — see below. Not a look at all, but the third
  mutually exclusive answer to "what happens to these pixels".

**Format** — **PNG**, **JPEG**, or **TIFF**. What each can carry:

| | Bit depths | Alpha |
|---|---|---|
| PNG | 8, 16 | yes |
| JPEG | 8 | no — flattens |
| TIFF | 8, 16, **32-bit float** | yes |

The controls follow the container rather than accepting a setting and ignoring it:
picking JPEG locks bit depth to 8, and 32-bit is offered only for TIFF, because
32-bit means *float* and neither PNG nor JPEG has a float format.

**Alpha**
- *Keep alpha (RGBA)* — default, PNG only. JPEG flattens on black instead.
- *Flatten on black* / *Flatten on white* — composite and write RGB.

*Un-premultiply* is on by default. Renders write premultiplied (associated) alpha;
dividing it out **before** the transfer function is what gives clean edges, because
the curve then sees true surface colour rather than colour already faded toward the
background. The alpha is re-applied afterwards, so the file you get is
premultiplied — matching Nuke, and what every compositor expects. Turn it off only
if your EXR already carries straight alpha.

**Bit depth** — **16-bit by default**, or 8-bit, or 32-bit float on TIFF. 16-bit is
the safer default for anything heading back into a comp, since it keeps the
gradients intact after the display transform. Drop to 8-bit for delivery. JPEG is
always 8-bit.

---

## Scene-linear output

Everything above produces a *picture*: the ACES transform is applied and the result
is display-referred. **Scene-linear** does the opposite — it writes the render's
original linear values with **no display transform at all**.

Use it when the destination is another piece of software rather than an eye: a
comp, a grade, a plate hand-off. Use the display modes when the destination is a
screen.

Picking it constrains everything else, because those constraints are real:

- **Output is 32-bit float TIFF.** Linear values run past 1.0 and cluster near
  zero; an 8- or 16-bit integer container would clip and band away exactly what the
  mode exists to preserve. TIFF is the only format here with a float mode.
- **Input colour space and output display grey out.** Nothing is being converted,
  so they have nothing to act on.
- **Un-premultiply is ignored.** The pixels are passed through untouched — that is
  the guarantee, and it is asserted bit-for-bit in the test suite.
- **The suffix becomes `_linear`**, and the file is tagged `colorspace=Linear`.
  Naming a linear render `_srgb` is how it ends up with a transfer function applied
  twice downstream.
- **The preview stays display-referred** and says so. Showing raw linear values
  would be a near-black smear, so the preview remains a framing and layer check
  rather than a colour one.

What you get back is bit-identical to the source layer, in a container more tools
will open than EXR.

**Output** — same folder as each source by default, or pick one folder. The
**`_srgb` suffix is on by default**, so converting in place never sits a `.png`
next to its `.exr` with the same stem and no way to tell which came from where.
Turn it off if your naming is already handled downstream.

**Light / dark** — the sun/moon button in the top right. The choice is remembered
in `%LOCALAPPDATA%\EXRtoSRGB\prefs.json`. Dark is the default and the better choice
when you're actually judging a render — a light surround biases how you read
exposure and saturation.

---

## Cryptomatte

If the selected file has cryptomattes, a **Cryptomatte** tab appears next to
Output settings. It reads the manifest out of the EXR metadata and lists every
object or material in the render; tick the ones you want and hit **Export mattes**.

**The preview switches to coloured IDs** — the view Nuke and AE show — using the
**Render / IDs** toggle above it, or automatically when you open the tab. Each
object gets a stable colour derived from its hash, and ranks are composited so
edges blend rather than alias.

**Ctrl-click the ID view to select objects directly.** Clicking an object toggles
it, ticked objects stay lit and everything else dims, so you can see the selection
build up on the image instead of hunting through the list. Picking costs nothing —
it reads the ID plane the preview was already built from rather than touching the
file again.

- **Type** — Blender writes `CryptoObject` and `CryptoMaterial`; Redshift and
  others add their own. Each is listed with its object count, and selections are
  remembered per type while you switch between them.
- **Filter** — type a fragment to narrow a long list. **Select all** acts on
  what the filter is showing, not the whole scene.
- **One file per object** or **Combine into one** — the union of everything
  ticked.

Mattes never go through the display transform. Coverage is data, not colour, and
running it through an ACES view would be as wrong as tone-mapping a normal pass.
Files come out tagged `colorspace=Linear`.

**White silhouette (premultiplied)** is the default: coverage lands in RGB *and*
alpha, so a fully covered pixel is white, a soft edge is correctly premultiplied
white, and the matte is usable even in something that ignores alpha. **Flat white
RGB** pins RGB to 1.0 and leaves the shape only in alpha — that one needs TIFF,
because OIIO's PNG writer always associates alpha and would collapse the two
modes into the same file.

A note on accuracy: cryptomatte stores a fixed number of *ranks* per pixel (how
many overlapping objects it can remember), set by the **Levels** value at render
time. Where more objects overlap than there are ranks, coverage is genuinely
missing from the file and no tool can recover it. Coverage is also clamped to 1.0
on the way out, because a single rank can legitimately exceed it — a Blender
render measured here reached 2.633, since the pixel filter accumulates.

---

## Sanity check (these are correct ACES values)

| ACEScg input | ACES 1.3 → sRGB 8-bit | ACES 2.0 → sRGB 8-bit |
|---|---|---|
| 0.18 (mid grey) | 91 | 89 |
| 1.0 | 207 | 180 |
| 4.0 (bright) | 244 | 229 |

Highlights roll off smoothly instead of clipping to 255 — that's the ACES curve
working. These values are asserted in the test suite.

## Verified against Nuke

`exrs_tests/` holds beauty renders straight out of Blender Cycles and C4D Redshift
alongside Nuke conversions of the same frames. Mean 8-bit error against those
references:

| Source | Layers | Mean error | Max |
|---|---|---|---|
| Blender Cycles beauty | 15 AOVs | 1.09 | 15 |
| Redshift beauty | flat RGB | 0.13 | 2 |
| Redshift w/ alpha | 10 AOVs | 0.05 | 28 |

The residual is ODT version drift between OCIO and Nuke, not a pipeline error.

```
pip install pytest
pytest -q
```

---

## How it's built

`core.py` is the conversion — colour pipeline, layer resolution, alpha handling,
thumbnails, sequence grouping. It imports no UI and is what the tests exercise.

`exr2srgb.py` is a [pywebview](https://pywebview.flowrl.com/) window: Python stays
the application and the interface is HTML/CSS in the OS's own webview (WebView2 on
Windows), so there's no bundled browser. `ui/theme.css` is the VISOR warm-neutral
palette, shared with the other VISOR tools, and `ui/select.js` reproduces the
invoice app's dropdown — same easing, same press and open animations — so the tools
feel like one family.

---

## License

MIT — see [LICENSE](LICENSE).

OpenImageIO, OpenColorIO and pywebview are bundled into the built `.exe` under their
own licenses (Apache-2.0/BSD-style); this repository's MIT license covers the Python
sources, `ui/`, and the build scripts only.
