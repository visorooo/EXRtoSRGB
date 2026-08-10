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

import json
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
        "transfer": "display",
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


def read_f32(path):
    """Normalised 0-1 pixels, so 8- and 16-bit files compare on one scale."""
    src = oiio.ImageInput.open(path)
    assert src is not None, "could not open %s" % path
    spec = src.spec()
    arr = np.array(src.read_image(format="float"), dtype=np.float32)
    src.close()
    return arr.reshape(spec.height, spec.width, spec.nchannels)


def _decode(uri):
    """Decode a preview data URI back to pixels for assertions."""
    import base64, tempfile
    raw = base64.b64decode(uri.split(",", 1)[1])
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with open(path, "wb") as fh:
            fh.write(raw)
        return read_u8(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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


def test_partial_alpha_output_is_associated(tmp_path):
    """
    On a half-covered pixel the file must carry `alpha * f(colour)`.

    This is the relationship that pins down all three of: un-premultiply
    happened, the transform saw true surface colour, and alpha went back on
    exactly once. A missing re-premultiply gives f(colour) - too bright - and
    premultiplying an already-premultiplied image gives alpha * f(alpha *
    colour), the dark edge fringe. Checked against a real production render:
    this formula matches After Effects to under one 8-bit level, while the
    double-premultiplied variant is out by 9.
    """
    half = _synthetic_alpha_exr(str(tmp_path / "half.exr"), cov=0.5)
    full = _synthetic_alpha_exr(str(tmp_path / "full.exr"), cov=1.0)
    s = lambda tag: settings(tmp_path, bits=16, suffix=tag)   # noqa: E731

    got = read_f32(core.convert_one(half, s("_half"))[0])
    opaque = read_f32(core.convert_one(full, s("_full"))[0])

    a = got[0, 0, 3]
    assert 0.49 < a < 0.51, "fixture should be half covered"
    # f(colour) is what the fully opaque render of the same colour produced
    expected = opaque[0, 0, :3] * a
    assert np.allclose(got[0, 0, :3], expected, atol=1.5 / 255), (
        "partial-alpha RGB %s is not alpha*f(colour) %s"
        % (got[0, 0, :3], expected))

    # and rule the two failure modes out explicitly
    assert not np.allclose(got[0, 0, :3], opaque[0, 0, :3], atol=1.5 / 255), \
        "output is straight alpha - the re-premultiply is missing"
    assert not np.allclose(got[0, 0, :3], opaque[0, 0, :3] * a * a,
                           atol=1.5 / 255), "alpha was applied twice"


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
# TIFF and scene-linear output
# ---------------------------------------------------------------------------

def _hdr_exr(path):
    """One pixel per value, including values well above 1.0."""
    arr = np.zeros((1, 4, 4), dtype=np.float32)
    for i, v in enumerate((0.18, 1.0, 4.0, 12.5)):
        arr[0, i, :3] = v
        arr[0, i, 3] = 1.0
    out = oiio.ImageOutput.create(path)
    out.open(path, oiio.ImageSpec(4, 1, 4, "float"))
    out.write_image(arr)
    out.close()
    return path


@pytest.mark.parametrize("fmt,bits,ext,pixfmt", [
    ("tiff", 8, ".tif", "uint8"),
    ("tiff", 16, ".tif", "uint16"),
    ("tiff", 32, ".tif", "float"),
    ("png", 8, ".png", "uint8"),
    ("png", 16, ".png", "uint16"),
])
def test_format_and_depth(tmp_path, fmt, bits, ext, pixfmt):
    src = _synthetic_alpha_exr(str(tmp_path / "a.exr"))
    out, _ = core.convert_one(src, settings(
        tmp_path, format=fmt, bits=bits, suffix="_%s%d" % (fmt, bits)))
    assert out.endswith(ext)
    i = oiio.ImageInput.open(out)
    assert str(i.spec().format) == pixfmt
    i.close()


def test_png_cannot_hold_32bit(tmp_path):
    """PNG has no float, so asking for 32-bit must degrade, not corrupt."""
    src = _synthetic_alpha_exr(str(tmp_path / "a.exr"))
    fmt, pixfmt, bits = core.resolve_output(
        settings(tmp_path, format="png", bits=32))
    assert (fmt, pixfmt, bits) == ("png", "uint16", 16)


def test_linear_forces_float_tiff(tmp_path):
    """Scene-linear is meaningless in an 8-bit container; it must upgrade."""
    for fmt, bits in (("png", 8), ("jpeg", 8), ("tiff", 16)):
        assert core.resolve_output(settings(
            tmp_path, format=fmt, bits=bits, transfer="linear")) \
            == ("tiff", "float", 32)


def test_linear_output_is_bit_exact(tmp_path):
    """
    The whole point of scene-linear: pixels come back exactly as they went in.

    No display transform, no clamp, no alpha juggling. A single altered bit here
    means the mode is not doing what it claims.
    """
    src = _hdr_exr(str(tmp_path / "hdr.exr"))
    out, _ = core.convert_one(src, settings(
        tmp_path, transfer="linear", format="tiff", bits=32, alpha_mode="keep"))
    assert out.endswith(".tif")

    i = oiio.ImageInput.open(src)
    a = np.array(i.read_image(format="float"), dtype=np.float32); i.close()
    j = oiio.ImageInput.open(out)
    b = np.array(j.read_image(format="float"), dtype=np.float32)
    assert str(j.spec().format) == "float"
    j.close()
    assert np.array_equal(a.ravel(), b.ravel()), "linear output was altered"


def test_linear_preserves_values_above_one(tmp_path):
    src = _hdr_exr(str(tmp_path / "hdr.exr"))
    out, _ = core.convert_one(src, settings(
        tmp_path, transfer="linear", format="tiff", bits=32))
    i = oiio.ImageInput.open(out)
    px = np.array(i.read_image(format="float"), dtype=np.float32).reshape(1, 4, 4)
    i.close()
    assert px[0, 3, 0] == pytest.approx(12.5), "highlight was clamped"
    assert px[0, 2, 0] == pytest.approx(4.0)


def test_display_output_is_clamped(tmp_path):
    """The display path must still clamp - only linear is allowed past 1.0."""
    src = _hdr_exr(str(tmp_path / "hdr.exr"))
    out, _ = core.convert_one(src, settings(
        tmp_path, transfer="display", format="tiff", bits=32))
    i = oiio.ImageInput.open(out)
    px = np.array(i.read_image(format="float"), dtype=np.float32)
    i.close()
    assert px.max() <= 1.0


def test_linear_ignores_unpremultiply(tmp_path):
    """Linear is a passthrough; the alpha toggle must not change the pixels."""
    src = _synthetic_alpha_exr(str(tmp_path / "half.exr"), cov=0.5)
    a, _ = core.convert_one(src, settings(
        tmp_path, transfer="linear", unpremult=True, suffix="_on"))
    b, _ = core.convert_one(src, settings(
        tmp_path, transfer="linear", unpremult=False, suffix="_off"))
    ia = oiio.ImageInput.open(a); pa = np.array(ia.read_image(format="float")); ia.close()
    ib = oiio.ImageInput.open(b); pb = np.array(ib.read_image(format="float")); ib.close()
    assert np.array_equal(pa, pb)


def test_linear_tags_colorspace(tmp_path):
    """
    A linear file labelled sRGB would get a transfer function applied twice
    downstream. TIFF can only express sRGB natively - "Linear" has no tag and
    OIIO drops it - so the description carries it and must not be lost.
    """
    src = _hdr_exr(str(tmp_path / "hdr.exr"))
    lin, _ = core.convert_one(src, settings(tmp_path, transfer="linear", suffix="_l"))
    dis, _ = core.convert_one(src, settings(tmp_path, format="tiff", suffix="_d"))

    def attrs(path):
        i = oiio.ImageInput.open(path)
        a = {x.name: x.value for x in i.spec().extra_attribs}
        i.close()
        return a

    la, da = attrs(lin), attrs(dis)
    assert la["ImageDescription"] == "colorspace=Linear"
    assert da["ImageDescription"] == "colorspace=sRGB"
    # the display file additionally carries a real sRGB tag; the linear must not
    assert "sRGB" not in str(la.get("oiio:ColorSpace", ""))
    assert "srgb" in str(da.get("oiio:ColorSpace", "")).lower()


def test_preview_stays_display_referred(tmp_path):
    """Raw linear values would render as a near-black smear."""
    src = _hdr_exr(str(tmp_path / "hdr.exr"))
    uri, info = core.make_thumbnail(src, settings(tmp_path, transfer="linear"))
    assert info["preview_only"] is True
    uri2, info2 = core.make_thumbnail(src, settings(tmp_path, transfer="display"))
    assert info2["preview_only"] is False
    assert uri == uri2, "preview should be identical regardless of output mode"


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
# Cryptomatte
# ---------------------------------------------------------------------------

# Hashes lifted from a real Blender 5.2 render's manifest. These are the
# renderer's own values, so they check our MurmurHash3_32 against another
# implementation rather than against itself. The first entry is deliberately
# non-ASCII: object names come from the DCC and are arbitrary Unicode.
BLENDER_HASHES = {
    "☄ Aurora Shader Controller.004": "cbe3e51b",
    "back_mountains_plane.004": "a6456e22",
    "mountains_fill.004": "422164de",
    "Snow Cliffs Mountain Mountains.009": "e13ba364",
    "default_surface": "78d05529",
    "default_volume": "b331280e",
    "default_light": "fe269f93",
    "default_background": "dba7ec85",
    "Sun.Default.021": "133dbde9",
    "Area.HDRI.019": "295e432c",
}


@pytest.mark.parametrize("name,expected", list(BLENDER_HASHES.items()))
def test_murmur3_matches_blender(name, expected):
    assert format(core.murmur3_32(name), "08x") == expected


def test_hash_to_float_avoids_nan_and_inf():
    """
    The spec's exponent clamp. An all-ones or all-zeros exponent gives NaN, Inf
    or a denormal - none of which survive the float equality that ID matching
    depends on.
    """
    for h in (0x7F800000, 0xFF800000, 0x7FFFFFFF, 0x00000000, 0x000FFFFF):
        v = core.hash_to_float(h)
        assert not np.isnan(v) and not np.isinf(v)


def _crypto_exr(path, layers=1, manifest_name="CryptoObject"):
    """A spec-shaped cryptomatte: two ranks per layer, ID/coverage pairs."""
    names, ids = ["sphere_A", "cube_B", "floor"], {}
    for n in names:
        ids[n] = core.name_to_id(n)
    W = H = 8
    chans, data = [], []
    for li in range(layers):
        base = "%s%02d" % (manifest_name, li)
        chans += ["%s.%s" % (base, c) for c in "rgba"]
        block = np.zeros((H, W, 4), np.float32)
        if li == 0:
            block[:4, :4] = [ids["sphere_A"], 1.0, 0.0, 0.0]
            block[:4, 4:] = [ids["cube_B"], 0.6, ids["floor"], 0.4]
            block[4:, :] = [ids["floor"], 1.0, 0.0, 0.0]
        data.append(block)
    arr = np.concatenate(data, axis=2)

    spec = oiio.ImageSpec(W, H, arr.shape[2], "float")
    spec.channelnames = tuple(chans)
    spec.attribute("cryptomatte/abc1234/name", manifest_name)
    spec.attribute("cryptomatte/abc1234/hash", "MurmurHash3_32")
    spec.attribute("cryptomatte/abc1234/conversion", "uint32_to_float32")
    spec.attribute("cryptomatte/abc1234/manifest", json.dumps(
        {n: format(core.murmur3_32(n), "08x") for n in names}))
    out = oiio.ImageOutput.create(path)
    out.open(path, spec)
    out.write_image(arr)
    out.close()
    return path


def test_probe_finds_cryptomatte(tmp_path):
    c = core.probe_cryptomattes(_crypto_exr(str(tmp_path / "c.exr")))
    assert len(c) == 1
    assert c[0]["label"] == "CryptoObject"
    assert len(c[0]["objects"]) == 3
    assert len(c[0]["ranks"]) == 2      # two ranks per layer
    assert c[0]["incomplete"] is False


def test_probe_strips_viewlayer_prefix(tmp_path):
    """Blender names them ViewLayer.CryptoObject; the UI wants the short form."""
    p = _crypto_exr(str(tmp_path / "v.exr"), manifest_name="ViewLayer.CryptoObject")
    c = core.probe_cryptomattes(p)[0]
    assert c["name"] == "ViewLayer.CryptoObject"
    assert c["label"] == "CryptoObject"
    assert len(c["ranks"]) == 2


def test_multiple_rank_layers(tmp_path):
    p = _crypto_exr(str(tmp_path / "m.exr"), layers=3)
    assert len(core.probe_cryptomattes(p)[0]["ranks"]) == 6


def test_extract_matte_coverage(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    cov, W, H = core.extract_matte(p, c, ["cube_B"])
    assert cov.max() == pytest.approx(0.6)
    cov2, _, _ = core.extract_matte(p, c, ["floor"])
    # floor is a full half plus the 0.4 share of the mixed quadrant
    assert cov2.max() == pytest.approx(1.0)
    assert (cov2 > 0).sum() > (cov > 0).sum()


def test_extract_matte_union(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    total, _, _ = core.extract_matte(p, c, ["sphere_A", "cube_B", "floor"])
    assert np.allclose(total, 1.0), "every pixel is covered by exactly one set"


def test_extract_matte_unknown_object(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    with pytest.raises(ValueError):
        core.extract_matte(p, c, ["not_in_scene"])


def test_coverage_is_clamped(tmp_path):
    """
    A single rank can exceed 1.0 - a Blender render reached 2.633, because the
    pixel filter accumulates. Without the clamp the matte would blow past white.
    """
    W = H = 4
    fid = core.name_to_id("thing")
    arr = np.zeros((H, W, 4), np.float32)
    arr[..., 0] = fid
    arr[..., 1] = 2.633
    path = str(tmp_path / "hot.exr")
    spec = oiio.ImageSpec(W, H, 4, "float")
    spec.channelnames = ("CryptoObject00.r", "CryptoObject00.g",
                         "CryptoObject00.b", "CryptoObject00.a")
    spec.attribute("cryptomatte/z/name", "CryptoObject")
    spec.attribute("cryptomatte/z/manifest",
                   json.dumps({"thing": format(core.murmur3_32("thing"), "08x")}))
    o = oiio.ImageOutput.create(path)
    o.open(path, spec)
    o.write_image(arr)
    o.close()
    c = core.probe_cryptomattes(path)[0]
    cov, _, _ = core.extract_matte(path, c, ["thing"])
    assert cov.max() == 1.0


def test_matte_is_white_silhouette(tmp_path):
    """Fully covered pixels must be white, edges premultiplied white."""
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    out = core.convert_mattes(
        p, {"out_dir": str(tmp_path), "format": "png", "bits": 16},
        c["id"], ["floor"])[0]
    px = read_u8(out).astype(np.float32) / 255.0
    solid = px[..., 3] > 0.99
    assert solid.any()
    assert px[..., :3][solid].min() == pytest.approx(1.0, abs=0.01)


def test_straight_mode_upgrades_to_tiff(tmp_path):
    """
    OIIO's PNG writer always associates alpha, so RGB 1.0 comes back as the
    coverage and "straight" would silently equal "associated".
    """
    s = {"format": "png", "bits": 16, "matte_mode": "straight"}
    assert core.resolve_matte_output(s)[0] == "tiff"
    assert core.resolve_matte_output({"format": "png", "bits": 16})[0] == "png"


def test_matte_never_jpeg(tmp_path):
    """A matte without an alpha channel is not a matte."""
    assert core.resolve_matte_output({"format": "jpeg", "bits": 8})[0] == "png"


def test_straight_mode_keeps_rgb_white(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    out = core.convert_mattes(
        p, {"out_dir": str(tmp_path), "format": "tiff", "bits": 16,
            "matte_mode": "straight"}, c["id"], ["cube_B"])[0]
    assert out.endswith(".tif")
    i = oiio.ImageInput.open(out)
    px = np.array(i.read_image(format="float"), np.float32).reshape(8, 8, 4)
    i.close()
    assert px[..., :3].min() == pytest.approx(1.0), "RGB should be pinned to white"
    assert px[..., 3].max() == pytest.approx(0.6)


def test_one_file_per_object_vs_combined(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    base = {"out_dir": str(tmp_path), "format": "png", "bits": 8}
    many = core.convert_mattes(p, base, c["id"], ["sphere_A", "cube_B"])
    assert len(many) == 2 and len(set(many)) == 2
    one = core.convert_mattes(p, dict(base, matte_combine=True), c["id"],
                              ["sphere_A", "cube_B"])
    assert len(one) == 1


def test_matte_filename_is_filesystem_safe(tmp_path):
    c = {"label": "CryptoObject"}
    s = {"out_dir": str(tmp_path), "format": "png"}
    name = core.matte_filename("/r/shot.exr", c, "☄ Aurora / Ctrl.004", s, 0)
    stem = os.path.basename(name)
    assert "☄" not in stem and "/" not in stem[1:] and stem.endswith(".png")


def test_unusable_cryptomatte_is_reported(tmp_path):
    """
    Redshift has been seen writing a three-channel 'Cryptomatte_' layer with no
    rank numbering. That is not usable, and must say so rather than produce a
    blank matte.
    """
    W = H = 4
    path = str(tmp_path / "bad.exr")
    spec = oiio.ImageSpec(W, H, 3, "float")
    spec.channelnames = ("Cryptomatte_.red", "Cryptomatte_.green",
                         "Cryptomatte_.blue")
    spec.attribute("cryptomatte/q/name", "Cryptomatte_")
    spec.attribute("cryptomatte/q/manifest", json.dumps({"a": "00000001"}))
    o = oiio.ImageOutput.create(path)
    o.open(path, spec)
    o.write_image(np.zeros((H, W, 3), np.float32))
    o.close()
    c = core.probe_cryptomattes(path)[0]
    assert c["incomplete"] is True
    with pytest.raises(ValueError):
        core.convert_mattes(path, {"out_dir": str(tmp_path)}, c["id"], ["a"])


def test_id_colours_are_distinct_and_stable(tmp_path):
    """Neighbouring objects must not come out the same colour."""
    names = ["sphere_A", "cube_B", "floor", "Sun.Default.021", "Area.HDRI.019"]
    cols = [core.id_colour(core.name_to_id(n)) for n in names]
    assert len(set(cols)) == len(names)
    # stable across calls, or the preview would flicker between renders
    assert cols == [core.id_colour(core.name_to_id(n)) for n in names]
    # nothing near-black, which would read as background
    assert all(max(c) > 0.5 for c in cols)
    assert core.id_colour(0.0) == (0.0, 0.0, 0.0)


def test_crypto_preview_renders_and_picks(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    uri, state = core.crypto_preview(p, c, max_px=64)
    assert uri.startswith("data:image/png;base64,")
    # the fixture puts sphere_A top-left, floor across the bottom half
    assert core.object_at(state, 0.2, 0.2) == "sphere_A"
    assert core.object_at(state, 0.5, 0.9) == "floor"


def test_preview_downsampling_never_invents_ids(tmp_path):
    """
    Averaging two IDs yields a third that matches no object. ID planes have to
    be sampled, not filtered - this is why _subsample exists.
    """
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    _, state = core.crypto_preview(p, c, max_px=4)   # forces real downsampling
    valid = set(c["objects"].values()) | {0.0}
    assert all(float(v) in valid for v in np.unique(state["ids"]))


def test_object_at_out_of_range_is_safe(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    _, state = core.crypto_preview(p, c, max_px=32)
    for u, v in ((-1.0, 0.5), (2.0, 0.5), (0.5, -3.0), (0.5, 9.9)):
        core.object_at(state, u, v)      # must not raise
    assert core.object_at(None, 0.5, 0.5) is None


def test_selection_dims_the_rest(tmp_path):
    p = _crypto_exr(str(tmp_path / "c.exr"))
    c = core.probe_cryptomattes(p)[0]
    plain, _ = core.crypto_preview(p, c, max_px=32)
    dimmed, _ = core.crypto_preview(p, c, max_px=32, selected=["floor"])
    assert plain != dimmed


def test_crypto_layers_still_excluded_from_beauty():
    """The v1.1 rule must survive: crypto is never a beauty candidate."""
    for layer in ("CryptoObject00", "ViewLayer.CryptoMaterial00", "Cryptomatte_"):
        assert core.score_layer(layer) < 0


# ---------------------------------------------------------------------------
# Viewer
# ---------------------------------------------------------------------------

def _hdr_rgba(tmp_path, w=8, h=8, value=0.18):
    """
    Neutral grey, deliberately.

    A saturated colour would be gamut-mapped by the ACES view - (0.18, 1.0, 4.0)
    drives red to exactly 0 - which makes per-channel assertions meaningless.
    Grey goes through the documented 0.18 -> 91 ladder instead.
    """
    path = str(tmp_path / "v.exr")
    arr = np.zeros((h, w, 4), np.float32)
    arr[..., :3] = value
    arr[..., 3] = 0.5
    o = oiio.ImageOutput.create(path)
    o.open(path, oiio.ImageSpec(w, h, 4, "float"))
    o.write_image(arr)
    o.close()
    return path


def test_viewer_caches_the_decode(tmp_path):
    """The whole point: a second load of the same layer must not re-read."""
    v = core.ViewerSession()
    p = _hdr_rgba(tmp_path)
    assert v.load(p) is True
    assert v.load(p) is False
    assert v.size == (8, 8)


def test_viewer_reloads_when_layer_changes(tmp_path):
    names = ["%s.%s" % (l, c) for l in ("Beauty", "Other") for c in "RGB"]
    path = str(tmp_path / "two.exr")
    spec = oiio.ImageSpec(4, 4, 6, "float")
    spec.channelnames = tuple(names)
    o = oiio.ImageOutput.create(path)
    o.open(path, spec)
    o.write_image(np.zeros((4, 4, 6), np.float32))
    o.close()
    v = core.ViewerSession()
    assert v.load(path, "Beauty") is True
    assert v.load(path, "Beauty") is False
    assert v.load(path, "Other") is True, "a different layer must re-read"


def test_exposure_is_a_stop_not_a_multiplier(tmp_path):
    """
    Exposure is applied in linear before the transform, so +1 stop doubles the
    scene value. Applied after, it would just brighten display values.
    """
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    s = settings(tmp_path)
    base = _decode(v.render(s, exposure=0.0, max_px=8)[0])
    up = _decode(v.render(s, exposure=1.0, max_px=8)[0])
    assert up[..., 0].mean() > base[..., 0].mean()
    # 0.18 doubled is 0.36; through the ACES curve that is well above 91/255
    assert up[0, 0, 0] > base[0, 0, 0] + 20


def test_gamma_changes_output(tmp_path):
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    s = settings(tmp_path)
    a = _decode(v.render(s, gamma=1.0, max_px=8)[0])
    b = _decode(v.render(s, gamma=2.2, max_px=8)[0])
    assert b[..., 0].mean() > a[..., 0].mean(), "gamma > 1 should lift midtones"


@pytest.mark.parametrize("channel", list(core.CHANNEL_MODES))
def test_channel_modes_render(tmp_path, channel):
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    uri, w, h = v.render(settings(tmp_path), channel=channel, max_px=8)
    px = _decode(uri)
    assert (w, h) == (8, 8)
    if channel in ("r", "g", "b", "a", "luma"):
        # isolated channels are shown as grey, so the three are equal
        assert np.array_equal(px[..., 0], px[..., 1])
        assert np.array_equal(px[..., 1], px[..., 2])


def test_alpha_channel_bypasses_the_transform(tmp_path):
    """Alpha is coverage, not colour; an ACES curve on it would be wrong."""
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    px = _decode(v.render(settings(tmp_path), channel="a", max_px=8)[0])
    assert abs(int(px[0, 0, 0]) - 128) <= 1, "0.5 alpha should read as ~128"


def test_render_does_not_corrupt_the_cache(tmp_path):
    """apply_transform writes in place; the cached layer must survive it."""
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    s = settings(tmp_path)
    first = v.render(s, max_px=8)[0]
    for _ in range(3):
        v.render(s, exposure=2.0, max_px=8)
        v.render(s, channel="g", max_px=8)
    assert v.render(s, max_px=8)[0] == first, "cached pixels were mutated"


def test_probe_reports_full_res_linear_values(tmp_path):
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path, w=64, h=64))
    v.render(settings(tmp_path), max_px=8)   # a heavily downsampled view
    p = v.sample(0.5, 0.5)
    # the scene value, not the 0.356 the display transform would show
    assert p["r"] == pytest.approx(0.18)
    assert p["g"] == pytest.approx(0.18)
    assert p["a"] == pytest.approx(0.5)
    assert 0 <= p["x"] < 64 and 0 <= p["y"] < 64


def test_probe_clamps_out_of_range(tmp_path):
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    for u, v_ in ((-5.0, 0.5), (9.0, 0.5), (0.5, -2.0), (0.5, 4.0)):
        assert v.sample(u, v_) is not None


def test_render_before_load_raises(tmp_path):
    with pytest.raises(RuntimeError):
        core.ViewerSession().render(settings(tmp_path))


# ---------------------------------------------------------------------------
# Layer naming and sequence expansion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("layer,expected", [
    ("Ambient Occlusion", "_Ambient_Occlusion"),
    ("ViewLayer.Diffuse Color", "_Diffuse_Color"),   # view layer prefix dropped
    ("ViewLayer.CryptoObject00", "_CryptoObject00"),
    ("", ""),
    (None, ""),
])
def test_layer_tag(layer, expected):
    assert core.layer_tag(layer) == expected


def test_layer_tag_prevents_collisions(tmp_path):
    """
    Two layers of the same EXR must not write the same filename.

    This is the other half of the export bug: even once the right layer is
    read, an unqualified name means the second export silently replaces the
    first.
    """
    names = []
    for layer in ("Beauty", "Ambient Occlusion"):
        s = settings(tmp_path, suffix=core.layer_tag(layer) + "_srgb")
        names.append(core.output_path_for("/r/shot.exr", s))
    assert len(set(names)) == 2


def _seq_files(tmp_path, stem="beauty", n=5, pad=4, ext=".exr"):
    made = []
    for i in range(1, n + 1):
        p = tmp_path / ("%s.%0*d%s" % (stem, pad, i, ext))
        p.write_bytes(b"x")
        made.append(str(p))
    return made


def test_siblings_expand_from_one_frame(tmp_path):
    made = _seq_files(tmp_path)
    got = core.find_sequence_siblings(made[0])
    assert sorted(got) == sorted(made)


def test_siblings_ignore_a_different_stem(tmp_path):
    a = _seq_files(tmp_path, "beauty")
    _seq_files(tmp_path, "beauty_v2")
    assert sorted(core.find_sequence_siblings(a[0])) == sorted(a)


def test_siblings_ignore_different_padding(tmp_path):
    a = _seq_files(tmp_path, "shot", n=3, pad=4)
    _seq_files(tmp_path, "shot", n=3, pad=6)
    got = core.find_sequence_siblings(a[0])
    assert all(len(os.path.basename(p).split(".")[1]) == 4 for p in got)
    assert len(got) == 3


def test_siblings_ignore_other_extensions(tmp_path):
    a = _seq_files(tmp_path, "plate", n=3, ext=".exr")
    _seq_files(tmp_path, "plate", n=3, ext=".png")
    got = core.find_sequence_siblings(a[0])
    assert len(got) == 3 and all(p.endswith(".exr") for p in got)


def test_unnumbered_file_returns_itself(tmp_path):
    p = tmp_path / "still.exr"
    p.write_bytes(b"x")
    assert core.find_sequence_siblings(str(p)) == [str(p)]


def test_expanded_frames_group_into_one_entry(tmp_path):
    made = _seq_files(tmp_path, "beauty", n=8)
    entries = core.group_sequences(core.find_sequence_siblings(made[0]))
    assert len(entries) == 1
    assert entries[0]["kind"] == "sequence" and entries[0]["count"] == 8


# ---------------------------------------------------------------------------
# Probe display values
# ---------------------------------------------------------------------------

def test_probe_returns_hex_of_the_display_colour(tmp_path):
    """
    A hex has to be the colour on screen. Hex of the linear value would read
    near-black for anything normally exposed and match nothing pasted elsewhere.
    """
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path, value=0.18))
    p = v.sample(0.5, 0.5, settings(tmp_path))
    assert p["r"] == pytest.approx(0.18)          # linear, unchanged
    # 0.18 through the ACES curve is 91 -> 0x5B
    assert p["dr"] == 91 and p["hex"] == "#5B5B5B"


def test_probe_hex_follows_exposure(tmp_path):
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path, value=0.18))
    base = v.sample(0.5, 0.5, settings(tmp_path))
    up = v.sample(0.5, 0.5, settings(tmp_path), exposure=1.0)
    assert up["dr"] > base["dr"], "hex must track what is on screen"
    assert up["r"] == base["r"], "the linear value is the pixel and must not move"


def test_probe_without_settings_has_no_hex(tmp_path):
    v = core.ViewerSession()
    v.load(_hdr_rgba(tmp_path))
    assert "hex" not in v.sample(0.5, 0.5)


# ---------------------------------------------------------------------------
# Parallel conversion
# ---------------------------------------------------------------------------

def _frames(tmp_path, n=6):
    out = []
    for i in range(1, n + 1):
        p = str(tmp_path / ("f.%04d.exr" % i))
        arr = np.full((2, 2, 3), i / 100.0, dtype=np.float32)
        o = oiio.ImageOutput.create(p)
        o.open(p, oiio.ImageSpec(2, 2, 3, "float"))
        o.write_image(arr)
        o.close()
        out.append(p)
    return out


def test_convert_many_converts_everything(tmp_path):
    files = _frames(tmp_path)
    out = tmp_path / "out"
    res = core.convert_many(files, settings(out), workers=4)
    assert len(res) == len(files)
    assert all(r[2] is None for r in res), "no frame should have errored"
    assert len(list(out.glob("*.png"))) == len(files)


def test_convert_many_preserves_order(tmp_path):
    """Completion order is a race; the log has to read like the file list."""
    files = _frames(tmp_path, 12)
    seen = []
    core.convert_many(files, settings(tmp_path / "o"), workers=6,
                      on_result=lambda i, p, o, inf, e: seen.append((i, p)))
    assert [i for i, _ in seen] == list(range(len(files)))
    assert [p for _, p in seen] == files


def test_convert_many_matches_serial(tmp_path):
    """Threading must not change a single pixel."""
    files = _frames(tmp_path, 4)
    a, b = tmp_path / "a", tmp_path / "b"
    for f in files:
        core.convert_one(f, settings(a))
    core.convert_many(files, settings(b), workers=4)
    for f in files:
        name = os.path.basename(f).replace(".exr", ".png")
        assert np.array_equal(read_u8(str(a / name)), read_u8(str(b / name)))


def test_convert_many_survives_a_bad_file(tmp_path):
    """One unreadable frame must not take the batch down with it."""
    files = _frames(tmp_path, 4)
    bad = str(tmp_path / "f.0099.exr")
    open(bad, "wb").write(b"not an exr")
    res = core.convert_many(files + [bad], settings(tmp_path / "o"), workers=3)
    assert sum(1 for r in res if r[2] is not None) == 1
    assert sum(1 for r in res if r[2] is None) == len(files)


def test_convert_many_honours_cancel(tmp_path):
    files = _frames(tmp_path, 8)
    res = core.convert_many(files, settings(tmp_path / "o"), workers=2,
                            should_stop=lambda: True)
    assert all(r[0] is None for r in res), "nothing should have been written"


def test_worker_count_is_bounded(tmp_path):
    """More workers than frames is waste; each holds a full float frame."""
    assert 1 <= core.default_workers() <= 8
    files = _frames(tmp_path, 2)
    core.convert_many(files, settings(tmp_path / "o"), workers=99)


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
