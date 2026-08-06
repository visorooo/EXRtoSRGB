# EXR → sRGB  ·  ACES linear → PNG / JPEG

A small batch converter so you don't have to round-trip through After Effects to
turn ACES-linear EXR renders into display-ready PNGs or JPEGs. Works on output
from Blender Cycles, C4D Octane and C4D Redshift.

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

**Format** — **PNG** or **JPEG**. JPEG is 8-bit and has no alpha, so picking it locks
bit depth to 8 and flattens; the quality dropdown only applies to JPEG.

**Alpha**
- *Keep alpha (RGBA)* — default, PNG only. JPEG flattens on black instead.
- *Flatten on black* / *Flatten on white* — composite and write RGB.

*Un-premultiply* is on by default. Renders write premultiplied (associated) alpha;
dividing it out **before** the transfer function is what gives clean edges, because
the curve then sees true surface colour rather than colour already faded toward the
background. The alpha is re-applied afterwards, so the file you get is
premultiplied — matching Nuke, and what every compositor expects. Turn it off only
if your EXR already carries straight alpha.

**Bit depth** — 8-bit (default) or 16-bit. 16-bit RGBA PNG is fully supported. JPEG
is always 8-bit.

**Output** — same folder as each source by default, or pick one folder. Optional
`_srgb` suffix so you don't overwrite anything.

**Light / dark** — the sun/moon button in the top right. The choice is remembered
in `%LOCALAPPDATA%\EXRtoSRGB\prefs.json`. Dark is the default and the better choice
when you're actually judging a render — a light surround biases how you read
exposure and saturation.

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
