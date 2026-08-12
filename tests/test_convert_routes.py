"""
The three routes to the same pixels must agree on how they convert.

The converter window, the Explorer right-click verbs and the viewer's Convert
button all end up writing a file, but only the window gathers settings from the
UI - the other two build their own blob in `convert_cli`. When the window's
default edge-alpha convention changed, that blob kept the old one, so a
right-click convert quietly disagreed with the same settings in the window.

Nothing about the output would look wrong; you would only find it by diffing
two files that were supposed to be identical.
"""

import core
import exr2srgb as app


def _captured(monkeypatch, **kw):
    seen = {}

    def fake(path, settings):
        seen.update(settings)
        return "out.png", {}

    monkeypatch.setattr(core, "convert_one", fake)
    app.convert_cli("frame.exr", **kw)
    return seen


def test_right_click_convert_uses_the_window_default_alpha(monkeypatch):
    """
    `unpremult=False` is the Nuke/After Effects convention, and it is what
    `applyDefaults` sets the Alpha dropdown to. The two have to move together.
    """
    s = _captured(monkeypatch)
    assert s["unpremult"] is False, (
        "the right-click and viewer converts still use the straight-alpha "
        "convention while the converter window defaults to the matched one")
    assert s["alpha_mode"] == "keep"


def test_right_click_convert_matches_the_documented_ladder(monkeypatch):
    """It is the same ACES setup the window opens with, not a second choice."""
    s = _captured(monkeypatch)
    assert s["config"] == core.ACES_CONFIGS[core.DEFAULT_CONFIG_LABEL]
    assert s["src"] == "ACEScg"
    assert s["view"] == core.view_for(s["config"], s["display"], True)


def test_scene_linear_verb_keeps_its_transfer(monkeypatch):
    """The linear verb moves data; it must not pick up a display transform."""
    s = _captured(monkeypatch, fmt="tiff", bits=32, transfer="linear")
    assert s["transfer"] == "linear"
    assert s["suffix"].endswith("_linear")
