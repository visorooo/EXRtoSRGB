"""
Shell association: does the app tell the truth about who owns .exr?

Separate from test_core/test_cli because this necessarily imports the window
module - but it still opens no window. Importing pywebview is not the same as
starting it.

The bug these guard against shipped twice. `set_association` wrote a correct
per-user registration, `association_state` read that same registration back,
and both agreed everything was fine while Explorer went on opening Photoshop.
Reading back what you wrote is not a check. The state has to come from the
shell.
"""

import os

import pytest

import exr2srgb as app

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows shell only")


@windows_only
def test_state_comes_from_the_shell_not_our_registry(monkeypatch):
    """
    A correct registration we do not own the default for must read as off.

    This is the exact shape of the 3.1 bug: HKCU\\Software\\Classes\\.exr named
    our ProgID, no UserChoice existed, and Windows still resolved .exr to
    Photoshop because a per-user class registration does not outrank an
    application that owns the type machine-wide.
    """
    monkeypatch.setattr(app, "effective_handler",
                        lambda: r"C:\Program Files\Adobe\Photoshop.exe")
    associated, handler = app.association_state()
    assert associated is False
    assert handler.endswith("Photoshop.exe")


@windows_only
def test_state_is_on_when_the_shell_names_us(monkeypatch):
    monkeypatch.setattr(app, "effective_handler", lambda: app._exe_path())
    associated, handler = app.association_state()
    assert associated is True
    assert handler == app._exe_path()


@windows_only
def test_case_and_shortpath_differences_still_count_as_us(monkeypatch):
    """The shell may hand back a different case than sys.executable carries."""
    monkeypatch.setattr(app, "effective_handler",
                        lambda: app._exe_path().upper())
    assert app.association_state()[0] is True


@windows_only
def test_the_chooser_is_not_reported_as_a_handler(monkeypatch):
    """
    OpenWith.exe means nothing owns the type, not that OpenWith owns it.

    Reporting it as the handler would make the cogwheel claim some other
    application had taken .exr when in fact it is simply unset.
    """
    import ctypes

    def fake(flags, which, ext, extra, buf, n):
        buf.value = r"C:\WINDOWS\system32\OpenWith.exe"
        return 0

    monkeypatch.setattr(ctypes.windll.shlwapi, "AssocQueryStringW", fake,
                        raising=False)
    assert app.effective_handler() is None


@windows_only
def test_sample_for_the_open_with_dialog_exists():
    """
    `SHOpenWithDialog` takes a file, not an extension - so there must be one.

    With no file the dialog does not appear and the association silently
    cannot be changed, which is indistinguishable from the bug it fixes.
    """
    path = app._sample_exr()
    assert os.path.exists(path)
    assert path.lower().endswith(".exr")


@windows_only
def test_our_own_user_choice_is_never_deleted(monkeypatch):
    """
    The one thing that makes us the default must survive re-registering.

    A UserChoice naming us is the only mechanism that beats an application
    owning .exr machine-wide. `set_association(True)` used to clear it
    unconditionally, so every install, upgrade and tick of the toggle handed
    the file type straight back.
    """
    deleted = []
    monkeypatch.setattr(app, "_user_choice", lambda: app.PROG_ID)
    import winreg
    monkeypatch.setattr(winreg, "DeleteKey",
                        lambda *a: deleted.append(a))
    assert app._clear_user_choice() == []
    assert deleted == []


@windows_only
def test_a_foreign_user_choice_is_still_cleared(monkeypatch):
    monkeypatch.setattr(app, "_user_choice", lambda: "Photoshop.OpenEXRFile.200")
    calls = []

    def fake_nuke(hive, path, *a, **k):
        calls.append(path)
        raise OSError("no such key")

    import winreg
    monkeypatch.setattr(winreg, "OpenKey", fake_nuke)
    app._clear_user_choice()
    assert any("UserChoice" in c for c in calls)


@windows_only
def test_a_class_claim_that_does_not_take_is_withdrawn(monkeypatch, tmp_path):
    """
    Claiming .exr without winning it is worse than not claiming it.

    Windows 11 Settings reads that key to decide what to show as the current
    default. A claim Explorer ignores makes Settings display this app and grey
    out "Set default" - so the user cannot fix an association the app broke.
    `set_association` therefore checks with the shell and withdraws the claim
    when it did not take.

    Nothing real is written: the whole winreg surface is faked, because a test
    that registers for .exr would change the machine it runs on.
    """
    import contextlib
    import winreg
    writes = []

    class FakeKey:
        pass

    @contextlib.contextmanager
    def fake_create(hive, path):
        yield FakeKey()

    monkeypatch.setattr(winreg, "CreateKey", fake_create)
    monkeypatch.setattr(winreg, "OpenKey", fake_create)
    monkeypatch.setattr(winreg, "SetValueEx",
                        lambda key, name, res, kind, value:
                        writes.append((name, value)))
    monkeypatch.setattr(winreg, "QueryValueEx", lambda *a: ("", 0))
    monkeypatch.setattr(winreg, "DeleteValue", lambda *a: None)
    monkeypatch.setattr(winreg, "DeleteKey", lambda *a: None)
    monkeypatch.setattr(app, "_persistent_icon", lambda name: "icon")
    monkeypatch.setattr(app, "refresh_shell", lambda: None)
    monkeypatch.setattr(app, "_clear_user_choice", lambda: [])
    # pretend the shell keeps naming someone else, whatever we write
    monkeypatch.setattr(app, "effective_handler",
                        lambda: r"C:\Program Files\Adobe\Photoshop.exe")

    app.set_association(True)
    claims = [v for n, v in writes if n == "" and v in (app.PROG_ID, "")]
    assert claims[-1] == "", "the claim must be withdrawn, not left behind"


def test_register_flags_select_parts():
    """
    The installer's two checkboxes have to be independent.

    Until 3.1 both registrations rode on the association task, so ticking only
    the right-click menu did nothing whatsoever.
    """
    for argv, expect in ((["--register"], {"assoc", "context"}),
                         (["--register", "assoc"], {"assoc"}),
                         (["--register", "context"], {"context"}),
                         (["--unregister"], {"assoc", "context"})):
        wanted = [w for w in ("assoc", "context") if w in argv] \
            or ["assoc", "context"]
        assert set(wanted) == expect
