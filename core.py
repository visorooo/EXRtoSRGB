#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Conversion core for EXR -> sRGB.

Everything here is pure: no UI imports, no global state beyond caches. The UI
(exr2srgb.py) drives this module and is the only thing that knows about windows,
threads or HTML. Keeping the split means the colour behaviour is testable in
isolation - see tests/test_core.py.
"""

import os
import re
import json
import base64
import struct
import tempfile

import numpy as np
import OpenImageIO as oiio
import PyOpenColorIO as OCIO


# ----------------------------------------------------------------------------
# OCIO configs
#
# OCIO compiles a set of ACES configs into the library itself, which is why the
# exe ships with no config files alongside it. ACES 1.2 is NOT among them - it
# predates the built-in registry and only exists as a downloadable config.ocio.
# That is what CUSTOM_CONFIG is for: point it at After Effects' or a studio's
# config and any ACES version becomes available without bloating the binary.
# ----------------------------------------------------------------------------

CUSTOM_CONFIG = "__custom__"

# label -> built-in registry name. Order is the order shown in the UI.
ACES_CONFIGS = {
    "ACES 1.3 · CG v2.2  (recommended)": "cg-config-v2.2.0_aces-v1.3_ocio-v2.4",
    "ACES 1.3 · CG v1.0  (After Effects)": "cg-config-v1.0.0_aces-v1.3_ocio-v2.1",
    "ACES 1.3 · CG v2.1": "cg-config-v2.1.0_aces-v1.3_ocio-v2.3",
    "ACES 1.3 · Studio v1.0  (After Effects)": "studio-config-v1.0.0_aces-v1.3_ocio-v2.1",
    "ACES 1.3 · Studio v2.1": "studio-config-v2.1.0_aces-v1.3_ocio-v2.3",
    "ACES 1.3 · Studio v2.2": "studio-config-v2.2.0_aces-v1.3_ocio-v2.4",
    "ACES 2.0 · CG v4.0  (newest)": "cg-config-v4.0.0_aces-v2.0_ocio-v2.5",
    "ACES 2.0 · Studio v4.0": "studio-config-v4.0.0_aces-v2.0_ocio-v2.5",
}

DEFAULT_CONFIG_LABEL = list(ACES_CONFIGS)[0]

# Input colour spaces we offer, filtered against what the chosen config has.
# Studio configs name some of these differently, hence the aliases.
PREFERRED_INPUT_CS = [
    "ACEScg",
    "ACES2065-1",
    "ACEScct",
    "Linear Rec.709 (sRGB)",
    "Linear Rec.2020",
    "Linear P3-D65",
    "sRGB - Texture",
    "Linear CIE-XYZ D65",
]

LAYER_AUTO = "__auto__"

_config_cache = {}
_proc_cache = {}


def get_config(config_ref):
    """
    Resolve a config reference to an OCIO Config.

    `config_ref` is either a built-in registry name or a filesystem path to a
    config.ocio. Paths are cached by (path, mtime) so editing a config during a
    session picks the change up.
    """
    if os.path.sep in config_ref or config_ref.lower().endswith(".ocio"):
        key = (config_ref, os.path.getmtime(config_ref))
        if key not in _config_cache:
            _config_cache[key] = OCIO.Config.CreateFromFile(config_ref)
        return _config_cache[key]
    if config_ref not in _config_cache:
        _config_cache[config_ref] = OCIO.Config.CreateFromBuiltinConfig(config_ref)
    return _config_cache[config_ref]


def get_processor(config_ref, src, display, view):
    key = (config_ref, src, display, view)
    if key not in _proc_cache:
        cfg = get_config(config_ref)
        dvt = OCIO.DisplayViewTransform()
        dvt.setSrc(src)
        dvt.setDisplay(display)
        dvt.setView(view)
        _proc_cache[key] = cfg.getProcessor(dvt).getDefaultCPUProcessor()
    return _proc_cache[key]


def list_displays(config_ref):
    return list(get_config(config_ref).getDisplays())


def default_display(config_ref):
    return get_config(config_ref).getDefaultDisplay()


def list_input_spaces(config_ref):
    """Offer the preferred spaces this config actually has, best first."""
    cfg = get_config(config_ref)
    names = list(cfg.getColorSpaceNames())
    have = set(names)
    picked = [c for c in PREFERRED_INPUT_CS if c in have]
    # a config with none of the preferred names is still usable; show everything
    return picked if picked else names


def list_views(config_ref, display):
    return list(get_config(config_ref).getViews(display))


def view_for(config_ref, display, tone_mapped):
    """Return the OCIO view name for this display."""
    cfg = get_config(config_ref)
    views = list(cfg.getViews(display))
    if tone_mapped:
        # The config's default view IS the ACES output transform (RRT+ODT).
        return cfg.getDefaultView(display)
    for cand in ("Un-tone-mapped", "Raw"):
        if cand in views:
            return cand
    return views[0] if views else cfg.getDefaultView(display)


def describe_config(config_ref):
    """Human-readable name for a config, for logging."""
    for label, name in ACES_CONFIGS.items():
        if name == config_ref:
            return label
    return os.path.basename(config_ref)


# ----------------------------------------------------------------------------
# Layer / channel resolution
#
# A multi-layer EXR names its channels "<layer>.<component>". Matching only on
# the component is what once made this tool silently export Ambient_Occlusion
# instead of the beauty: Blender writes layers alphabetically, so
# "Ambient_Occlusion.R" is simply the first channel whose name ends in "R".
# Group into layers first, then choose deliberately.
# ----------------------------------------------------------------------------

_COMPONENT_ALIASES = {
    "r": "r", "red": "r",
    "g": "g", "green": "g",
    "b": "b", "blue": "b",
    "a": "a", "alpha": "a",
    # X/Y/Z, kept distinct rather than folded into anything. Blender writes
    # Normal and Position as an X,Y,Z triple and Mist and Denoising Depth as a
    # lone Z, while Octane writes ambient occlusion and depth as a lone Y - so
    # the same letter means a vector component in one file and a whole
    # single-channel pass in another. Only the full set tells them apart, which
    # is why the decision happens in group_layers and not here.
    "x": "x", "y": "y", "z": "z",
    "depth": "z", "luminance": "y",
}

# Layers that are data, not colour. Running these through an ACES view transform
# is meaningless, so they must never win the auto-pick.
_DATA_LAYER_TOKENS = (
    "crypto", "normal", "position", "depth", "motion", "vector", "velocity",
    "objectid", "materialid", "puzzle", "uv", "zdepth", "worldposition",
    "objectposition", "samplecount", "deep",
)

# Single-letter data layers, matched whole rather than as substrings: Redshift
# writes normals as "N" and depth as "Z", which no substring rule can catch
# without also condemning every layer containing an n or a z.
_DATA_LAYER_EXACT = frozenset(("n", "z", "p", "id", "st", "uv", "xyz"))

_BEAUTY_HINTS = (
    ("beauty", 500),
    ("combined", 450),
    ("composite", 400),
    ("rgba", 380),
    ("final", 350),
    ("main", 300),
)


def split_channel(name):
    """Split an EXR channel name into (layer, component). Bare -> layer ''."""
    if "." in name:
        layer, comp = name.rsplit(".", 1)
    else:
        layer, comp = "", name
    return layer, _COMPONENT_ALIASES.get(comp.lower())


def group_layers(channelnames):
    """
    Group channel names into {layer: {component: index}}, order preserved.

    Three shapes qualify, and telling them apart needs the whole set:

    - **R, G, B** - an ordinary colour layer.
    - **X, Y, Z** - a vector pass such as Normal or Position, mapped to R, G, B
      the way a compositor shows it.
    - **A lone Y or Z** - a single-channel pass, mapped to all three so it reads
      as greyscale. Octane writes ambient occlusion and depth this way, Blender
      writes Mist and Denoising Depth this way.

    The same letter therefore means different things in different files: `Z` is
    a vector component in `Normal.X/.Y/.Z` and a whole depth pass in `Mist.Z`.
    Requiring R, G and B made real passes invisible in files where everything
    else read fine, which looks like data loss rather than filtering.
    """
    layers = {}
    for i, name in enumerate(channelnames):
        layer, comp = split_channel(name)
        if comp is None:
            continue
        layers.setdefault(layer, {}).setdefault(comp, i)

    out = {}
    for name, comps in layers.items():
        have = set(comps)
        if {"r", "g", "b"} <= have:
            out[name] = comps
        elif {"x", "y", "z"} <= have:
            vec = {"r": comps["x"], "g": comps["y"], "b": comps["z"]}
            if "a" in comps:
                vec["a"] = comps["a"]
            out[name] = vec
        elif name and len(have - {"a"}) == 1 and (have - {"a"}) <= {"y", "z"}:
            # Named only. The bare layer scores as "an ordinary image" and would
            # be auto-picked as the beauty, so a file whose only top-level
            # channel is Z would silently convert as though it were a render.
            # Refusing that is the invariant test_no_rgb_raises protects.
            i = comps[next(iter(have - {"a"}))]
            mono = {"r": i, "g": i, "b": i}
            if "a" in comps:
                mono["a"] = comps["a"]
            out[name] = mono
    return out


def score_layer(layer):
    """How strongly this layer name reads as 'the beauty'. Higher wins."""
    if layer == "":
        return 1000  # bare R/G/B - an ordinary single-layer image
    flat = layer.lower().replace("_", "").replace(" ", "")
    if flat in _DATA_LAYER_EXACT:
        return -1000
    for token in _DATA_LAYER_TOKENS:
        if token in flat:
            return -1000
    for hint, points in _BEAUTY_HINTS:
        if hint in flat:
            return points + (20 if "denois" in flat else 0)
    return 0


def pick_layer(channelnames, requested=None):
    """
    Resolve which layer to convert.

    Returns (layer_name, components, note). `note` is a human-readable warning
    when the choice was not clear-cut, or None.
    """
    layers = group_layers(channelnames)
    if not layers:
        raise ValueError("no RGB channels found in %d channels" % len(channelnames))

    if requested is not None and requested in layers:
        return requested, layers[requested], None

    note = None
    if requested is not None:
        note = "layer %r not in this file, auto-detected instead" % requested

    ranked = sorted(layers.items(), key=lambda kv: -score_layer(kv[0]))
    best, comps = ranked[0]
    if score_layer(best) <= 0:
        extra = "no layer looks like a beauty pass, using %r" % (best or "R,G,B")
        note = "%s; %s" % (note, extra) if note else extra
    return best, comps, note


def layer_tag(layer):
    """
    A filename-safe fragment naming a layer, or "" for the bare/auto case.

    Exports from two different layers of the same EXR would otherwise land on
    the same filename, so the second silently replaces the first.
    """
    if not layer:
        return ""
    safe = re.sub(r"[^\w.\-]+", "_", layer).strip("_")
    # Blender prefixes every layer with the view layer; it adds nothing here
    safe = safe.rsplit(".", 1)[-1] if safe.count(".") else safe
    return "_" + safe if safe else ""


def find_sequence_siblings(path):
    """
    Every frame of the run `path` belongs to, or just `path` if it is not one.

    Dropping `beauty.0001.exr` should bring the sequence with it - that is what
    the file means. Matching is on directory, stem, padding width and extension,
    so `beauty.0001.exr` never absorbs `beauty_v2.0001.exr` or a differently
    padded run.
    """
    directory, fname = os.path.split(path)
    stem_ext, ext = os.path.splitext(fname)
    m = _FRAME_RE.match(stem_ext)
    if not m or not m.group("frame"):
        return [path]

    stem, pad = m.group("stem"), len(m.group("frame"))
    out = []
    try:
        entries = os.listdir(directory or ".")
    except OSError:
        return [path]
    for other in entries:
        o_stem_ext, o_ext = os.path.splitext(other)
        if o_ext.lower() != ext.lower():
            continue
        om = _FRAME_RE.match(o_stem_ext)
        if om and om.group("stem") == stem and len(om.group("frame")) == pad:
            out.append(os.path.join(directory, other))
    return sorted(out) or [path]


def image_size(path):
    """(width, height) without decoding any pixels."""
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    spec = src.spec()
    size = (spec.width, spec.height)
    src.close()
    return size


def layer_index(path):
    """
    Every convertible layer in an EXR, across every part.

    Returns an ordered `{name: (subimage, components)}`.

    Multi-part matters more than it sounds. Blender's File Output node writes
    one **subimage per slot**, so a custom AOV pass - the thing anyone actually
    recombines a beauty from in comp - arrives as 16 parts rather than 16
    channel groups. Reading only part 0 shows exactly one layer and hides the
    rest, with nothing to suggest anything is missing.

    Naming: layers keep their own name where they have one, since Blender
    prefixes each part's channels with it. A part whose channels are bare RGBA
    falls back to the part name, then to its index. Part 0 keeps the empty name
    for a plain single-part image, which is what the rest of the code and every
    saved setting already expect.
    """
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    out = {}
    try:
        sub = 0
        while src.seek_subimage(sub, 0):
            spec = src.spec()
            part = spec.getattribute("name") or ""
            for lname, comps in group_layers(list(spec.channelnames)).items():
                key = lname or (part if sub else "")
                if not key and sub:
                    key = "part%d" % sub
                # Two parts can carry identically named layers; a name that
                # silently overwrote another would hide a pass completely.
                if key in out:
                    key = "%s (part %d)" % (key or "R,G,B", sub)
                out[key] = (sub, comps)
            sub += 1
    finally:
        src.close()
    return out


def probe_layers(path):
    """Return the convertible layer names in an EXR, best-guess first."""
    index = layer_index(path)
    return sorted(index, key=lambda k: (-score_layer(k), k))


def pick_from_index(index, requested=None):
    """
    Resolve which layer to convert, given a multi-part index.

    Same rules as pick_layer, which stays for the single-spec case: never match
    on the component suffix, score names, and say so when the choice was not
    clear-cut.
    """
    if not index:
        raise ValueError("no RGB channels found")
    if requested is not None and requested in index:
        sub, comps = index[requested]
        return requested, sub, comps, None

    note = None
    if requested is not None:
        note = "layer %r not in this file, auto-detected instead" % requested

    best = sorted(index, key=lambda k: (-score_layer(k), k))[0]
    sub, comps = index[best]
    if score_layer(best) <= 0:
        extra = "no layer looks like a beauty pass, using %r" % (best or "R,G,B")
        note = "%s; %s" % (note, extra) if note else extra
    return best, sub, comps, note


def read_layer(path, requested=None):
    """
    Read one layer out of an EXR, from whichever part holds it.

    Returns (rgb float32 HxWx3, alpha float32 HxW or None, W, H, layer, note).
    Only the channels needed are read - a 60-channel Blender AOV dump is
    otherwise half a gigabyte of float for three channels of output.
    """
    index = layer_index(path)
    layer, sub, comps, note = pick_from_index(index, requested)

    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    try:
        if not src.seek_subimage(sub, 0):
            raise IOError("part %d missing from %s" % (sub, path))
        spec = src.spec()
        W, H = spec.width, spec.height
        wanted = [comps["r"], comps["g"], comps["b"]]
        has_alpha = "a" in comps
        if has_alpha:
            wanted.append(comps["a"])
        lo, hi = min(wanted), max(wanted) + 1
        pixels = src.read_image(sub, 0, lo, hi, "float")
    finally:
        src.close()

    arr = np.array(pixels, dtype=np.float32).reshape(H, W, hi - lo)
    rgb = np.ascontiguousarray(arr[..., [i - lo for i in wanted[:3]]])
    alpha = np.ascontiguousarray(arr[..., comps["a"] - lo]) if has_alpha else None
    return rgb, alpha, W, H, layer, note


# ----------------------------------------------------------------------------
# The transform itself
# ----------------------------------------------------------------------------

def apply_transform(rgb, alpha, W, H, settings):
    """
    ACES linear -> display, returning float RGB(A) in 0..1 plus the alpha.

    The un-premultiply / re-premultiply pair here is load-bearing. Dividing alpha
    out before the transform is what lets the transfer function see true surface
    colour instead of colour already faded toward the background. Putting it back
    afterwards is what keeps the file associated, which is how every compositor
    reads PNG edge pixels. Breaking either half puts a bright fringe on every
    antialiased edge.

    With transfer="linear" none of that happens: the pixels are handed back
    exactly as they came off disk. No OCIO, no alpha juggling, and crucially no
    clamp, so values above 1.0 survive. That is the whole point of the mode - it
    exists to move scene-referred data, not to make a picture.
    """
    if settings.get("transfer") == "linear":
        return rgb, alpha

    unpremulted = False
    if alpha is not None and settings["unpremult"]:
        mask = alpha > 1e-6
        if mask.any():
            rgb = rgb.copy()
            inv = np.zeros_like(alpha)
            inv[mask] = 1.0 / alpha[mask]
            rgb *= inv[..., None]
            unpremulted = True

    proc = get_processor(settings["config"], settings["src"],
                         settings["display"], settings["view"])
    buf = np.ascontiguousarray(rgb, dtype=np.float32)
    proc.apply(OCIO.PackedImageDesc(buf, W, H, 3))
    buf = np.clip(buf, 0.0, 1.0)

    a = None
    if alpha is not None:
        a = np.clip(alpha, 0.0, 1.0).astype(np.float32)
        if unpremulted:
            buf *= a[..., None]
    return buf, a


def compose(buf, a, alpha_mode, force_flat=False, clamp=True):
    """
    Apply the alpha mode. Returns (float array, nchannels).

    `clamp` is off for scene-linear output, where compositing over white still
    means adding (1 - a) but the result must be allowed past 1.0 like every other
    value in the frame.
    """
    if a is None:
        return buf, 3
    if alpha_mode == "white":
        out = buf + (1.0 - a[..., None])
        return (np.clip(out, 0.0, 1.0) if clamp else out), 3
    if alpha_mode == "black" or force_flat:
        # buf is associated, so compositing over black is just dropping alpha
        return buf, 3
    return np.dstack([buf, a]), 4


def write_image(path, arr_uint, W, H, nchannels, fmt, quality=95,
                colorspace="sRGB"):
    spec = oiio.ImageSpec(W, H, nchannels, fmt)
    # Tags what is actually in the file. A scene-linear TIFF labelled sRGB would
    # be read back with a transfer function applied that was never there.
    spec.attribute("oiio:ColorSpace", colorspace)
    if path.lower().endswith((".jpg", ".jpeg")):
        spec.attribute("CompressionQuality", int(quality))
    if path.lower().endswith((".tif", ".tiff")):
        # zip beat lzw and packbits on real frames, and is lossless either way.
        spec.attribute("compression", "zip")
        # TIFF can only express sRGB (via the EXIF ColorSpace tag); "Linear" has
        # no representation and OIIO silently drops it. Absence of a tag is a
        # weak signal, so state it in ImageDescription, which always survives.
        spec.attribute("ImageDescription", "colorspace=%s" % colorspace)
    out = oiio.ImageOutput.create(path)
    if out is None:
        raise IOError("Could not create writer for %s: %s" % (path, oiio.geterror()))
    if not out.open(path, spec):
        raise IOError("Could not open %s for writing: %s" % (path, out.geterror()))
    if not out.write_image(np.ascontiguousarray(arr_uint)):
        err = out.geterror()
        out.close()
        raise IOError("Failed writing %s: %s" % (path, err))
    out.close()


EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "tiff": ".tif"}


def output_path_for(in_path, settings):
    out_dir = settings.get("out_dir") or os.path.dirname(in_path)
    base = os.path.splitext(os.path.basename(in_path))[0]
    ext = EXTENSIONS.get(settings["format"], ".png")
    return os.path.join(out_dir, base + settings.get("suffix", "") + ext)


def resolve_output(settings):
    """
    Work out the real pixel format, given what the container can carry.

    Kept separate from convert_one so the UI can ask the same question without
    converting anything, and so the constraints live in one place rather than
    being re-derived in JavaScript.
    """
    fmt = settings["format"]
    linear = settings.get("transfer") == "linear"
    bits = int(settings.get("bits", 8))

    if linear:
        # Scene-linear needs float, and only TIFF carries float here. Anything
        # else would quantise or clip the values the mode exists to preserve.
        return "tiff", "float", 32
    if fmt == "jpeg":
        return "jpeg", "uint8", 8
    if bits == 32:
        # 32-bit is float-only, and PNG has no float. Fall back rather than
        # writing something the container cannot represent.
        return ("tiff", "float", 32) if fmt == "tiff" else (fmt, "uint16", 16)
    return fmt, ("uint16" if bits == 16 else "uint8"), bits


def convert_one(in_path, settings):
    """Convert a single EXR. Returns (output path, info dict)."""
    rgb, alpha, W, H, layer, note = read_layer(in_path, settings.get("layer"))
    buf, a = apply_transform(rgb, alpha, W, H, settings)

    fmt, pix_fmt, bits = resolve_output(settings)
    linear = settings.get("transfer") == "linear"
    out_f, nch = compose(buf, a, settings["alpha_mode"],
                         force_flat=(fmt == "jpeg"), clamp=not linear)

    if pix_fmt == "float":
        arr_out = np.ascontiguousarray(out_f, dtype=np.float32)
    elif pix_fmt == "uint16":
        arr_out = (np.clip(out_f, 0.0, 1.0) * 65535.0 + 0.5).astype(np.uint16)
    else:
        arr_out = (np.clip(out_f, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    out_path = output_path_for(in_path, dict(settings, format=fmt))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_image(out_path, arr_out, W, H, nch, pix_fmt, settings.get("quality", 95),
                colorspace=("Linear" if linear else "sRGB"))
    return out_path, {"layer": layer, "note": note, "width": W, "height": H}


def default_workers():
    """
    How many frames to convert at once.

    OIIO and OCIO both release the GIL for the expensive parts - decoding and
    the transform - so threads genuinely overlap here. The cap exists because
    the work is memory-bandwidth bound well before it is core bound: every
    worker holds a full float frame, so a 4K RGBA plate is ~130 MB each.
    """
    return max(1, min(8, (os.cpu_count() or 4)))


def convert_many(paths, settings, workers=None, on_result=None, should_stop=None):
    """
    Convert many files, in parallel, preserving input order in the results.

    `on_result(index, path, out_path, info, error)` is called once per file, in
    submission order, from the calling thread - pool.map's iterator is consumed
    here, so callers do not need their own lock. `should_stop()` is polled at the
    start of each item so a cancel takes effect without draining the batch.

    Ordering matters for the log: completion order would read like a race.
    """
    from concurrent.futures import ThreadPoolExecutor

    paths = list(paths)
    if workers is None:
        workers = default_workers()
    workers = max(1, min(int(workers), len(paths) or 1))

    results = [None] * len(paths)

    def work(i_path):
        i, path = i_path
        if should_stop and should_stop():
            return i, None, None, None
        try:
            out, info = convert_one(path, settings)
            return i, out, info, None
        except Exception as e:  # reported per file; one bad frame is not fatal
            return i, None, None, e

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, out, info, err in pool.map(work, enumerate(paths)):
            results[i] = (out, info, err)
            if on_result:
                on_result(i, paths[i], out, info, err)
    return results


# ----------------------------------------------------------------------------
# Thumbnails
# ----------------------------------------------------------------------------

def _box_downsample(arr, factor):
    """Area-average by an integer factor. Crops the ragged edge."""
    if factor <= 1:
        return arr
    H, W = arr.shape[:2]
    h, w = (H // factor) * factor, (W // factor) * factor
    a = arr[:h, :w]
    shape = (h // factor, factor, w // factor, factor) + arr.shape[2:]
    return a.reshape(shape).mean(axis=(1, 3))


def make_thumbnail(path, settings, max_px=512):
    """
    Render a preview of what conversion would produce.

    Returns (data-uri str, info dict). Downsampling happens in linear before the
    display transform, so the preview is a true preview - same layer, same
    transform, same alpha handling as the real output.

    The exception is scene-linear output, which has no display transform by
    definition. Showing those values raw would be a near-black smear, so the
    preview stays display-referred and reports `preview_only=True`; it is then
    the UI's job to say the preview is not what gets written.
    """
    rgb, alpha, W, H, layer, note = read_layer(path, settings.get("layer"))
    linear = settings.get("transfer") == "linear"
    if linear:
        settings = dict(settings, transfer="display")

    # ceil, not floor: 1920 // 256 is 7, which leaves a 274px thumbnail above
    # the cap the caller asked for
    factor = max(1, -(-max(W, H) // max_px))
    if factor > 1:
        rgb = np.ascontiguousarray(_box_downsample(rgb, factor), dtype=np.float32)
        if alpha is not None:
            alpha = np.ascontiguousarray(_box_downsample(alpha, factor),
                                         dtype=np.float32)
        H, W = rgb.shape[:2]

    buf, a = apply_transform(rgb, alpha, W, H, settings)
    # preview always keeps alpha when present so the checkerboard shows through
    out_f, nch = compose(buf, a, settings.get("alpha_mode", "keep"))
    arr = (out_f * 255.0 + 0.5).astype(np.uint8)

    # OIIO's Python API has no in-memory encode, so round-trip a temp file
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        write_image(tmp_path, arr, W, H, nch, "uint8")
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    uri = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
    return uri, {"layer": layer, "note": note, "width": W, "height": H,
                 "preview_only": linear}


# ----------------------------------------------------------------------------
# Cryptomatte
#
# The manifest lives in EXR metadata as cryptomatte/<id>/{name,hash,conversion,
# manifest}, where the manifest is JSON mapping object names to MurmurHash3_32
# values in hex. Channels come in ID/coverage pairs: for a layer named
# "ViewLayer.CryptoObject", the channels are ViewLayer.CryptoObject00.r (an ID),
# .g (its coverage), .b (the next ID), .a (its coverage), continuing into
# ...01, ...02 for deeper ranks.
#
# Both the hash and the float conversion have to match the renderer bit for bit
# or no ID will ever compare equal. Verified against a Blender 5.2 render: all
# 25 manifest entries hashed identically, including one with a non-ASCII name.
# ----------------------------------------------------------------------------

_CRYPTO_RANK_RE = re.compile(r"^(?P<base>.+?)(?P<rank>\d{2})$")


def murmur3_32(key, seed=0):
    """MurmurHash3 x86 32-bit, as the Cryptomatte specification requires."""
    data = key.encode("utf-8")
    length = len(data)
    nblocks = length // 4
    h = seed
    c1, c2 = 0xCC9E2D51, 0x1B873593
    for i in range(nblocks):
        k = struct.unpack_from("<I", data, i * 4)[0]
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
        h = ((h << 13) | (h >> 19)) & 0xFFFFFFFF
        h = (h * 5 + 0xE6546B64) & 0xFFFFFFFF
    tail = data[nblocks * 4:]
    k = 0
    for i, ch in enumerate(tail):
        k |= ch << (8 * i)
    if tail:
        k = (k * c1) & 0xFFFFFFFF
        k = ((k << 15) | (k >> 17)) & 0xFFFFFFFF
        k = (k * c2) & 0xFFFFFFFF
        h ^= k
    h ^= length
    h ^= h >> 16
    h = (h * 0x85EBCA6B) & 0xFFFFFFFF
    h ^= h >> 13
    h = (h * 0xC2B2AE35) & 0xFFFFFFFF
    h ^= h >> 16
    return h


def hash_to_float(h):
    """
    The spec's uint32_to_float32: reinterpret the bits as float32.

    The exponent clamp matters. All-zero or all-one exponents are NaN/Inf/zero,
    none of which survive a float comparison, so those hashes get a bit flipped.
    """
    exponent = h >> 23 & 255
    if exponent in (0, 255):
        h ^= 1 << 23
    return struct.unpack("<f", struct.pack("<I", h))[0]


def name_to_id(name):
    return hash_to_float(murmur3_32(name))


def probe_cryptomattes(path):
    """
    Find the cryptomatte types in an EXR.

    Returns a list of dicts: {id, name, label, objects, ranks, incomplete}.
    `ranks` is a list of (id_channel, coverage_channel) index pairs. An entry
    with no usable ranks is reported with incomplete=True rather than dropped,
    so the UI can explain instead of silently offering nothing.
    """
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    # Cryptomatte usually lives in the first part, but a File Output node can
    # put it in any of them. Take the first part that actually carries the
    # metadata rather than assuming zero and reporting "none found".
    channels, meta, subimage = [], {}, 0
    try:
        sub = 0
        while src.seek_subimage(sub, 0):
            spec = src.spec()
            m = {a.name: a.value for a in spec.extra_attribs}
            if any(k.startswith("cryptomatte/") for k in m):
                channels = list(spec.channelnames)
                meta = m
                subimage = sub
                break
            sub += 1
    finally:
        src.close()

    blocks = {}
    for key, value in meta.items():
        if not key.startswith("cryptomatte/"):
            continue
        parts = key.split("/", 2)
        if len(parts) == 3:
            blocks.setdefault(parts[1], {})[parts[2]] = value

    # channel layer -> {component: index}
    by_layer = {}
    for i, chan in enumerate(channels):
        layer, comp = split_channel(chan)
        if comp:
            by_layer.setdefault(layer, {})[comp] = i

    out = []
    for cid, block in sorted(blocks.items()):
        name = block.get("name") or ""
        objects = {}
        raw = block.get("manifest")
        if raw:
            try:
                for obj, hexhash in json.loads(raw).items():
                    objects[obj] = hash_to_float(int(hexhash, 16))
            except (ValueError, TypeError):
                pass

        # rank layers are "<name>00", "<name>01", ... each holding two ranks
        ranks = []
        for layer in sorted(by_layer):
            m = _CRYPTO_RANK_RE.match(layer)
            if not m or m.group("base") != name:
                continue
            comps = by_layer[layer]
            if {"r", "g"} <= set(comps):
                ranks.append((comps["r"], comps["g"]))
            if {"b", "a"} <= set(comps):
                ranks.append((comps["b"], comps["a"]))

        out.append({
            "id": cid,
            "name": name,
            # "ViewLayer.CryptoObject" reads better as "CryptoObject"
            "label": name.rsplit(".", 1)[-1] if name else cid,
            "objects": objects,
            "ranks": ranks,
            # Which part the rank channels are in - the indices in `ranks` are
            # only meaningful against that part's channel list.
            "subimage": subimage,
            "incomplete": not ranks or not objects,
        })
    return out


def read_crypto_ranks(path, ranks, subimage=0):
    """
    Read the ID/coverage channel pairs for one cryptomatte type.

    `subimage` comes from probe_cryptomattes: the channel indices in `ranks`
    index that part's channel list, so reading them from part 0 would return
    whatever happened to sit at those positions there.
    """
    wanted = sorted({i for pair in ranks for i in pair})
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    try:
        if not src.seek_subimage(int(subimage), 0):
            raise IOError("part %s missing from %s" % (subimage, path))
        spec = src.spec()
        W, H = spec.width, spec.height
        lo, hi = wanted[0], wanted[-1] + 1
        pixels = src.read_image(int(subimage), 0, lo, hi, "float")
    finally:
        src.close()
    arr = np.array(pixels, dtype=np.float32).reshape(H, W, hi - lo)
    return [(arr[..., a - lo], arr[..., b - lo]) for a, b in ranks], W, H


def extract_matte(path, crypto, object_names):
    """
    Build a coverage matte for the named objects.

    Returns (coverage HxW float32 in 0..1, W, H). Coverage is summed across
    every rank whose ID matches, then clamped: a single rank can legitimately
    exceed 1.0 because the renderer's pixel filter accumulates (a Blender render
    reached 2.633), so the clamp is required, not defensive.
    """
    targets = [crypto["objects"][n] for n in object_names
               if n in crypto["objects"]]
    if not targets:
        raise ValueError("none of those objects are in this cryptomatte")

    pairs, W, H = read_crypto_ranks(path, crypto["ranks"], crypto.get("subimage", 0))
    cov = np.zeros((H, W), dtype=np.float32)
    for ids, weights in pairs:
        hit = np.zeros((H, W), dtype=bool)
        for t in targets:
            hit |= (ids == t)
        cov += np.where(hit, weights, 0.0)
    return np.clip(cov, 0.0, 1.0), W, H


def resolve_matte_output(settings):
    """
    Container and depth for a matte.

    "straight" (RGB pinned to 1.0) only survives in TIFF. OIIO's PNG writer
    always associates alpha, so RGB 1.0 comes back as the coverage and the two
    modes collapse into the same file - measured, not assumed. Rather than write
    something that silently is not what was asked for, straight upgrades to TIFF
    the same way scene-linear does.
    """
    fmt = settings.get("format", "png")
    if fmt == "jpeg":
        fmt = "png"  # a matte without alpha is not a matte
    bits = int(settings.get("bits", 16))
    if settings.get("matte_mode", "associated") == "straight":
        fmt = "tiff"
    if bits == 32 and fmt != "tiff":
        bits = 16
    pix_fmt = "float" if bits == 32 else ("uint16" if bits == 16 else "uint8")
    return fmt, pix_fmt, bits


def id_colour(float_id):
    """
    A stable pseudo-random colour per ID, the way Nuke and AE show cryptomattes.

    The hash is already well distributed, so its bits are reused directly as
    hue/saturation/value rather than hashed again. Saturation and value are kept
    in the top of their range so neighbouring objects stay distinguishable and
    nothing lands on near-black.
    """
    if float_id == 0.0:
        return (0.0, 0.0, 0.0)
    h = struct.unpack("<I", struct.pack("<f", np.float32(float_id)))[0]
    hue = (h & 0xFFFF) / 65535.0
    sat = 0.55 + ((h >> 16) & 0xFF) / 255.0 * 0.45
    val = 0.65 + ((h >> 24) & 0xFF) / 255.0 * 0.35

    i = int(hue * 6.0) % 6
    f = hue * 6.0 - int(hue * 6.0)
    p, q, t = val * (1 - sat), val * (1 - f * sat), val * (1 - (1 - f) * sat)
    return [(val, t, p), (q, val, p), (p, val, t),
            (p, q, val), (t, p, val), (val, p, q)][i]


def _subsample(arr, factor):
    """
    Nearest-neighbour, never averaged.

    Averaging two IDs produces a third that matches no object in the manifest,
    so ID planes must be sampled, not filtered. Coverage is sampled alongside
    them so the two stay aligned.
    """
    return arr[::factor, ::factor] if factor > 1 else arr


def crypto_preview(path, crypto, max_px=512, selected=(), dim_unselected=True):
    """
    Render the cryptomatte as coloured IDs, and return what a click needs.

    Returns (data-uri, state) where state carries the subsampled top-rank ID
    plane so a later click resolves to an object without re-reading the file.

    No display transform: these are IDs, not colour.
    """
    pairs, W, H = read_crypto_ranks(path, crypto["ranks"], crypto.get("subimage", 0))
    factor = max(1, -(-max(W, H) // max_px))
    ids0 = np.ascontiguousarray(_subsample(pairs[0][0], factor))
    h, w = ids0.shape

    rgb = np.zeros((h, w, 3), dtype=np.float32)
    total = np.zeros((h, w), dtype=np.float32)
    lookup = {}
    for name, fid in crypto["objects"].items():
        lookup[fid] = name
    sel_ids = {crypto["objects"][n] for n in selected if n in crypto["objects"]}

    # Composite every rank so edges blend between the objects that share them,
    # which is what makes the picture readable rather than aliased.
    for ids, cov in pairs:
        ids_s = _subsample(ids, factor)
        cov_s = np.clip(_subsample(cov, factor), 0.0, 1.0)
        for fid in np.unique(ids_s):
            if fid == 0.0:
                continue
            mask = ids_s == fid
            if not mask.any():
                continue
            c = id_colour(float(fid))
            if dim_unselected and sel_ids and fid not in sel_ids:
                c = tuple(v * 0.18 for v in c)
            weight = np.where(mask, cov_s, 0.0)
            rgb += weight[..., None] * np.array(c, dtype=np.float32)
            total += weight

    alpha = np.clip(total, 0.0, 1.0)
    arr = (np.clip(np.dstack([rgb, alpha]), 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)

    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        write_image(tmp_path, arr, w, h, 4, "uint8", colorspace="Linear")
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    state = {"ids": ids0, "width": w, "height": h,
             "full_width": W, "full_height": H, "lookup": lookup}
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), state


def object_at(state, u, v):
    """
    Resolve a normalised click (0..1) to an object name, or None.

    Works off the subsampled plane the preview was built from, so picking costs
    nothing beyond an array index - the file is not touched again.
    """
    if not state:
        return None
    x = min(state["width"] - 1, max(0, int(u * state["width"])))
    y = min(state["height"] - 1, max(0, int(v * state["height"])))
    return state["lookup"].get(float(state["ids"][y, x]))


def matte_filename(in_path, crypto, object_name, settings, index=None):
    """A filesystem-safe name per matte, since object names are arbitrary."""
    out_dir = settings.get("out_dir") or os.path.dirname(in_path)
    base = os.path.splitext(os.path.basename(in_path))[0]
    # object names come from the DCC and may hold spaces, dots or emoji
    safe = re.sub(r"[^\w.\-]+", "_", object_name).strip("_") or "matte"
    if index is not None:
        safe = "%s_%s" % (crypto["label"], safe)
    fmt, _, _ = resolve_matte_output(settings)
    return os.path.join(out_dir, "%s_%s%s" % (base, safe, EXTENSIONS[fmt]))


def write_matte(out_path, cov, W, H, settings):
    """
    Write a coverage matte as a white silhouette with alpha.

    No display transform: coverage is data, not colour, and running it through
    an ACES view would be as wrong as tone-mapping a normal pass.

    Default is associated - coverage in RGB as well as alpha. A fully covered
    pixel is therefore white and a partial edge is correctly premultiplied
    white, which is what "flat white with alpha" means once edges are handled
    properly. "straight" pins RGB to 1.0 and leaves the shape only in alpha;
    see resolve_matte_output for why that needs TIFF.
    """
    fmt, pix_fmt, bits = resolve_matte_output(settings)
    if settings.get("matte_mode", "associated") == "straight":
        rgb = np.ones((H, W, 3), dtype=np.float32)
    else:
        rgb = np.repeat(cov[..., None], 3, axis=2)
    out_f = np.dstack([rgb, cov])

    if pix_fmt == "float":
        arr = out_f.astype(np.float32)
    elif pix_fmt == "uint16":
        arr = (out_f * 65535.0 + 0.5).astype(np.uint16)
    else:
        arr = (out_f * 255.0 + 0.5).astype(np.uint8)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_image(out_path, arr, W, H, 4, pix_fmt, colorspace="Linear")
    return out_path


def convert_mattes(in_path, settings, crypto_id, object_names):
    """
    Export mattes for the selected objects. Returns a list of output paths.

    Combined writes one file for the union of the selection; otherwise each
    object gets its own, which is what usually goes into a comp.
    """
    cryptos = {c["id"]: c for c in probe_cryptomattes(in_path)}
    crypto = cryptos.get(crypto_id)
    if crypto is None:
        raise ValueError("no cryptomatte %r in this file" % crypto_id)
    if crypto["incomplete"]:
        raise ValueError("cryptomatte %r is unusable: %s"
                         % (crypto["label"],
                            "no manifest" if not crypto["objects"]
                            else "no rank channels"))

    # read the ranks once no matter how many mattes come out of them
    pairs, W, H = read_crypto_ranks(in_path, crypto["ranks"], crypto.get("subimage", 0))

    def coverage(names):
        targets = [crypto["objects"][n] for n in names if n in crypto["objects"]]
        cov = np.zeros((H, W), dtype=np.float32)
        for ids, weights in pairs:
            hit = np.zeros((H, W), dtype=bool)
            for t in targets:
                hit |= (ids == t)
            cov += np.where(hit, weights, 0.0)
        return np.clip(cov, 0.0, 1.0)

    written = []
    if settings.get("matte_combine"):
        out = matte_filename(in_path, crypto, "combined", settings, index=0)
        written.append(write_matte(out, coverage(object_names), W, H, settings))
    else:
        for i, name in enumerate(object_names):
            out = matte_filename(in_path, crypto, name, settings, index=i)
            written.append(write_matte(out, coverage([name]), W, H, settings))
    return written


# ----------------------------------------------------------------------------
# Viewer
#
# The converter is a throughput problem; a viewer is a latency one. Measured on
# this machine, one preview frame costs 125 ms at 1080p and 860 ms for a 2160
# square 80-channel file - and dropping the preview resolution barely moves
# either, because the cost is reading and decoding the EXR rather than the
# transform.
#
# So the only way to make exposure and view changes interactive is to stop
# re-reading. ViewerSession decodes a layer once and keeps it; everything after
# that is arithmetic on an array already in memory.
# ----------------------------------------------------------------------------

CHANNEL_MODES = ("rgb", "r", "g", "b", "a", "luma")


class ViewerSession:
    """
    Holds one decoded layer so display changes do not re-read the file.

    Not thread-safe by itself: the UI drives it from one worker at a time.
    """

    def __init__(self):
        self._key = None
        self._rgb = None
        self._alpha = None
        self._W = self._H = 0
        self._layer = None
        self._note = None
        # Downsampling the full-resolution layer costs 82 ms on a 2160 square
        # frame and the source never changes between renders, so the scaled
        # copy is kept per output size.
        self._scaled = {}

    @property
    def size(self):
        return self._W, self._H

    @property
    def layer(self):
        return self._layer

    def load(self, path, layer=None):
        """Decode if this is not already the layer in hand. Returns True if read."""
        key = (path, layer, os.path.getmtime(path))
        if key == self._key:
            return False
        rgb, alpha, W, H, chosen, note = read_layer(path, layer)
        self._key = key
        self._rgb, self._alpha = rgb, alpha
        self._W, self._H = W, H
        self._layer, self._note = chosen, note
        self._scaled.clear()
        return True

    def _at_size(self, max_px):
        """The layer scaled for this output size, computed once per size."""
        factor = max(1, -(-max(self._W, self._H) // max_px))
        if factor not in self._scaled:
            if factor > 1:
                rgb = np.ascontiguousarray(
                    _box_downsample(self._rgb, factor), dtype=np.float32)
                alpha = (np.ascontiguousarray(
                    _box_downsample(self._alpha, factor), dtype=np.float32)
                    if self._alpha is not None else None)
            else:
                rgb, alpha = self._rgb, self._alpha
            self._scaled[factor] = (rgb, alpha)
        return self._scaled[factor]

    def _crop(self, box):
        """
        A region of the full-resolution layer, clamped to the image.

        Deliberately not cached: crops follow the viewport, so every pan would
        add another entry and the cache would grow without bound. The slice is a
        view rather than a copy, and `render` copies before touching it anyway.
        """
        x, y, w, h = (int(v) for v in box)
        x = max(0, min(x, self._W - 1))
        y = max(0, min(y, self._H - 1))
        w = max(1, min(w, self._W - x))
        h = max(1, min(h, self._H - y))
        rgb = self._rgb[y:y + h, x:x + w]
        alpha = self._alpha[y:y + h, x:x + w] if self._alpha is not None else None
        return rgb, alpha

    def render(self, settings, exposure=0.0, gamma=1.0, channel="rgb",
               max_px=1024, crop=None):
        """
        Apply exposure, the display transform and gamma, and return a data URI.

        Exposure is applied in linear *before* the transform, which is what makes
        it behave like a camera stop rather than a brightness slider. Gamma is
        applied after, on display values, matching how a compositor's viewer
        gamma works.

        `crop` is `(x, y, w, h)` in source pixels. Given one, the region is taken
        from the full-resolution layer and no downsampling happens at all - which
        is the only way zooming past 1:1 shows real pixels rather than an
        upscaled preview. Without it the whole image is scaled to `max_px`.
        """
        if self._rgb is None:
            raise RuntimeError("nothing loaded")

        if crop is not None:
            rgb, alpha = self._crop(crop)
        else:
            rgb, alpha = self._at_size(max_px)
        return self._encode(rgb, alpha, settings, exposure, gamma, channel)

    def _encode(self, rgb, alpha, settings, exposure, gamma, channel):
        """
        Exposure, display transform, gamma, channel isolation -> data URI.

        Split out so the difference view goes through exactly the same chain as
        a normal render; two encode paths would drift and the comparison would
        be measuring the drift rather than the images.
        """
        h, w = rgb.shape[:2]

        # apply_transform writes in place, so the cached copy must not be handed
        # to it directly. Exposure already produces a new array when non-zero.
        rgb = (rgb * np.float32(2.0 ** exposure)) if exposure else rgb.copy()

        if channel == "a":
            # Alpha is not colour; show it as it is, with no transform at all.
            a = (alpha if alpha is not None
                 else np.ones((h, w), dtype=np.float32))
            buf = np.repeat(np.clip(a, 0, 1)[..., None], 3, axis=2)
        else:
            buf, _ = apply_transform(rgb, None, w, h, settings)
            if channel in ("r", "g", "b"):
                i = "rgb".index(channel)
                buf = np.repeat(buf[..., i:i + 1], 3, axis=2)
            elif channel == "luma":
                y = (0.2126 * buf[..., 0] + 0.7152 * buf[..., 1]
                     + 0.0722 * buf[..., 2])
                buf = np.repeat(y[..., None], 3, axis=2)

        if gamma and gamma != 1.0:
            buf = np.power(np.clip(buf, 0.0, 1.0), 1.0 / float(gamma))

        out = np.clip(buf, 0.0, 1.0)
        if channel == "rgb" and alpha is not None:
            out = np.dstack([out, np.clip(alpha, 0, 1)])
        nch = out.shape[2]
        arr = (out * 255.0 + 0.5).astype(np.uint8)
        return _png_data_uri(arr, w, h, nch), w, h

    def sample(self, u, v, settings=None, exposure=0.0, gamma=1.0):
        """
        Values under a normalised coordinate.

        Linear values come from the full-resolution layer, not the preview, so
        they are the pixel's real values rather than a resampled approximation.

        When `settings` is given the same pixel is also pushed through the
        display chain, which is what a hex code has to mean: the colour on
        screen. A hex of the linear value would read as near-black for anything
        normally exposed and would match nothing anyone could paste elsewhere.
        """
        if self._rgb is None:
            return None
        x = min(self._W - 1, max(0, int(u * self._W)))
        y = min(self._H - 1, max(0, int(v * self._H)))
        px = self._rgb[y, x]
        out = {
            "x": x, "y": y,
            "r": float(px[0]), "g": float(px[1]), "b": float(px[2]),
            "a": (float(self._alpha[y, x]) if self._alpha is not None else None),
        }
        if settings is not None:
            one = np.array(px, dtype=np.float32).reshape(1, 1, 3).copy()
            if exposure:
                one *= np.float32(2.0 ** exposure)
            disp, _ = apply_transform(one, None, 1, 1, settings)
            if gamma and gamma != 1.0:
                disp = np.power(np.clip(disp, 0.0, 1.0), 1.0 / float(gamma))
            rgb8 = (np.clip(disp, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)[0, 0]
            out["dr"], out["dg"], out["db"] = (int(rgb8[0]), int(rgb8[1]),
                                               int(rgb8[2]))
            out["hex"] = "#%02X%02X%02X" % (rgb8[0], rgb8[1], rgb8[2])
        return out


def render_difference(a, b, settings, exposure=0.0, gamma=1.0, max_px=1024):
    """
    |A - B| in linear, through the same display chain as a normal render.

    Difference is taken in **linear**, not on display values: the tone curve is
    steep in the shadows and shallow in the highlights, so a difference measured
    after it would exaggerate dark mismatches and hide bright ones. Taken before,
    a difference of 0.01 means the same thing wherever it happens - and exposure
    then works as the gain control for reading small ones, which is what it
    already is everywhere else in this app.

    Returns (uri, w, h). Raises ValueError if the two are different sizes, since
    a resized comparison would be measuring the resampler.
    """
    if a.size != b.size:
        raise ValueError("different sizes: %dx%d and %dx%d"
                         % (a.size[0], a.size[1], b.size[0], b.size[1]))
    rgb_a, alpha_a = a._at_size(max_px)
    rgb_b, _ = b._at_size(max_px)
    if rgb_a.shape != rgb_b.shape:
        raise ValueError("layers do not line up")
    diff = np.abs(rgb_a - rgb_b)
    # Alpha carried from A so the surround still reads as transparent rather
    # than as a region that happens to match.
    return a._encode(diff, alpha_a, settings, exposure, gamma, "rgb")


def _png_data_uri(arr, W, H, nchannels):
    """OIIO's Python API has no in-memory encode, so round-trip a temp file."""
    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        write_image(tmp_path, arr, W, H, nchannels, "uint8")
        with open(tmp_path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# ----------------------------------------------------------------------------
# Image sequences
#
# A render is usually 120 files that differ by a frame number. Listing them
# individually buries everything else, so runs are collapsed into one row.
# ----------------------------------------------------------------------------

_FRAME_RE = re.compile(r"^(?P<stem>.*?)(?P<frame>\d+)$")


def group_sequences(paths, min_frames=2):
    """
    Collapse frame runs into sequence entries.

    Returns a list of dicts, input order preserved by first appearance:
      {kind: 'single',   path, label, count: 1}
      {kind: 'sequence', paths, label, count, first, last, pattern}
    """
    buckets = {}
    order = []
    for p in paths:
        directory, fname = os.path.split(p)
        stem_ext, ext = os.path.splitext(fname)
        m = _FRAME_RE.match(stem_ext)
        if m and m.group("frame"):
            key = (directory, m.group("stem"), len(m.group("frame")), ext.lower())
            frame = int(m.group("frame"))
        else:
            key = (directory, stem_ext, -1, ext.lower())
            frame = None
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append((frame, p))

    entries = []
    for key in order:
        directory, stem, pad, ext = key
        items = buckets[key]
        if pad < 0 or len(items) < min_frames:
            for _, p in items:
                entries.append({"kind": "single", "path": p, "count": 1,
                                "label": os.path.basename(p), "dir": directory})
            continue
        items.sort(key=lambda t: t[0])
        frames = [f for f, _ in items]
        pattern = "%s%s%s" % (stem, "#" * pad, ext)
        entries.append({
            "kind": "sequence",
            "paths": [p for _, p in items],
            "count": len(items),
            "first": frames[0],
            "last": frames[-1],
            "label": pattern,
            "dir": directory,
        })
    return entries


def expand_entries(entries):
    """Flatten sequence entries back into a plain file list."""
    out = []
    for e in entries:
        out.extend(e["paths"] if e["kind"] == "sequence" else [e["path"]])
    return out


def find_exrs(root, recurse=True):
    """Collect .exr files under a folder."""
    found = []
    if recurse:
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".exr"):
                    found.append(os.path.join(dirpath, f))
    else:
        for f in os.listdir(root):
            p = os.path.join(root, f)
            if f.lower().endswith(".exr") and os.path.isfile(p):
                found.append(p)
    return sorted(found)
