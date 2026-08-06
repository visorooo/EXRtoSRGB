# EXR → PNG  ·  ACES linear → sRGB / Rec.709

A small batch converter so you don't have to round-trip through After Effects to
turn ACES-linear EXR renders into display-ready PNGs. Works on output from
Blender Cycles, C4D Octane and C4D Redshift.

The color is done with **OpenColorIO's built-in ACES configs** — the same engine
your renderers use — so the PNG matches your viewport instead of a naive gamma
guess. The ACES output transform (RRT + ODT) is baked in.

Built at [VISOR](https://github.com/visorooo) for 3D/CGI/VFX production.

---

## Download

Grab `EXRtoPNG.exe` from the [Releases](../../releases) page. It's self-contained —
the OIIO/OCIO DLLs and the ACES configs are compiled in, so there's nothing to
install and no config file to ship alongside it.

---

## Run from source (quick test before building the exe)

```
pip install OpenImageIO OpenColorIO
python exr2png.py
```

## Build the standalone .exe (on Windows)

Double-click **`build_exe.bat`**, or run:

```
pip install OpenImageIO OpenColorIO pyinstaller
pyinstaller --onefile --windowed --name "EXRtoPNG" ^
  --collect-all OpenImageIO ^
  --collect-all PyOpenColorIO ^
  exr2png.py
```

The exe lands in `dist\EXRtoPNG.exe`.

Notes:
- The `--collect-all` flags bundle the OIIO/OCIO DLLs — don't drop them.
- The ACES configs are compiled into OpenColorIO, so there's **no config file to
  ship** — the exe is self-contained.
- One-file builds are big (~200–400 MB) and start a bit slow because OIIO+OCIO
  are heavy. For instant startup use `--onedir` instead of `--onefile` (you'll
  get a folder you can zip).
- Custom icon: add `--icon app.ico` (like you did for the Render Time Calculator).
- Want drag-and-drop? `pip install tkinterdnd2` and add `--collect-all tkinterdnd2`
  to the build. The app auto-enables drop if the package is present.

---

## Settings, and the one that actually matters

**ACES version** — default is **ACES 1.3**, which matches the output transform in
most current Blender / Octane / Redshift ACES setups. If your render looks
slightly off vs the viewport (highlights too bright or too desaturated), flip to
**ACES 2.0**. That's the single most likely thing to need adjusting — Blender's
bundled ACES OCIO config is 1.x; only switch to 2.0 if your pipeline is on the
new transform.

**Input color space** — `ACEScg` by default (the AP1-linear space your beauty
EXRs are stored in). Use `ACES2065-1` if you exported AP0.

**Output display** — `sRGB - Display` for screen, `Rec.1886 Rec.709 - Display`
for video.

**Look**
- *Tone-mapped (match viewport)* — applies the full ACES filmic curve. This is
  what you see in the viewport. Default.
- *Un-tone-mapped (plain convert)* — gamut + transfer only, no filmic rolloff.
  Use if your viewport is set to a "standard"/raw view.

**Alpha** — keep RGBA or drop to RGB. *Un-premultiply* is on by default; renders
write premultiplied (associated) alpha, and un-premultiplying before the transfer
function gives clean straight-alpha PNGs with correct edges (same as AE's
interpret-as-premult). Turn it off only if you specifically want premultiplied.

**Bit depth** — 8-bit (default) or 16-bit. 16-bit RGBA PNG is fully supported.

**Output** — same folder as each source by default, or pick one folder. Optional
`_srgb` suffix so you don't overwrite anything.

---

## Sanity check (these are correct ACES values)

| ACEScg input | ACES 1.3 → sRGB 8-bit | ACES 2.0 → sRGB 8-bit |
|---|---|---|
| 0.18 (mid grey) | 91 | 89 |
| 1.0 | 207 | 180 |
| 4.0 (bright) | 244 | 229 |

Highlights roll off smoothly instead of clipping to 255 — that's the ACES curve
working.

---

## License

MIT — see [LICENSE](LICENSE).

OpenImageIO and OpenColorIO are bundled into the built `.exe` under their own
licenses (both Apache-2.0/BSD-style); this repository's MIT license covers
`exr2png.py` and the build scripts only.
