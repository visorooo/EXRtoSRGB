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
import base64
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
    """Group channel names into {layer: {component: index}}, order preserved."""
    layers = {}
    for i, name in enumerate(channelnames):
        layer, comp = split_channel(name)
        if comp is None:
            continue
        layers.setdefault(layer, {}).setdefault(comp, i)
    return {k: v for k, v in layers.items() if {"r", "g", "b"} <= set(v)}


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


def probe_layers(path):
    """Return the convertible layer names in an EXR, best-guess first."""
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    names = list(src.spec().channelnames)
    src.close()
    return sorted(group_layers(names), key=lambda k: (-score_layer(k), k))


def read_layer(path, requested=None):
    """
    Read one layer out of an EXR.

    Returns (rgb float32 HxWx3, alpha float32 HxW or None, W, H, layer, note).
    Only the channels needed are read - a 60-channel Blender AOV dump is
    otherwise half a gigabyte of float for three channels of output.
    """
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    try:
        spec = src.spec()
        W, H = spec.width, spec.height
        layer, comps, note = pick_layer(list(spec.channelnames), requested)
        wanted = [comps["r"], comps["g"], comps["b"]]
        has_alpha = "a" in comps
        if has_alpha:
            wanted.append(comps["a"])
        lo, hi = min(wanted), max(wanted) + 1
        pixels = src.read_image(0, 0, lo, hi, "float")
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
    """
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


def compose(buf, a, alpha_mode, force_flat=False):
    """Apply the alpha mode. Returns (float array, nchannels)."""
    if a is None:
        return buf, 3
    if alpha_mode == "white":
        return np.clip(buf + (1.0 - a[..., None]), 0.0, 1.0), 3
    if alpha_mode == "black" or force_flat:
        # buf is associated, so compositing over black is just dropping alpha
        return buf, 3
    return np.dstack([buf, a]), 4


def write_image(path, arr_uint, W, H, nchannels, fmt, quality=95):
    spec = oiio.ImageSpec(W, H, nchannels, fmt)
    spec.attribute("oiio:ColorSpace", "sRGB")
    if path.lower().endswith((".jpg", ".jpeg")):
        spec.attribute("CompressionQuality", int(quality))
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


def output_path_for(in_path, settings):
    out_dir = settings.get("out_dir") or os.path.dirname(in_path)
    base = os.path.splitext(os.path.basename(in_path))[0]
    ext = ".jpg" if settings["format"] == "jpeg" else ".png"
    return os.path.join(out_dir, base + settings.get("suffix", "") + ext)


def convert_one(in_path, settings):
    """Convert a single EXR. Returns (output path, info dict)."""
    rgb, alpha, W, H, layer, note = read_layer(in_path, settings.get("layer"))
    buf, a = apply_transform(rgb, alpha, W, H, settings)

    is_jpeg = settings["format"] == "jpeg"
    out_f, nch = compose(buf, a, settings["alpha_mode"], force_flat=is_jpeg)

    if settings["bits"] == 16 and not is_jpeg:
        arr_uint = (out_f * 65535.0 + 0.5).astype(np.uint16)
        pix_fmt = "uint16"
    else:
        arr_uint = (out_f * 255.0 + 0.5).astype(np.uint8)
        pix_fmt = "uint8"

    out_path = output_path_for(in_path, settings)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    write_image(out_path, arr_uint, W, H, nch, pix_fmt, settings.get("quality", 95))
    return out_path, {"layer": layer, "note": note, "width": W, "height": H}


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
    """
    rgb, alpha, W, H, layer, note = read_layer(path, settings.get("layer"))

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
    return uri, {"layer": layer, "note": note, "width": W, "height": H}


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
