#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regression tests for the conversion core.

Both bugs that shipped in v1.0 - converting the Ambient Occlusion pass instead
of the beauty, and dropping the re-premultiply after the display transform -
would have been caught here. The Nuke comparisons are the ones that matter;
everything else guards an invariant that has already been violated once.

    pip install pytest
    pytest -q
"""

import os
import sys

import numpy as np
import pytest
import OpenImageIO as oiio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.join(os.path.dirname(HERE), "exrs_tests")

BLENDER = os.path.join(TESTS, "Blender_Cycles", "B3D_cycles_beauty_demo")
REDSHIFT = os.path.join(TESTS, "C4D_Redshift", "C4D_redshift_beauty_demo")
EXTRA = os.path.join(TESTS, "C4D_Redshift", "extra_demo", "extra_demo")

needs_fixtures = pytest.mark.skipif(
    not os.path.isdir(TESTS), reason="exrs_tests/ not present")

ACES13 = core.ACES_CONFIGS["ACES 1.3 · CG v2.2  (recommended)"]
ACES20 = core.ACES_CONFIGS["ACES 2.0 · CG v4.0  (newest)"]


def settings(out_dir, config=ACES13, **kw):
    display = core.default_display(config)
    s = {
        "config": config,
        "src": "ACEScg",
        "display": display,
        "view": core.view_for(config, display, True),
        "format": "png",
        "quality": 95,
        "bits": 8,
        "alpha_mode": "keep",
        "layer": None,
        "unpremult": True,
        "out_dir": str(out_dir),
        "suffix": "",
    }
    s.update(kw)
    return s


def read_u8(path):
    src = oiio.ImageInput.open(path)
    assert src is not None, "could not open %s" % path
    spec = src.spec()
    arr = np.array(src.read_image(format="uint8"), dtype=np.uint8)
    src.close()
    return arr.reshape(spec.height, spec.width, spec.nchannels)


def mean_err(a, b):
    n = min(a.shape[2], b.shape[2])
    return float(np.abs(a[..., :n].astype(np.int16)
                        - b[..., :n].astype(np.int16)).mean())


# ---------------------------------------------------------------------------
# Colour correctness - the documented ACES ladder
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def patch_exr(tmp_path_factory):
    """A 3-pixel ACEScg patch at 0.18 / 1.0 / 4.0."""
    d = tmp_path_factory.mktemp("patch")
    path = str(d / "patch.exr")
    arr = np.zeros((1, 3, 3), dtype=np.float32)
    for i, v in enumerate((0.18, 1.0, 4.0)):
        arr[0, i, :] = v
    out = oiio.ImageOutput.create(path)
    out.open(path, oiio.ImageSpec(3, 1, 3, "float"))
    out.write_image(arr)
    out.close()
    return path


@pytest.mark.parametrize("config,expected", [
    (ACES13, [91, 207, 244]),
    (ACES20, [89, 180, 229]),
])
def test_aces_ladder(patch_exr, tmp_path, config, expected):
    """The values README and CLAUDE.md both publish as correct ACES output.

    If 1.0 lands on 255 the tone curve is not being applied at all - most
    likely the view resolved to a plain transfer function.
    """
    out, _ = core.convert_one(patch_exr, settings(tmp_path, config=config,
                                                  suffix="_" + config[:8]))
    px = read_u8(out)
    assert [int(px[0, i, 0]) for i in range(3)] == expected


def test_untonemapped_differs(patch_exr, tmp_path):
    """Un-tone-mapped must not silently resolve to the same view."""
    display = core.default_display(ACES13)
    plain = settings(tmp_path, view=core.view_for(ACES13, display, False),
                     suffix="_plain")
    out, _ = core.convert_one(patch_exr, plain)
    px = read_u8(out)
    assert int(px[0, 1, 0]) > 244, "1.0 should clip high without the ACES curve"


# ---------------------------------------------------------------------------
# Layer resolution
# ---------------------------------------------------------------------------

def test_beauty_outranks_alphabetically_first_aov():
    """The v1.0 bug: Ambient_Occlusion sorts first and used to win."""
    names = []
    for layer in ("Ambient_Occlusion", "Beauty_Denoised", "Diffuse_Color"):
        names += ["%s.%s" % (layer, c) for c in "RGBA"]
    layer, comps, note = core.pick_layer(names)
    assert layer == "Beauty_Denoised"
    assert note is None


def test_bare_rgb_outranks_named_layers():
    names = ["R", "G", "B", "A", "Beauty.R", "Beauty.G", "Beauty.B"]
    assert core.pick_layer(names)[0] == ""


def test_redshift_lowercase_components():
    """Redshift writes red/green/blue, not R/G/B."""
    names = ["Beauty.red", "Beauty.green", "Beauty.blue", "Beauty.alpha"]
    layer, comps, _ = core.pick_layer(names)
    assert layer == "Beauty"
    assert set(comps) == {"r", "g", "b", "a"}


@pytest.mark.parametrize("layer", [
    "CryptoObject", "N", "ObjectPosition", "Depth", "MotionVector", "UV",
])
def test_data_layers_never_auto_picked(layer):
    assert core.score_layer(layer) < 0


def test_denoised_beauty_preferred():
    assert core.score_layer("Beauty_Denoised") > core.score_layer("Beauty")


def test_missing_requested_layer_falls_back_with_note():
    names = ["Beauty.R", "Beauty.G", "Beauty.B"]
    layer, _, note = core.pick_layer(names, requested="NoSuchLayer")
    assert layer == "Beauty"
    assert note and "NoSuchLayer" in note


def test_no_rgb_raises():
    """The old code fell back to channels 0,1,2 and converted anything."""
    with pytest.raises(ValueError):
        core.pick_layer(["Z", "CryptoObject00.R", "CryptoObject00.G"])


def test_ambiguous_pick_warns():
    names = ["Diffuse_Color.R", "Diffuse_Color.G", "Diffuse_Color.B"]
    _, _, note = core.pick_layer(names)
    assert note and "beauty" in note


# ---------------------------------------------------------------------------
# Alpha association
# ---------------------------------------------------------------------------

def _synthetic_alpha_exr(path, cov=0.5):
    """One pixel of 50% coverage, associated: rgb already multiplied by alpha."""
    arr = np.zeros((1, 1, 4), dtype=np.float32)
    arr[0, 0, :3] = 0.18 * cov
    arr[0, 0, 3] = cov
    out = oiio.ImageOutput.create(path)
    out.open(path, oiio.ImageSpec(1, 1, 4, "float"))
    out.write_image(arr)
    out.close()
    return path


def test_output_stays_associated(tmp_path):
    """
    The v1.0 alpha bug: un-premultiply happened, re-premultiply did not, so
    edge pixels came out roughly twice as bright as they should.
    """
    src = _synthetic_alpha_exr(str(tmp_path / "half.exr"), cov=0.5)
    out, _ = core.convert_one(src, settings(tmp_path))
    px = read_u8(out)
    rgb, a = int(px[0, 0, 0]), int(px[0, 0, 3])
    assert a == 128, "alpha should survive untouched"
    # 0.18 through the ACES curve is 91; associated at 50% coverage is ~45
    assert abs(rgb - 45) <= 2, "expected premultiplied ~45, got %d" % rgb


def test_unpremult_pair_is_symmetric(tmp_path):
    """Fully opaque pixels must be identical whether un-premult is on or off."""
    src = _synthetic_alpha_exr(str(tmp_path / "opaque.exr"), cov=1.0)
    on, _ = core.convert_one(src, settings(tmp_path, unpremult=True, suffix="_on"))
    off, _ = core.convert_one(src, settings(tmp_path, unpremult=False, suffix="_off"))
    assert np.array_equal(read_u8(on), read_u8(off))


def test_flatten_on_white(tmp_path):
    src = _synthetic_alpha_exr(str(tmp_path / "half.exr"), cov=0.5)
    out, _ = core.convert_one(src, settings(tmp_path, alpha_mode="white"))
    px = read_u8(out)
    assert px.shape[2] == 3
    assert int(px[0, 0, 0]) > 128, "half-covered pixel over white must be bright"


# ---------------------------------------------------------------------------
# Output format
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt,bits,mode,ext,nch,pixfmt", [
    ("png", 8, "keep", ".png", 4, "uint8"),
    ("png", 16, "keep", ".png", 4, "uint16"),
    ("png", 8, "black", ".png", 3, "uint8"),
    ("jpeg", 8, "keep", ".jpg", 3, "uint8"),
])
def test_output_shape(tmp_path, fmt, bits, mode, ext, nch, pixfmt):
    src = _synthetic_alpha_exr(str(tmp_path / "a.exr"))
    out, _ = core.convert_one(src, settings(
        tmp_path, format=fmt, bits=bits, alpha_mode=mode,
        suffix="_%s%d%s" % (fmt, bits, mode)))
    assert out.endswith(ext)
    src_in = oiio.ImageInput.open(out)
    spec = src_in.spec()
    src_in.close()
    assert spec.nchannels == nch
    assert str(spec.format) == pixfmt


def test_jpeg_ignores_16bit(tmp_path):
    """JPEG is 8-bit; asking for 16 must not produce a broken file."""
    src = _synthetic_alpha_exr(str(tmp_path / "a.exr"))
    out, _ = core.convert_one(src, settings(tmp_path, format="jpeg", bits=16))
    src_in = oiio.ImageInput.open(out)
    assert str(src_in.spec().format) == "uint8"
    src_in.close()


# ---------------------------------------------------------------------------
# OCIO config registry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,name", list(core.ACES_CONFIGS.items()))
def test_every_offered_config_loads(label, name):
    """A config in the dropdown that fails to load is a broken dropdown."""
    cfg = core.get_config(name)
    display = core.default_display(name)
    assert display
    assert core.view_for(name, display, True)
    assert core.list_input_spaces(name)


def test_after_effects_configs_present():
    """The two AE entries that exist as OCIO built-ins. ACES 1.2 does not."""
    names = set(core.ACES_CONFIGS.values())
    assert "cg-config-v1.0.0_aces-v1.3_ocio-v2.1" in names
    assert "studio-config-v1.0.0_aces-v1.3_ocio-v2.1" in names


def test_config_versions_actually_differ(patch_exr, tmp_path):
    """Guard against every label silently resolving to the same transform."""
    seen = {}
    for label, name in core.ACES_CONFIGS.items():
        out, _ = core.convert_one(
            patch_exr, settings(tmp_path, config=name,
                                suffix="_" + str(abs(hash(name)) % 10000)))
        seen[label] = tuple(int(v) for v in read_u8(out)[0, :, 0])
    assert len(set(seen.values())) > 1, "all configs produced identical output"


# ---------------------------------------------------------------------------
# Sequence grouping
# ---------------------------------------------------------------------------

def test_sequence_collapses():
    paths = [r"C:\r\beauty.%04d.exr" % i for i in range(1, 121)]
    entries = core.group_sequences(paths)
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "sequence"
    assert e["count"] == 120
    assert (e["first"], e["last"]) == (1, 120)
    assert e["label"] == "beauty.####.exr"


def test_single_file_not_collapsed():
    entries = core.group_sequences([r"C:\r\beauty.0001.exr"])
    assert entries[0]["kind"] == "single"


def test_distinct_sequences_stay_separate():
    paths = ([r"C:\r\beauty.%04d.exr" % i for i in (1, 2)]
             + [r"C:\r\shadow.%04d.exr" % i for i in (1, 2)])
    entries = core.group_sequences(paths)
    assert len(entries) == 2
    assert {e["label"] for e in entries} == {"beauty.####.exr", "shadow.####.exr"}


def test_same_name_different_folders_stay_separate():
    paths = [r"C:\a\f.%04d.exr" % i for i in (1, 2)] + \
            [r"C:\b\f.%04d.exr" % i for i in (1, 2)]
    assert len(core.group_sequences(paths)) == 2


def test_padding_difference_stays_separate():
    paths = [r"C:\r\f.%04d.exr" % i for i in (1, 2)] + \
            [r"C:\r\f.%06d.exr" % i for i in (1, 2)]
    assert len(core.group_sequences(paths)) == 2


def test_expand_roundtrip():
    paths = [r"C:\r\beauty.%04d.exr" % i for i in range(1, 11)]
    assert core.expand_entries(core.group_sequences(paths)) == paths


def test_unnumbered_file_survives():
    entries = core.group_sequences([r"C:\r\still.exr"])
    assert entries[0]["kind"] == "single"
    assert entries[0]["label"] == "still.exr"


# ---------------------------------------------------------------------------
# The real renders, against Nuke
# ---------------------------------------------------------------------------

@needs_fixtures
@pytest.mark.parametrize("stem,expect_layer,limit", [
    (BLENDER, "Beauty_Denoised", 2.0),
    (REDSHIFT, "", 0.5),
    (EXTRA, "", 0.2),
])
def test_matches_nuke_reference(tmp_path, stem, expect_layer, limit):
    out, info = core.convert_one(stem + ".exr", settings(tmp_path))
    assert info["layer"] == expect_layer
    assert mean_err(read_u8(out), read_u8(stem + ".png")) < limit


@needs_fixtures
def test_blender_beauty_is_not_grey():
    """
    The AO pass is achromatic; the beauty is not. This is the shape of the v1.0
    bug independent of any reference image.
    """
    rgb, _, _, _, layer, _ = core.read_layer(BLENDER + ".exr")
    assert layer == "Beauty_Denoised"
    spread = float(np.abs(rgb[..., 0].mean() - rgb[..., 2].mean()))
    assert spread > 0.05, "channels are too close - looks like a grey AOV"


@needs_fixtures
def test_explicit_layer_override():
    rgb, _, _, _, layer, note = core.read_layer(BLENDER + ".exr",
                                                requested="Ambient_Occlusion")
    assert layer == "Ambient_Occlusion" and note is None


@needs_fixtures
def test_thumbnail_is_smaller_and_valid(tmp_path):
    uri, info = core.make_thumbnail(BLENDER + ".exr", settings(tmp_path),
                                    max_px=256)
    assert uri.startswith("data:image/png;base64,")
    assert info["layer"] == "Beauty_Denoised"
    assert max(info["width"], info["height"]) <= 256


@needs_fixtures
def test_thumbnail_matches_full_conversion(tmp_path):
    """A preview that doesn't predict the output is worse than none."""
    s = settings(tmp_path)
    uri, _ = core.make_thumbnail(BLENDER + ".exr", s, max_px=256)
    out, _ = core.convert_one(BLENDER + ".exr", s)

    import base64
    raw = base64.b64decode(uri.split(",", 1)[1])
    tmp_png = tmp_path / "thumb.png"
    tmp_png.write_bytes(raw)

    thumb = read_u8(str(tmp_png)).astype(np.float32)
    full = read_u8(out).astype(np.float32)
    n = min(thumb.shape[2], full.shape[2])
    assert abs(thumb[..., :n].mean() - full[..., :n].mean()) < 4.0
