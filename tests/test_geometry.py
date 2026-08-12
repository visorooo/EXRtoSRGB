"""
Window geometry: can the window that gets restored actually be seen?

This is the "it is in the taskbar but not on the screen" bug, and it is the
worst kind to ship, because it cannot be recovered from the UI - a window you
cannot see is a window you cannot drag back.

Two sources, both reproduced below:

- Minimising fires `moved` with (-32000, -32000) and `resized` with a collapsed
  size. Those were persisted like any other value.
- A rect saved for a display that is no longer attached stays in the file.

The tempting check - "negative coordinates are off-screen" - is wrong, and
wrong specifically on the machine this was reported from: it stacks a second
monitor above the primary at y = -1440, so every valid position on it is
negative. The test has to be intersection with a real display.
"""

import pytest

import exr2srgb as app


# The reporting machine: 3440x1440 primary at the origin, second stacked above.
STACKED = [(0, 0, 3440, 1440), (0, -1440, 3440, 1440)]


@pytest.fixture
def stacked(monkeypatch):
    monkeypatch.setattr(app, "screen_rects", lambda: STACKED)


def test_minimised_position_is_refused(stacked):
    """(-32000, -32000) is a minimise sentinel, not a place to put a window."""
    assert app.geometry_is_sane(-32000, -32000, 1180, 920) is False


def test_negative_is_fine_on_a_monitor_stacked_above(stacked):
    """The whole point: valid positions on the second display are negative."""
    assert app.geometry_is_sane(200, -1200, 1180, 920) is True
    assert app.geometry_is_sane(0, -1440, 3440, 1440) is True


def test_rect_on_a_detached_display_is_refused(stacked):
    """A monitor that is gone leaves a plausible-looking rect behind."""
    assert app.geometry_is_sane(4000, 300, 1180, 920) is False
    assert app.geometry_is_sane(-5000, 200, 1180, 920) is False


def test_a_sliver_on_screen_does_not_count(stacked):
    """Restoring with only an edge visible is not a recovery."""
    assert app.geometry_is_sane(3430, 400, 1180, 920) is False


def test_absurdly_small_is_refused(stacked):
    """The 560x420 that was actually in prefs.json - below any usable size."""
    assert app.geometry_is_sane(100, 100, 60, 40) is False


def test_missing_coordinates_are_not_a_position(stacked):
    assert app.geometry_is_sane(None, None, 1180, 920) is False


def test_saved_size_without_position_still_opens_visible(monkeypatch, stacked):
    """
    The state prefs.json was actually found in: a size and no position.

    It must not fall through to (0, 0) or to whatever the last event left -
    centre it instead.
    """
    monkeypatch.setattr(app, "load_prefs",
                        lambda: {"viewer_geometry": {"w": 900, "h": 700}})
    monkeypatch.setattr(app, "primary_screen", lambda: (0, 0, 3440, 1440))
    x, y, w, h = app.viewer_geometry("nonexistent.exr")
    assert (w, h) == (900, 700)
    assert app.geometry_is_sane(x, y, w, h)


def test_a_tiny_saved_size_is_floored(monkeypatch, stacked):
    """560x420 was restorable and unusable; it comes back at least MIN."""
    monkeypatch.setattr(app, "load_prefs",
                        lambda: {"viewer_geometry": {"w": 60, "h": 40,
                                                     "x": 100, "y": 100}})
    monkeypatch.setattr(app, "primary_screen", lambda: (0, 0, 3440, 1440))
    _x, _y, w, h = app.viewer_geometry("nonexistent.exr")
    assert w >= app.MIN_W and h >= app.MIN_H


class StubEvents:
    """Just enough of pywebview's event container to capture the handlers."""

    def __init__(self):
        self.handlers = {}

    def _slot(self, name):
        events = self

        class Slot:
            def __iadd__(self, fn):
                events.handlers[name] = fn
                return self

        return Slot()

    def __getattr__(self, name):
        return self._slot(name)


class StubWindow:
    def __init__(self):
        self.events = StubEvents()


def _wire(monkeypatch, stored, saved):
    monkeypatch.setattr(app, "load_prefs", lambda: dict(stored))
    monkeypatch.setattr(app, "save_prefs", lambda d: saved.update(d))
    win = StubWindow()
    app.remember_geometry(win, "viewer_geometry")
    return win.events.handlers


def test_minimising_does_not_overwrite_a_good_position(monkeypatch, stacked):
    """
    The write that produced the bug.

    Minimising fires moved(-32000, -32000) and a collapsed resize. Persisting
    either is what reopened the window off-screen, or at 560x420.
    """
    saved = {}
    stored = {"viewer_geometry": {"x": 300, "y": 200, "w": 1200, "h": 900}}
    h = _wire(monkeypatch, stored, saved)
    h["moved"](-32000, -32000)
    h["resized"](160, 28)
    h["closing"]()
    assert saved["viewer_geometry"] == {"x": 300, "y": 200, "w": 1200, "h": 900}


def test_a_resize_alone_keeps_the_remembered_position(monkeypatch, stacked):
    """
    How the file lost its x/y in the first place.

    State started empty each session, so a session that only ever resized wrote
    {w, h} over a good {x, y, w, h} - and every later launch re-centred.
    """
    saved = {}
    stored = {"viewer_geometry": {"x": 300, "y": -1200, "w": 1200, "h": 900}}
    h = _wire(monkeypatch, stored, saved)
    h["resized"](1000, 800)
    h["closing"]()
    assert saved["viewer_geometry"] == {"x": 300, "y": -1200, "w": 1000, "h": 800}


def test_an_offscreen_saved_rect_is_recentred(monkeypatch, stacked):
    monkeypatch.setattr(app, "load_prefs",
                        lambda: {"viewer_geometry": {"w": 900, "h": 700,
                                                     "x": -32000, "y": -32000}})
    monkeypatch.setattr(app, "primary_screen", lambda: (0, 0, 3440, 1440))
    x, y, w, h = app.viewer_geometry("nonexistent.exr")
    assert (x, y) != (-32000, -32000)
    assert app.geometry_is_sane(x, y, w, h)
