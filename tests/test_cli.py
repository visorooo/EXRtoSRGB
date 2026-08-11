"""
Tests for the command line.

`cli.py` imports core and nothing else, so this stays inside the same boundary
as test_core: no window is ever created. The point of most of these is that the
CLI is a real path to the same pixels, not a thin wrapper that quietly differs -
so the ACES ladder is asserted here too.
"""

import os
import sys

import numpy as np
import pytest
import OpenImageIO as oiio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli  # noqa: E402
import core  # noqa: E402


def _exr(path, value=0.18, layers=False):
    chans = ["R", "G", "B", "A"]
    if layers:
        chans += ["Ambient_Occlusion.R", "Ambient_Occlusion.G",
                  "Ambient_Occlusion.B"]
    spec = oiio.ImageSpec(4, 4, len(chans), "float")
    spec.channelnames = chans
    px = np.zeros((4, 4, len(chans)), np.float32)
    px[..., :3] = value
    px[..., 3] = 1.0
    if layers:
        px[..., 4:] = 0.5
    out = oiio.ImageOutput.create(str(path))
    out.open(str(path), spec)
    out.write_image(px)
    out.close()
    return str(path)


def _read(path):
    src = oiio.ImageInput.open(path)
    spec = src.spec()
    arr = np.array(src.read_image(format="uint8"), np.uint8)
    src.close()
    return arr.reshape(spec.height, spec.width, spec.nchannels)


# ---------------------------------------------------------------------------
# Colour, through the CLI rather than around it
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(0.18, 91), (1.0, 207), (4.0, 244)])
def test_cli_produces_the_aces_ladder(tmp_path, value, expected):
    """
    The same ladder the README and the app assert.

    A CLI that reached the pixels by a different route could drift from the UI
    without anything failing, and the ladder is what would catch it: if 1.0
    lands on 255 the ACES curve is not being applied at all.
    """
    src = _exr(tmp_path / "grey.exr", value)
    rc = cli.main([src, "--out", str(tmp_path / "out"), "--bits", "8", "-q"])
    assert rc == cli.EXIT_OK
    px = _read(str(tmp_path / "out" / "grey_srgb.png"))
    assert abs(int(px[0, 0, 0]) - expected) <= 1


# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

def test_all_layers_writes_one_file_each(tmp_path):
    src = _exr(tmp_path / "beauty.exr", layers=True)
    out = tmp_path / "out"
    assert cli.main([src, "--out", str(out), "--all-layers", "-q"]) == cli.EXIT_OK
    names = sorted(p.name for p in out.iterdir())
    assert names == ["beauty_Ambient_Occlusion_srgb.png", "beauty_srgb.png"]


def test_all_layers_files_are_not_identical(tmp_path):
    """The whole failure mode this guards: same beauty written twice."""
    src = _exr(tmp_path / "beauty.exr", value=0.18, layers=True)
    out = tmp_path / "out"
    cli.main([src, "--out", str(out), "--all-layers", "-q"])
    a = _read(str(out / "beauty_srgb.png"))
    b = _read(str(out / "beauty_Ambient_Occlusion_srgb.png"))
    assert not np.array_equal(a[..., :3], b[..., :3])


def test_named_layer_is_honoured(tmp_path):
    """
    --layer must read that layer, not auto-detect the beauty.

    The fixture makes the two tell each other apart: the beauty is 0.18, which
    is 91 through ACES, and the AO layer is 0.5, which is 165. Getting 91 here
    would be the v1.0 bug wearing a different hat.
    """
    src = _exr(tmp_path / "beauty.exr", layers=True)
    out = tmp_path / "out"
    cli.main([src, "--out", str(out), "--layer", "Ambient_Occlusion", "-q"])
    # a single named layer keeps the plain suffix - nothing to collide with
    assert [p.name for p in out.iterdir()] == ["beauty_srgb.png"]
    got = int(_read(str(out / "beauty_srgb.png"))[0, 0, 0])
    assert abs(got - 165) <= 1, "expected 0.5 through ACES, got %d" % got
    assert abs(got - 91) > 10, "that is the beauty, not the requested layer"


# ---------------------------------------------------------------------------
# Output rules
# ---------------------------------------------------------------------------

def test_dry_run_writes_nothing(tmp_path, capsys):
    src = _exr(tmp_path / "a.exr")
    out = tmp_path / "out"
    assert cli.main([src, "--out", str(out), "--dry-run"]) == cli.EXIT_OK
    assert "a_srgb.png" in capsys.readouterr().out
    assert not list(out.iterdir()), "dry run must not write"


def test_linear_forces_float_tiff(tmp_path):
    src = _exr(tmp_path / "a.exr")
    out = tmp_path / "out"
    cli.main([src, "--out", str(out), "--look", "linear", "-q"])
    assert [p.name for p in out.iterdir()] == ["a_linear.tif"]


def test_no_suffix(tmp_path):
    src = _exr(tmp_path / "a.exr")
    out = tmp_path / "out"
    cli.main([src, "--out", str(out), "--no-suffix", "-q"])
    assert [p.name for p in out.iterdir()] == ["a.png"]


def test_folder_is_expanded(tmp_path):
    for i in range(1, 6):
        _exr(tmp_path / ("f.%04d.exr" % i))
    out = tmp_path / "out"
    assert cli.main([str(tmp_path), "--out", str(out), "-q"]) == cli.EXIT_OK
    assert len(list(out.iterdir())) == 5


def test_nothing_to_convert_is_its_own_exit_code(tmp_path):
    assert cli.main([str(tmp_path), "-q"]) == cli.EXIT_NOTHING


# ---------------------------------------------------------------------------
# Config resolution - a CLI takes whatever is typed
# ---------------------------------------------------------------------------

def test_config_accepts_the_registry_name():
    assert cli._resolve_config("cg-config-v2.2.0_aces-v1.3_ocio-v2.4") \
        == "cg-config-v2.2.0_aces-v1.3_ocio-v2.4"


def test_config_accepts_a_substring():
    assert "v2.2" in cli._resolve_config("cg-config-v2.2")


def test_config_default_is_the_recommended_one():
    assert cli._resolve_config(None) == list(core.ACES_CONFIGS.values())[0]


def test_ambiguous_config_is_refused_not_guessed():
    """Picking one of several silently is how the wrong curve ships."""
    with pytest.raises(SystemExit) as e:
        cli._resolve_config("aces")
    assert "ambiguous" in str(e.value)


def test_unknown_config_is_refused():
    with pytest.raises(SystemExit) as e:
        cli._resolve_config("definitely-not-a-config")
    assert "matched nothing" in str(e.value)


def test_config_accepts_a_path(tmp_path):
    p = tmp_path / "config.ocio"
    p.write_text("dummy")
    assert cli._resolve_config(str(p)) == str(p)
