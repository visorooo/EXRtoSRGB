#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXR -> sRGB  ·  ACES linear -> PNG / JPEG / TIFF

A batch converter for renders out of Blender Cycles, C4D Octane and C4D Redshift.
Colour is handled with OpenColorIO's built-in ACES configs - the same engine the
renderers use - so output matches the viewport instead of a naive gamma guess.
It also writes scene-linear 32-bit TIFF, and exports cryptomatte object mattes.

The UI is HTML/CSS rendered in a native window (pywebview -> WebView2 on Windows,
WebKit elsewhere). Python remains the application; the web layer is only the
face. All pixel work lives in core.py and is tested independently.

Dependencies:
    pip install OpenImageIO OpenColorIO pywebview

Build a standalone .exe on Windows:
    build_exe.bat
"""

import os
import re
import sys
import tempfile
import threading
import traceback

import webview
from webview.dom import DOMEventHandler

import core

APP_NAME = "EXR → sRGB"
VERSION = "3.1.1"

# _MEIPASS only exists in a frozen build; from source this is the repo folder.
BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
UI_DIR = os.path.join(BASE_DIR, "ui")


def _js_str(s):
    """Quote a Python string for injection into evaluate_js."""
    import json
    return json.dumps("" if s is None else str(s))


# ----------------------------------------------------------------------------
# Preferences
#
# Stored on the Python side rather than in localStorage: WebView2's profile
# persistence is what silently restored stale form state between launches (see
# applyDefaults in ui/app.js), so it is not somewhere to keep anything that
# matters. A small JSON file is predictable and survives a profile reset.
# ----------------------------------------------------------------------------

def prefs_path():
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_CONFIG_HOME")
            or os.path.expanduser("~"))
    return os.path.join(base, "EXRtoSRGB", "prefs.json")


def load_prefs():
    import json
    try:
        with open(prefs_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_prefs(update):
    import json
    prefs = load_prefs()
    prefs.update(update)
    path = prefs_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(prefs, fh, indent=2)
    except OSError:
        pass  # a preference that cannot be saved is not worth failing over
    return prefs


# ----------------------------------------------------------------------------
# File association
#
# Per-user only: everything goes under HKCU\\Software\\Classes, so no elevation
# is needed and nothing is changed for other accounts. It stays behind an
# explicit button rather than happening at install or first run - silently
# taking over a file type is not something a converter should do.
# ----------------------------------------------------------------------------

PROG_ID = "VISOR.EXRtoSRGB.exr"
# The name Windows shows in "Open with" and in Settings > Default apps, and the
# key that page reads our capabilities from. Both are part of being *choosable*
# as a default rather than merely registered - see choose_default.
APP_NAME = "EXR to sRGB"
CAPABILITIES_KEY = r"Software\VISOR\EXRtoSRGB\Capabilities"


def _exe_path():
    """The executable Windows would run for us - the frozen exe, or python."""
    return sys.executable


def _exe_command():
    """The command Windows should run, quoted, with the path placeholder."""
    if getattr(sys, "frozen", False):
        return '"%s" --view "%%1"' % sys.executable
    # from source, go through the interpreter and this script
    return '"%s" "%s" --view "%%1"' % (
        sys.executable, os.path.abspath(__file__))


def set_clipboard(text):
    """
    Put text on the clipboard.

    Done here rather than with navigator.clipboard: the UI is served from a
    file:// origin, which is not a secure context, so the JS clipboard API is
    unavailable. execCommand('copy') needs a selection and a user gesture and is
    unreliable inside a webview, so Win32 is the dependable route.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32

    # Every prototype is declared. Without argtypes ctypes marshals a Python
    # int as a C int, so a 64-bit HGLOBAL is truncated - and above 2^31 it does
    # not truncate quietly, it raises "int too long to convert". That made every
    # copy on this window fail while looking, from the JS side, like a call that
    # simply returned nothing.
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.restype = wintypes.BOOL
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalFree.restype = wintypes.HGLOBAL
    k32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    u32.OpenClipboard.restype = wintypes.BOOL
    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.EmptyClipboard.restype = wintypes.BOOL
    u32.CloseClipboard.restype = wintypes.BOOL
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    buf = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buf)
    handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return False
    ptr = k32.GlobalLock(handle)
    if not ptr:
        k32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, buf, size)
    k32.GlobalUnlock(handle)

    # The clipboard is a single global lock and only one process may hold it at
    # a time, so OpenClipboard fails transiently whenever anything else is
    # touching it - a clipboard manager, another app, the window that just lost
    # focus. Retrying briefly is the documented remedy; without it a copy fails
    # at random and looks like the button not working.
    import time as _time
    for attempt in range(10):
        if u32.OpenClipboard(None):
            break
        _time.sleep(0.02 * (attempt + 1))
    else:
        k32.GlobalFree(handle)
        return False
    try:
        u32.EmptyClipboard()
        # Ownership passes to the clipboard on success; do not free after this.
        if not u32.SetClipboardData(CF_UNICODETEXT, handle):
            k32.GlobalFree(handle)
            return False
    finally:
        u32.CloseClipboard()
    return True


# ----------------------------------------------------------------------------
# Single instance
#
# Only the converter is limited. Viewers are deliberately unrestricted: opening
# several images side by side is the point, and each --view launch is its own
# process because that is what a double-click gives us.
# ----------------------------------------------------------------------------

MUTEX_NAME = "Local\\VISOR.EXRtoSRGB.converter"
_instance_mutex = None


def claim_single_instance():
    """
    True if this process owns the converter. False means one is already open.

    The handle is kept on a module global on purpose: letting it be collected
    would release the mutex and let a second window through.
    """
    global _instance_mutex
    if os.name != "nt":
        return True
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False,
                                                          MUTEX_NAME)
    return ctypes.GetLastError() != ERROR_ALREADY_EXISTS


def focus_existing_instance():
    """Bring the already-running converter forward instead of opening another."""
    if os.name != "nt":
        return False
    import ctypes
    u32 = ctypes.windll.user32
    title = "%s  ·  v%s" % (APP_NAME, VERSION)
    hwnd = u32.FindWindowW(None, title)
    if not hwnd:
        return False
    SW_RESTORE = 9
    u32.ShowWindow(hwnd, SW_RESTORE)
    u32.SetForegroundWindow(hwnd)
    return True


# ----------------------------------------------------------------------------
# Update check
#
# Asks GitHub what the latest release is. Deliberately small: no auto-download
# on launch, no telemetry, one unauthenticated request to a public endpoint, and
# every failure is silent - an offline machine or a rate-limited IP must never
# produce an error the user has to dismiss to use a converter.
# ----------------------------------------------------------------------------

RELEASES_API = "https://api.github.com/repos/visorooo/EXRtoSRGB/releases/latest"
RELEASES_PAGE = "https://github.com/visorooo/EXRtoSRGB/releases/latest"


def _version_tuple(text):
    """'v3.0.5' -> (3, 0, 5). Missing parts are zero, so 3.1 sorts under 3.1.0."""
    parts = re.findall(r"\d+", str(text or ""))
    nums = [int(p) for p in parts[:4]] or [0]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def check_for_update(timeout=6.0):
    """
    Return {'available', 'latest', 'current', 'url', 'asset'} or None.

    None means "could not tell" - offline, rate-limited, GitHub down. That is
    reported as nothing rather than as a problem, because a failed update check
    is not a thing the user did wrong.
    """
    import json as _json
    import urllib.request
    req = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json",
                 # GitHub rejects requests with no User-Agent.
                 "User-Agent": "EXRtoSRGB/%s" % VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception:                               # noqa: BLE001
        return None
    tag = data.get("tag_name") or ""
    if not tag:
        return None
    asset = ""
    for a in data.get("assets") or []:
        name = a.get("name") or ""
        if name.lower().endswith(".exe"):
            asset = a.get("browser_download_url") or ""
            break
    return {
        "available": _version_tuple(tag) > _version_tuple(VERSION),
        "latest": tag.lstrip("vV"),
        "current": VERSION,
        "url": data.get("html_url") or RELEASES_PAGE,
        "asset": asset,
    }


def refresh_shell():
    """
    Make Explorer notice a changed association or icon.

    SHChangeNotify alone updates the handler but frequently not the icon:
    Explorer caches icons per file type in its own database and will keep
    showing the previous one, which looks exactly like the registration having
    failed. `ie4uinit -show` rebuilds that cache, and is the documented way to
    do it without deleting iconcache*.db by hand.
    """
    try:
        import ctypes
        # SHCNE_ASSOCCHANGED, SHCNF_IDLIST
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)
    except Exception:
        pass
    try:
        import subprocess
        subprocess.run(["ie4uinit.exe", "-show"], timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


CONTEXT_KEY = r"Software\Classes\SystemFileAssociations\.exr\shell\EXRtoSRGB.Convert"

# label, format, bit depth, transfer
CONVERT_VERBS = [
    ("01png", "PNG · 16-bit", "png", 16, "display"),
    ("02png8", "PNG · 8-bit", "png", 8, "display"),
    ("03jpg", "JPEG · quality 95", "jpeg", 8, "display"),
    ("04tif", "TIFF · 16-bit", "tiff", 16, "display"),
    ("05tiflin", "TIFF · 32-bit scene-linear", "tiff", 32, "linear"),
]


def _persistent_icon(name):
    """
    A copy of an icon that outlives the process.

    In a one-file build BASE_DIR is a temp directory that is deleted on exit, so
    a registry entry pointing there shows a blank icon the moment the app
    closes. The copy lives beside the preferences instead.
    """
    src = os.path.join(BASE_DIR, name)
    dst = os.path.join(os.path.dirname(prefs_path()), name)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(src):
            with open(src, "rb") as a, open(dst, "wb") as b:
                b.write(a.read())
        return dst if os.path.exists(dst) else src
    except OSError:
        return src


def _convert_command(fmt, bits, transfer):
    exe = ('"%s"' % sys.executable if getattr(sys, "frozen", False)
           else '"%s" "%s"' % (sys.executable, os.path.abspath(__file__)))
    return '%s --convert %s --bits %d --transfer %s "%%1"' % (
        exe, fmt, bits, transfer)


def context_menu_state():
    if os.name != "nt":
        return False
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONTEXT_KEY).Close()
        return True
    except OSError:
        return False


def set_context_menu(enable):
    """
    Add or remove the right-click "Convert to sRGB" submenu.

    Registered under SystemFileAssociations rather than our own ProgID, so the
    entries appear on .exr files whatever application owns the file type - you
    do not have to make this the default viewer to get the convert commands.
    """
    if os.name != "nt":
        raise RuntimeError("context menu is Windows-only")
    import winreg

    def nuke(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                while True:
                    try:
                        nuke(path + "\\" + winreg.EnumKey(k, 0))
                    except OSError:
                        break
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    if not enable:
        nuke(CONTEXT_KEY)
        refresh_shell()
        return False

    icon = _persistent_icon("exr.ico")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CONTEXT_KEY) as k:
        winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, "Convert to sRGB")
        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, icon)
        # An empty SubCommands turns the child "shell" key into a flyout.
        winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
    for key, label, fmt, bits, transfer in CONVERT_VERBS:
        base = "%s\\shell\\%s" % (CONTEXT_KEY, key)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, label)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + "\\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ,
                              _convert_command(fmt, bits, transfer))
    refresh_shell()
    return True


def convert_cli(path, fmt="png", bits=16, transfer="display", layer=None):
    """
    Headless convert, for the right-click menu. No window, no prompts.

    Errors surface as a message box rather than a silent failure, because there
    is no console to print to when the shell launches this.
    """
    config = core.ACES_CONFIGS[core.DEFAULT_CONFIG_LABEL]
    display = core.default_display(config)
    settings = {
        "config": config,
        "src": "ACEScg",
        "display": display,
        "view": core.view_for(config, display, True),
        "format": fmt,
        "quality": 95,
        "bits": int(bits),
        "alpha_mode": "keep",
        "layer": layer,
        "unpremult": True,
        "transfer": transfer,
        "out_dir": None,
        # The layer goes in the name, or exporting the same EXR twice from two
        # different layers writes the same file and the second wins silently.
        "suffix": core.layer_tag(layer)
                  + ("_linear" if transfer == "linear" else "_srgb"),
    }
    try:
        out, info = core.convert_one(path, settings)
        return out
    except Exception as e:
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, "Could not convert:\n\n%s\n\n%s" % (path, e),
                "EXR → sRGB", 0x10)
        except Exception:
            pass
        return None


# Explorer resolves .exr through this before it ever looks at
# HKCU\Software\Classes\.exr. Windows writes it whenever the user picks a
# default with "Open with", and Windows 11 keeps a second copy under
# UserChoiceLatest. Ignoring them is why ticking the toggle appeared to do
# nothing: the write succeeded, and the OS was reading somewhere else.
FILEEXTS = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
            r"\FileExts\.exr")
USER_CHOICE_KEYS = ("UserChoice", "UserChoiceLatest")


def _user_choice():
    """The ProgID Explorer is actually honouring, or None."""
    import winreg
    for sub in USER_CHOICE_KEYS:
        for path in ("%s\\%s" % (FILEEXTS, sub),
                     # Windows 11 nests it one deeper under UserChoiceLatest
                     "%s\\%s\\ProgId" % (FILEEXTS, sub)):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                    value = winreg.QueryValueEx(k, "ProgId")[0]
                    if value:
                        return value
            except OSError:
                continue
    return None


def _clear_user_choice():
    """
    Drop a *foreign* recorded default so our ProgID is reachable again.

    The Hash beside it is signed per user and cannot be forged, so there is no
    way to write a UserChoice - but deleting is allowed.

    **It must never delete our own.** Once the user has set .exr to this app in
    Settings, that UserChoice is the only thing making us the default: nothing
    else outranks an application that owns the type machine-wide. Deleting it
    hands .exr straight back to Photoshop. Until 3.1.1 this ran unconditionally
    at the end of every `set_association(True)`, so every install, upgrade and
    tick of the toggle silently undid the one thing that worked.
    """
    import winreg

    if _user_choice() == PROG_ID:
        return []

    def nuke(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                                winreg.KEY_ALL_ACCESS) as k:
                while True:
                    try:
                        nuke(path + "\\" + winreg.EnumKey(k, 0))
                    except OSError:
                        break
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            return True
        except OSError:
            return False

    return [sub for sub in USER_CHOICE_KEYS
            if nuke("%s\\%s" % (FILEEXTS, sub))]


def effective_handler():
    """
    The exe Windows would actually run for a .exr, straight from the shell.

    `AssocQueryString` is the API Explorer resolves a double-click through, so
    it is the only answer that counts. Reading our own registry keys back tells
    you what we wrote, which is a different question and was the reason the
    toggle read "on" while double-click opened Photoshop - see the note in
    CLAUDE.md. Returns a full path, or None if Windows would show the
    "How do you want to open this?" chooser instead.
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    ASSOCF_NOFIXUPS = 0x40
    ASSOCSTR_EXECUTABLE = 2
    fn = ctypes.windll.shlwapi.AssocQueryStringW
    fn.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
                   wintypes.LPCWSTR, wintypes.LPWSTR,
                   ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.LONG
    n = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(1024)
    if fn(ASSOCF_NOFIXUPS, ASSOCSTR_EXECUTABLE, ".exr", None, buf, ctypes.byref(n)):
        return None
    path = buf.value
    # The chooser itself is not a handler; reporting it as one would make the
    # toggle claim some application owns .exr when nothing does.
    if os.path.basename(path).lower() == "openwith.exe":
        return None
    return path


def association_state():
    """
    Is .exr currently pointing at us? Returns (associated, current_handler).

    Asks the shell rather than reading back our own keys. Writing
    `HKCU\\Software\\Classes\\.exr` is necessary but *not sufficient*: when
    another application already owns .exr machine-wide, Windows does not treat
    a per-user class registration as a default, and the only per-user override
    it honours is a UserChoice - which is hash-signed and cannot be written by
    us. So the honest state is whatever `effective_handler` says.
    """
    if os.name != "nt":
        return False, None
    handler = effective_handler()
    if handler:
        mine = os.path.normcase(os.path.abspath(handler)) == \
            os.path.normcase(os.path.abspath(_exe_path()))
        return mine, handler
    return False, _user_choice()


def _sample_exr():
    """
    Some real .exr on disk, for the "Open with" dialog to be about.

    `SHOpenWithDialog` takes a file, not an extension. The installer ships one
    next to the exe; from source it is in docs/. If neither is there a 1x1 is
    written to the temp folder - the dialog only ever looks at the suffix.
    """
    for candidate in (os.path.join(BASE_DIR, "sample", "sample_render.exr"),
                      os.path.join(BASE_DIR, "docs", "sample_render.exr")):
        if os.path.exists(candidate):
            return candidate
    tmp = os.path.join(tempfile.gettempdir(), "EXRtoSRGB-associate.exr")
    if not os.path.exists(tmp):
        import numpy as np
        import OpenImageIO as oiio
        spec = oiio.ImageSpec(1, 1, 3, "half")
        out = oiio.ImageOutput.create(tmp)
        out.open(tmp, spec)
        out.write_image(np.zeros((1, 1, 3), dtype="float32"))
        out.close()
    return tmp


def _is_windows_11():
    """Build 22000 is the 10-to-11 boundary; the association UI changed there."""
    try:
        return sys.getwindowsversion().build >= 22000
    except Exception:                               # noqa: BLE001
        return False


def choose_default(hwnd=0):
    """
    Hand the user to whichever UI their Windows lets set a default.

    Since Windows 8 the default handler for an extension is `UserChoice`, and
    the Hash beside it is signed per user. No application can write it, and one
    written without a valid hash makes Windows discard the association
    entirely. Writing HKCU\\Software\\Classes\\.exr is enough only while nothing
    else claims .exr; the moment Photoshop, After Effects or another viewer has
    registered it machine-wide, our registration is outranked and inert. That
    is why the installer's checkbox appeared to do nothing.

    **On Windows 11 `SHOpenWithDialog` no longer sets defaults.** Called with
    `OAIF_FORCE_REGISTRATION` it puts up a message box reading "To change your
    default apps, go to Settings > Apps > Default apps" and returns - measured,
    not assumed. So 11 gets the Settings page, deep-linked to our entry, which
    is why `set_association` bothers to write Capabilities and
    RegisteredApplications: without them that page has no row to land on.

    Windows 10 keeps the dialog, which is one click instead of four.

    Returns True if .exr ends up pointing at us - which on Windows 11 it will
    not yet, because the user is still in Settings when this returns.
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    if _is_windows_11():
        # `registeredAppUser=` is accepted but does not scroll to a desktop
        # app's row - measured, the page opens at the top of an alphabetical
        # list of every application on the machine. So there is no point
        # pretending: open the page and let the caller say which box to type
        # `.exr` into, which is the fast route anyway.
        try:
            os.startfile("ms-settings:defaultapps")
        except OSError:
            return False
        return association_state()[0]

    class OPENASINFO(ctypes.Structure):
        _fields_ = [("pcszFile", wintypes.LPCWSTR),
                    ("pcszClass", wintypes.LPCWSTR),
                    ("oaifInFlags", ctypes.c_int)]

    OAIF_ALLOW_REGISTRATION = 0x01
    OAIF_REGISTER_EXT = 0x02
    OAIF_FORCE_REGISTRATION = 0x08
    # No OAIF_EXEC: this is about setting the default, not opening the file.
    info = OPENASINFO(_sample_exr(), None,
                      OAIF_ALLOW_REGISTRATION | OAIF_REGISTER_EXT
                      | OAIF_FORCE_REGISTRATION)
    fn = ctypes.windll.shell32.SHOpenWithDialog
    fn.argtypes = [wintypes.HWND, ctypes.POINTER(OPENASINFO)]
    fn.restype = wintypes.LONG
    fn(hwnd, ctypes.byref(info))
    refresh_shell()
    return association_state()[0]


def repair_association():
    """
    Re-point our own registration at the exe that is running.

    The command records an absolute path, and the filename carries the version,
    so every upgrade leaves the previous registration aimed at a file that is no
    longer there: double-click stops working, and the natural next move - picking
    the app by hand in "Open with" - writes a UserChoice that overrides us and
    uses the application icon rather than the document one.

    Only ever rewrites a registration that is already ours, and only when the
    recorded path differs from the running one, so it cannot take a file type
    from another application or undo a deliberate change.
    """
    if os.name != "nt":
        return False
    import winreg
    # Gated on our registration existing, not on our being the current default.
    # Those came apart in 3.1.1 when association_state started asking the shell:
    # on a machine where Photoshop owns .exr we are registered but not default,
    # and that is exactly when the recorded path most needs to be right - it is
    # what the user is about to pick in the "Open with" dialog.
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                       r"Software\Classes\%s" % PROG_ID))
    except OSError:
        return False
    key = r"Software\Classes\%s\shell\open\command" % PROG_ID
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            current = winreg.QueryValueEx(k, "")[0]
    except OSError:
        current = ""
    wanted = _exe_command()
    if current == wanted:
        return False
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, wanted)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes\%s\DefaultIcon" % PROG_ID) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _persistent_icon("exr.ico"))
        refresh_shell()
        return True
    except OSError:
        return False


def set_association(enable):
    """Register or remove the .exr handler for the current user."""
    if os.name != "nt":
        raise RuntimeError("file association is Windows-only")
    import winreg

    if not enable:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Classes\%s\shell\open\command" % PROG_ID)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Classes\%s\shell\open" % PROG_ID)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Classes\%s\shell" % PROG_ID)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Classes\%s\DefaultIcon" % PROG_ID)
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Classes\%s" % PROG_ID)
        except OSError:
            pass
        # Only clear .exr if it is still pointing at us.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\.exr", 0,
                                winreg.KEY_ALL_ACCESS) as k:
                if winreg.QueryValueEx(k, "")[0] == PROG_ID:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "")
        except OSError:
            pass
        # Take the "choosable" half back out too, or the app keeps a row in
        # Settings > Default apps and a line in "Open with" after uninstall.
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Classes\.exr\OpenWithProgids", 0,
                                winreg.KEY_ALL_ACCESS) as k:
                winreg.DeleteValue(k, PROG_ID)
        except OSError:
            pass
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\RegisteredApplications", 0,
                                winreg.KEY_ALL_ACCESS) as k:
                winreg.DeleteValue(k, APP_NAME)
        except OSError:
            pass
        app_key = r"Software\Classes\Applications\%s" % \
            os.path.basename(_exe_path())
        for path in (CAPABILITIES_KEY + r"\FileAssociations",
                     CAPABILITIES_KEY,
                     r"Software\VISOR\EXRtoSRGB",
                     app_key + r"\shell\open\command",
                     app_key + r"\shell\open",
                     app_key + r"\shell",
                     app_key + r"\SupportedTypes",
                     app_key):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except OSError:
                pass
        refresh_shell()
        return False

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Classes\%s" % PROG_ID) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "OpenEXR image")
    # The .exr document icon, not the application icon - a file and the app
    # that opens it should not look identical in Explorer.
    icon = _persistent_icon("exr.ico")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Classes\%s\DefaultIcon" % PROG_ID) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, icon)
    with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Classes\%s\shell\open\command" % PROG_ID) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _exe_command())
    # Everything below is what makes us *choosable* rather than merely
    # registered. Without it the app is not offered in Settings > Default apps
    # at all, and shows up in "Open with" as the bare filename - which is what
    # the user has to click, since on Windows 11 they are the only one who can
    # actually set the default. See choose_default.
    #
    # OpenWithProgids: puts us in the "Open with" list for .exr without
    # claiming the type. Additive, and the one piece that is safe whether or
    # not we end up as the default.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Classes\.exr\OpenWithProgids") as k:
        winreg.SetValueEx(k, PROG_ID, 0, winreg.REG_NONE, b"")

    # Applications\<exe>: the friendly name and the types we handle, so the
    # chooser says "EXR to sRGB" rather than "EXRtoSRGB.exe".
    app_key = r"Software\Classes\Applications\%s" % os.path.basename(_exe_path())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, app_key) as k:
        winreg.SetValueEx(k, "FriendlyAppName", 0, winreg.REG_SZ, APP_NAME)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          app_key + r"\SupportedTypes") as k:
        winreg.SetValueEx(k, ".exr", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          app_key + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, _exe_command())

    # Capabilities + RegisteredApplications: how an application declares itself
    # to Settings > Default apps. Without this entry the Settings page has no
    # row for us, so there is nowhere for the user to pick us even though the
    # class registration is perfect.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, CAPABILITIES_KEY) as k:
        winreg.SetValueEx(k, "ApplicationName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(k, "ApplicationDescription", 0, winreg.REG_SZ,
                          "View and convert ACES-linear EXR renders.")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          CAPABILITIES_KEY + r"\FileAssociations") as k:
        winreg.SetValueEx(k, ".exr", 0, winreg.REG_SZ, PROG_ID)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\RegisteredApplications") as k:
        winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, CAPABILITIES_KEY)

    # Whatever Windows recorded when a default was picked by hand sits in front
    # of everything above. Clearing a *foreign* one is allowed and is what makes
    # the rest reachable; ours is left alone - see _clear_user_choice.
    _clear_user_choice()

    # Claim .exr's class default last, then check with the shell whether it
    # actually took - and take it back out if it did not.
    #
    # This key is the one that gives .exr the aperture document icon, because
    # the icon comes off the ProgID. It is also a trap when it does not work:
    # Windows 11's Settings reads it to decide what to *display* as the current
    # default, so a claim that Explorer ignores makes Settings show us as the
    # default and grey out its "Set default" button - there is apparently
    # nothing to change - while double-click still opens Photoshop. That locks
    # the user out of the only UI that could fix it, and we would be the ones
    # doing the locking.
    #
    # Claiming it only works because of the OpenWithProgids, Capabilities and
    # Applications entries above; with the older, thinner registration the same
    # key resolved straight back to Photoshop. Rather than encode a rule about
    # when that holds, ask: write it, query the shell, keep it if we won.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          r"Software\Classes\.exr") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, PROG_ID)
    refresh_shell()
    if not association_state()[0]:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Classes\.exr") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "")
        refresh_shell()

    return True


# ----------------------------------------------------------------------------
# Window geometry
#
# pywebview centres on whatever it considers the primary display, which on a
# multi-monitor setup is not reliably where the user is looking. Positions are
# computed explicitly instead, and remembered per window kind so the app opens
# where it was left.
# ----------------------------------------------------------------------------

# Chrome above and below the image in the viewer: title bar row + footer row.
VIEWER_CHROME_H = 96
VIEWER_CHROME_W = 32


def primary_screen():
    """(x, y, width, height) of the display to open on."""
    try:
        screens = webview.screens
        # The origin screen is the primary one; on stacked layouts the others
        # have negative coordinates.
        for s in screens:
            if getattr(s, "x", 0) == 0 and getattr(s, "y", 0) == 0:
                return 0, 0, s.width, s.height
        s = screens[0]
        return getattr(s, "x", 0), getattr(s, "y", 0), s.width, s.height
    except Exception:
        return 0, 0, 1920, 1080


def centre_on_screen(w, h):
    sx, sy, sw, sh = primary_screen()
    return sx + max(0, (sw - w) // 2), sy + max(0, (sh - h) // 2)


def viewer_geometry(path):
    """
    Size the viewer to the image, capped to the screen.

    Fits the frame at up to 1:1 within a comfortable share of the display, so a
    small render opens small and a 4K plate opens as large as it usefully can
    rather than at a fixed default that suits neither.
    """
    saved = load_prefs().get("viewer_geometry") or {}
    if saved.get("w") and saved.get("h"):
        w, h = int(saved["w"]), int(saved["h"])
        if saved.get("x") is not None and saved.get("y") is not None:
            return int(saved["x"]), int(saved["y"]), w, h
        x, y = centre_on_screen(w, h)
        return x, y, w, h

    sx, sy, sw, sh = primary_screen()
    try:
        iw, ih = core.image_size(path)
    except Exception:
        iw, ih = 1280, 720

    max_w = int(sw * 0.78) - VIEWER_CHROME_W
    max_h = int(sh * 0.86) - VIEWER_CHROME_H
    # never upscale past 1:1 - a 512px render should not open full screen
    scale = min(max_w / iw, max_h / ih, 1.0)
    w = max(720, int(iw * scale) + VIEWER_CHROME_W)
    h = max(520, int(ih * scale) + VIEWER_CHROME_H)
    x, y = centre_on_screen(w, h)
    return x, y, w, h


def remember_geometry(window, key):
    """
    Persist size and position, so the window reopens where it was left.

    Saved on a short debounce after the last move or resize rather than only on
    close: `closing` does not fire if the process is killed or crashes, and
    losing the geometry in those cases is exactly when it is most annoying. The
    debounce keeps a drag from writing the file on every frame.
    """
    state = {}
    timer = {"t": None}

    def flush():
        if state.get("w") and state.get("h"):
            save_prefs({key: dict(state)})

    def schedule():
        if timer["t"] is not None:
            timer["t"].cancel()
        timer["t"] = threading.Timer(0.8, flush)
        timer["t"].daemon = True
        timer["t"].start()

    def on_resized(w, h):
        state["w"], state["h"] = int(w), int(h)
        schedule()

    def on_moved(x, y):
        state["x"], state["y"] = int(x), int(y)
        schedule()

    def on_closing():
        if timer["t"] is not None:
            timer["t"].cancel()
        flush()

    try:
        window.events.resized += on_resized
        window.events.moved += on_moved
        window.events.closing += on_closing
    except Exception:
        pass  # geometry memory is a convenience, never a failure


class ViewerApi:
    """
    Bridge for the standalone viewer window.

    Deliberately small: one file, one session. The converter's Api is not reused
    because the viewer has no file list, no batch and no settings to gather.
    """

    def __init__(self, path):
        self._window = None
        self._path = path
        self._session = core.ViewerSession()
        self._layers = []
        # The comparison image. A second session rather than reloading one,
        # because the whole point is flipping between them without a decode.
        self._b_path = None
        self._b_session = None

    def _settings(self):
        prefs = load_prefs()
        config = prefs.get("view_config") or core.ACES_CONFIGS[
            core.DEFAULT_CONFIG_LABEL]
        display = core.default_display(config)
        return {
            "config": config,
            "src": "ACEScg",
            "display": display,
            "view": core.view_for(config, display, True),
            "unpremult": True,
            "transfer": "display",
        }

    def init(self):
        try:
            self._layers = core.probe_layers(self._path)
        except Exception:
            self._layers = []
        return {
            "path": self._path,
            "name": os.path.basename(self._path),
            "layers": self._layers,
            "theme": load_prefs().get("theme", "dark"),
        }

    def render(self, exposure=0.0, gamma=1.0, channel="rgb", layer=None):
        try:
            self._session.load(self._path, layer)
            # Generous fixed resolution: zoom and pan are CSS, so this only has
            # to be sharp enough for 1:1, not re-rendered as the user moves.
            uri, w, h = self._session.render(
                self._settings(), exposure=float(exposure), gamma=float(gamma),
                channel=str(channel), max_px=1600)
            full_w, full_h = self._session.size
            return {"uri": uri, "width": w, "height": h,
                    "full_width": full_w, "full_height": full_h,
                    "layer": self._session.layer}
        except Exception as e:
            return {"error": str(e)}

    # -- A/B comparison ---------------------------------------------------

    def pick_compare(self):
        """Choose the image to compare against."""
        if not self._window:
            return {"ok": False}
        picked = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("OpenEXR (*.exr)", "All files (*.*)"))
        if not picked:
            return {"ok": False}
        return self.load_compare(picked[0])

    def load_compare(self, path):
        try:
            path = os.path.abspath(path)
            sess = core.ViewerSession()
            sess.load(path)
            if sess.size != self._session.size:
                return {"ok": False,
                        "error": "%d x %d does not match this image's %d x %d"
                                 % (sess.size + self._session.size)}
            self._b_session = sess
            self._b_path = path
            return {"ok": True, "name": os.path.basename(path),
                    "layers": core.probe_layers(path)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def clear_compare(self):
        self._b_session = None
        self._b_path = None
        return {"ok": True}

    def render_b(self, exposure=0.0, gamma=1.0, channel="rgb", layer=None):
        """Render the comparison image with the same settings as A."""
        if self._b_session is None:
            return {"error": "nothing to compare with"}
        try:
            if layer is not None:
                self._b_session.load(self._b_path, layer)
            uri, w, h = self._b_session.render(
                self._settings(), exposure=float(exposure), gamma=float(gamma),
                channel=str(channel), max_px=1600)
            return {"uri": uri, "width": w, "height": h,
                    "layer": self._b_session.layer,
                    "name": os.path.basename(self._b_path)}
        except Exception as e:
            return {"error": str(e)}

    def render_diff(self, exposure=0.0, gamma=1.0):
        """|A - B| in linear, through the same display chain."""
        if self._b_session is None:
            return {"error": "nothing to compare with"}
        try:
            uri, w, h = core.render_difference(
                self._session, self._b_session, self._settings(),
                exposure=float(exposure), gamma=float(gamma), max_px=1600)
            return {"uri": uri, "width": w, "height": h}
        except Exception as e:
            return {"error": str(e)}

    def render_crop(self, x, y, w, h, exposure=0.0, gamma=1.0, channel="rgb"):
        """
        Re-render one region at source resolution, for zooming past 1:1.

        The base render is a fixed 1600px scaled by CSS, which is sharp enough
        up to 1:1 and interpolated beyond it - so on a 4K plate at 200% the
        viewer was showing invented pixels while `image-rendering: pixelated`
        made them look authoritative. This returns the real ones for the region
        actually on screen.
        """
        try:
            uri, rw, rh = self._session.render(
                self._settings(), exposure=float(exposure), gamma=float(gamma),
                channel=str(channel), crop=(x, y, w, h))
            return {"uri": uri, "width": rw, "height": rh,
                    "x": int(x), "y": int(y)}
        except Exception as e:
            return {"error": str(e)}

    def probe(self, u, v, exposure=0.0, gamma=1.0):
        try:
            return self._session.sample(float(u), float(v), self._settings(),
                                        float(exposure), float(gamma)) or {}
        except Exception:
            return {}

    def copy_text(self, text):
        """Put text on the clipboard from Python - file:// blocks the JS API."""
        return set_clipboard(str(text))

    def convert_presets(self):
        """
        The same list the right-click menu uses.

        Defined once in CONVERT_VERBS so the two menus cannot drift: an option
        that exists in Explorer but not here (or vice versa) would be a puzzle
        with no visible cause.
        """
        return [{"label": label, "format": fmt, "bits": bits,
                 "transfer": transfer}
                for _key, label, fmt, bits, transfer in CONVERT_VERBS]

    def convert(self, fmt, bits, transfer):
        """
        Convert the open file with one of the presets.

        Uses the layer currently on screen. Exporting the beauty while looking
        at Ambient Occlusion is the kind of wrong that looks right.
        """
        try:
            out = convert_cli(self._path, fmt, int(bits), transfer,
                              layer=self._session.layer)
            if not out:
                return {"ok": False, "error": "conversion failed"}
            return {"ok": True, "name": os.path.basename(out), "path": out}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def reveal(self, path):
        """Show a converted file in Explorer."""
        try:
            if os.name == "nt" and os.path.exists(path):
                import subprocess
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return True
        except Exception:
            return False

    def set_theme(self, theme):
        if theme in ("dark", "light"):
            save_prefs({"theme": theme})
        return True

    def close_window(self):
        if self._window:
            self._window.destroy()
        return True


def open_viewer(path, blocking=True):
    """Create a viewer window for one EXR, sized and placed sensibly."""
    path = os.path.abspath(path)
    api = ViewerApi(path)
    bg = "#f5f4f1" if load_prefs().get("theme") == "light" else "#242322"
    x, y, w, h = viewer_geometry(path)
    win = webview.create_window(
        "%s · EXR → sRGB" % os.path.basename(path),
        os.path.join(UI_DIR, "viewer.html"),
        js_api=api, width=w, height=h, x=x, y=y, min_size=(560, 420),
        background_color=bg, text_select=False)
    api._window = win
    remember_geometry(win, "viewer_geometry")
    return win


class Api:
    """
    The bridge exposed to JavaScript as window.pywebview.api.

    Every method here runs on a pywebview worker thread, so it may block. UI
    updates are pushed the other way with evaluate_js.
    """

    def __init__(self):
        self._window = None
        self.files = []          # flat list of every .exr path
        self.entries = []        # grouped view of the above
        self.cancel_flag = threading.Event()
        self.worker = None
        self.custom_configs = {}  # label -> path
        self._pick_state = None   # subsampled ID plane for ctrl-click picking
        self._pick_key = None
        self._viewer = core.ViewerSession()
        self._last_added = None
        self._last_expanded = 0

    # -- helpers ----------------------------------------------------------

    def _js(self, fn, *args):
        if not self._window:
            return
        payload = ", ".join(_js_str(a) if isinstance(a, str) else
                            ("true" if a is True else
                             "false" if a is False else str(a))
                            for a in args)
        try:
            self._window.evaluate_js("window.%s(%s)" % (fn, payload))
        except Exception:
            pass  # window closing mid-conversion

    def _regroup(self):
        self.entries = core.group_sequences(self.files)
        return self._entries_payload()

    def _entries_payload(self):
        out = []
        for e in self.entries:
            out.append({
                "kind": e["kind"],
                "label": e["label"],
                "count": e["count"],
                "dir": e["dir"],
                "first": e.get("first"),
                "last": e.get("last"),
            })
        return out

    def _push_files(self, select_label=None):
        """
        Hand the grouped list to the UI.

        `select_label` names the entry to select - the one just added, so a drop
        lands on the file that was dropped rather than leaving an older
        selection in place.
        """
        import json
        payload = json.dumps(self._entries_payload())
        if self._window:
            try:
                self._window.evaluate_js(
                    "window.onFilesChanged(%s, %s)"
                    % (payload, json.dumps(select_label)))
            except Exception:
                pass

    def _add_paths(self, paths, recurse=True):
        """Add files. Returns the number added; the new entry is on _last_added."""
        added = 0
        expanded = 0
        last = None
        for p in paths:
            if not p:
                continue
            p = os.path.abspath(p)
            if os.path.isdir(p):
                for f in core.find_exrs(p, recurse):
                    if f not in self.files:
                        self.files.append(f)
                        added += 1
                        last = f
            elif p.lower().endswith(".exr"):
                # A numbered frame means the run, not the one file.
                siblings = core.find_sequence_siblings(p)
                if len(siblings) > 1:
                    grew = [f for f in siblings if f not in self.files]
                    if grew:
                        self.files.extend(grew)
                        added += len(grew)
                        # "Pulled in" counts the frames the user did not drop.
                        # The dropped one only discounts if it was itself new -
                        # re-dropping a frame already on the list makes every
                        # sibling an extra.
                        expanded += len(grew) - (1 if p in grew else 0)
                    last = p
                elif p not in self.files:
                    self.files.append(p)
                    added += 1
                    last = p
        self.files.sort()
        self._regroup()
        label = None
        if last:
            for e in self.entries:
                paths = e["paths"] if e["kind"] == "sequence" else [e["path"]]
                if last in paths:
                    label = e["label"]
                    break
        self._last_added = label
        self._last_expanded = expanded
        return added

    def _added_message(self, n):
        extra = getattr(self, "_last_expanded", 0)
        if extra > 0:
            return ("Added %d file(s) — %d pulled in from the sequence."
                    % (n, extra))
        return "Added %d file(s)." % n

    def _entry_paths(self, index):
        if not (0 <= index < len(self.entries)):
            return []
        e = self.entries[index]
        return e["paths"] if e["kind"] == "sequence" else [e["path"]]

    def _settings(self, s):
        """Translate the UI's settings blob into what core expects."""
        config = s["config"]
        config = self.custom_configs.get(config, config)
        display = s["display"]
        return {
            "config": config,
            "src": s["src"],
            "display": display,
            "view": core.view_for(config, display, bool(s["tone"])),
            "format": s["format"],
            "quality": int(s["quality"]),
            "bits": int(s["bits"]),
            "alpha_mode": s["alpha_mode"],
            "layer": None if s["layer"] == core.LAYER_AUTO else s["layer"],
            "unpremult": bool(s["unpremult"]),
            "transfer": s.get("transfer", "display"),
            "out_dir": s["out_dir"] or None,
            "suffix": s["suffix"],
            # Consumed by _layer_passes and popped there, so it never reaches
            # core - which would reject an unknown key on a settings blob.
            "all_layers": bool(s.get("all_layers")),
        }

    # -- lifecycle --------------------------------------------------------

    def init(self):
        return {
            "version": VERSION,
            "config": core.ACES_CONFIGS[core.DEFAULT_CONFIG_LABEL],
            "hint": "Drop .exr files or folders anywhere in this window.",
            "theme": load_prefs().get("theme", "dark"),
            "preview_width": load_prefs().get("preview_width", 360),
        }

    def set_preview_width(self, width):
        """Remember the dragged preview width, clamped to something sane."""
        try:
            w = max(240, min(900, int(width)))
        except (TypeError, ValueError):
            return False
        save_prefs({"preview_width": w})
        return True

    def set_theme(self, theme):
        """Persist the light/dark choice. See load_prefs for why not localStorage."""
        if theme in ("dark", "light"):
            save_prefs({"theme": theme})
        return True

    def check_update(self):
        """
        Ask GitHub whether there is a newer release.

        Runs on a bridge worker thread, so a slow or unreachable network delays
        nothing the user is looking at. Returns {} when it cannot tell, and the
        UI shows nothing in that case - a failed update check is not news.
        """
        result = check_for_update()
        return result or {}

    def open_url(self, url):
        """
        Open a link in the real browser.

        The UI is a file:// page, so target="_blank" opens nothing; the shell has
        to be asked from this side. Only ever called with the release URL that
        came back from the update check.
        """
        url = str(url or "")
        if not url.startswith("https://github.com/visorooo/EXRtoSRGB"):
            return False
        try:
            import webbrowser
            webbrowser.open(url)
            return True
        except Exception:                           # noqa: BLE001
            return False

    def copy_text(self, text):
        """
        Put text on the clipboard from Python - file:// blocks the JS API.

        The viewer has had this since the probe grew a hex. The converter needs
        it too now that the preview copies values, and its absence was silent:
        the bridge call just rejected, so the copy did nothing and the "Copied"
        log line never ran either.
        """
        return set_clipboard(str(text))

    # -- presets ----------------------------------------------------------
    #
    # A studio uses one combination of config, display, format and bit depth for
    # months at a time, and re-picking it every launch is the sort of friction
    # that ends in someone shipping the wrong curve. Stored with the other
    # preferences, so there is one file to back up and no new format.

    def presets(self):
        """Saved settings blobs, newest name order preserved."""
        saved = load_prefs().get("presets") or {}
        return {"names": sorted(saved.keys()), "presets": saved}

    def save_preset(self, name, s):
        name = str(name).strip()
        if not name:
            return {"ok": False, "error": "a preset needs a name"}
        saved = load_prefs().get("presets") or {}
        # Deliberately not storing out_dir: a preset is how to convert, not
        # where to put it, and carrying a stale path across projects is worse
        # than re-picking one.
        blob = {k: v for k, v in dict(s).items() if k != "out_dir"}
        saved[name] = blob
        save_prefs({"presets": saved})
        self._js("onLog", "Saved preset %s." % name, "ok")
        return {"ok": True, "names": sorted(saved.keys())}

    def delete_preset(self, name):
        saved = load_prefs().get("presets") or {}
        if str(name) in saved:
            del saved[str(name)]
            save_prefs({"presets": saved})
        return {"ok": True, "names": sorted(saved.keys())}

    def config_list(self):
        configs = [{"value": name, "label": label}
                   for label, name in core.ACES_CONFIGS.items()]
        for label, path in self.custom_configs.items():
            configs.append({"value": label, "label": label})
        configs.append({"value": "__custom__", "label": "Custom config.ocio…"})
        return {
            "configs": configs,
            "current": core.ACES_CONFIGS[core.DEFAULT_CONFIG_LABEL],
        }

    def pick_config(self):
        """
        Load an external config.ocio.

        This is how ACES 1.2 gets in: it predates OCIO's built-in registry and
        exists only as a downloadable config, so it cannot be compiled into the
        exe the way the eight built-ins are.
        """
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=False,
            file_types=("OCIO config (*.ocio)", "All files (*.*)"))
        if not res:
            return {"ok": False}
        path = res[0]
        try:
            core.get_config(path)
        except Exception as e:
            self._js("onLog", "Could not load config: %s" % e, "err")
            return {"ok": False}
        label = "Custom · " + os.path.basename(os.path.dirname(path) or path)
        self.custom_configs[label] = path
        return {"ok": True, "config": label}

    def color_options(self, config):
        config = self.custom_configs.get(config, config)
        inputs = core.list_input_spaces(config)
        displays = core.list_displays(config)
        default_in = "ACEScg" if "ACEScg" in inputs else (inputs[0] if inputs else "")
        return {
            "inputs": inputs,
            "displays": displays,
            "default_input": default_in,
            "default_display": core.default_display(config),
        }

    def layers(self, index):
        paths = self._entry_paths(index)
        if not paths:
            return {"layers": []}
        try:
            return {"layers": core.probe_layers(paths[0])}
        except Exception as e:
            return {"layers": [], "error": str(e)}

    # -- viewer -----------------------------------------------------------

    def open_in_window(self, index):
        """
        Open the selected file in its own viewer window.

        A second pywebview window in this process rather than a new process:
        it opens immediately and shares nothing that would need syncing.
        Double-clicking an .exr takes the other route and launches the exe with
        --view, but both end up rendering ui/viewer.html.
        """
        paths = self._entry_paths(index)
        if not paths:
            return {"ok": False, "error": "nothing selected"}
        try:
            open_viewer(paths[0])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def association(self):
        """
        Current .exr handler state, for the settings toggle.

        Repairs a stale path on the way past: this runs at startup, which is the
        first moment after an upgrade that anything can notice the registration
        points at the previous filename.
        """
        if os.name != "nt":
            return {"supported": False, "associated": False}
        repaired = repair_association()
        associated, current = association_state()
        if repaired:
            self._js("onLog",
                     "Updated the .exr association to this version.", "dim")
        return {"supported": True, "associated": associated,
                "current": current or "", "repaired": repaired}

    def context_menu(self):
        if os.name != "nt":
            return {"supported": False, "enabled": False}
        return {"supported": True, "enabled": context_menu_state()}

    def set_context_menu(self, enable):
        try:
            state = set_context_menu(bool(enable))
            self._js("onLog",
                     "Added the right-click convert menu." if state
                     else "Removed the right-click convert menu.", "ok")
            return {"ok": True, "enabled": state}
        except Exception as e:
            self._js("onLog", "Could not change the context menu: %s" % e, "err")
            return {"ok": False, "error": str(e)}

    def set_association(self, enable):
        """
        Register, then verify with the shell rather than with our own keys.

        `set_association` writes everything it can, but on a machine where
        another application already owns .exr that registration is outranked
        and does nothing. Only Windows' own chooser can move the default, so
        when the shell still names someone else the dialog is offered instead
        of reporting a success that the next double-click contradicts.
        """
        try:
            set_association(bool(enable))
            if not enable:
                self._js("onLog", "Removed the .exr association.", "ok")
                return {"ok": True, "associated": False}
            done, handler = association_state()
            if done:
                self._js("onLog", "EXR files now open in this viewer.", "ok")
                return {"ok": True, "associated": True}
            done = choose_default()
            if done:
                self._js("onLog", "EXR files now open in this viewer.", "ok")
            else:
                who = os.path.basename(handler) if handler else "another app"
                self._js("onLog",
                         "Windows still opens .exr with %s. Pick EXR to sRGB in "
                         "the dialog to change it." % who, "warn")
            return {"ok": True, "associated": done}
        except Exception as e:
            self._js("onLog", "Could not change file association: %s" % e, "err")
            return {"ok": False, "error": str(e)}

    def view(self, index, s, exposure=0.0, gamma=1.0, channel="rgb",
             max_px=512, frame=0):
        """
        Render through the cached viewer session.

        The session keeps the decoded layer, so exposure, gamma and channel
        changes never re-read the file: measured 4.1x faster at 1080p and 5.6x
        on a 2160 square 80-channel frame.

        `frame` indexes into a sequence entry. Stepping does re-read, because a
        different frame is a different file - that cost is the sequence player's
        ceiling and is why it steps rather than plays.
        """
        paths = self._entry_paths(index)
        if not paths:
            return {"error": "nothing selected"}
        try:
            settings = self._settings(s)
            layer = settings.get("layer")
            i = max(0, min(int(frame), len(paths) - 1))
            self._viewer.load(paths[i], layer)
            uri, w, h = self._viewer.render(
                settings, exposure=float(exposure), gamma=float(gamma),
                channel=str(channel), max_px=int(max_px))
            full_w, full_h = self._viewer.size
            return {"uri": uri, "width": w, "height": h,
                    "full_width": full_w, "full_height": full_h,
                    "layer": self._viewer.layer,
                    "frame": i, "frames": len(paths),
                    "name": os.path.basename(paths[i])}
        except Exception as e:
            return {"error": str(e)}

    def probe(self, index, u, v, s=None, exposure=0.0, gamma=1.0):
        """
        Values under the cursor, from the full-res layer.

        Takes the settings so the reading carries the display colour and hex as
        well as the linear pixel - the same pair the viewer reports, and for the
        same reason: a hex has to be the colour on screen.
        """
        paths = self._entry_paths(index)
        if not paths:
            return {}
        try:
            settings = self._settings(s) if s else None
            return self._viewer.sample(float(u), float(v), settings,
                                       float(exposure), float(gamma)) or {}
        except Exception:
            return {}

    # -- cryptomatte ------------------------------------------------------

    def cryptomattes(self, index):
        """List the cryptomatte types in the selected entry, with their objects."""
        paths = self._entry_paths(index)
        if not paths:
            return {"types": []}
        try:
            found = core.probe_cryptomattes(paths[0])
        except Exception as e:
            return {"types": [], "error": str(e)}
        return {"types": [{
            "id": c["id"],
            "label": c["label"],
            "incomplete": c["incomplete"],
            "ranks": len(c["ranks"]),
            # sorted for a stable list; the manifest order is arbitrary
            "objects": sorted(c["objects"]),
        } for c in found]}

    def crypto_preview(self, index, crypto_id, selected, max_px=512):
        """
        Render the cryptomatte as coloured IDs, the way Nuke and AE show it.

        The subsampled ID plane is kept here so a later ctrl-click resolves by
        array lookup instead of re-reading a 466 MB file.
        """
        paths = self._entry_paths(index)
        if not paths:
            return {"error": "nothing selected"}
        try:
            crypto = next(c for c in core.probe_cryptomattes(paths[0])
                          if c["id"] == crypto_id)
            uri, state = core.crypto_preview(paths[0], crypto,
                                             max_px=int(max_px),
                                             selected=list(selected or []))
            self._pick_state = state
            self._pick_key = (paths[0], crypto_id)
            return {"uri": uri, "width": state["full_width"],
                    "height": state["full_height"]}
        except StopIteration:
            return {"error": "no such cryptomatte"}
        except Exception as e:
            return {"error": str(e)}

    def pick_object(self, index, crypto_id, u, v):
        """Resolve a normalised click on the ID preview to an object name."""
        paths = self._entry_paths(index)
        if not paths:
            return {"name": None}
        if getattr(self, "_pick_key", None) != (paths[0], crypto_id):
            # preview was for a different file; rebuild the lookup first
            self.crypto_preview(index, crypto_id, [])
        return {"name": core.object_at(getattr(self, "_pick_state", None),
                                       float(u), float(v))}

    def export_mattes(self, index, s, crypto_id, object_names):
        """Export the selected mattes. Runs on a worker like a conversion does."""
        paths = self._entry_paths(index)
        if not paths:
            self._js("onLog", "Nothing selected.", "warn")
            return False
        try:
            settings = self._settings(s)
        except Exception as e:
            self._js("onLog", "Bad settings: %s" % e, "err")
            return False
        settings["matte_mode"] = s.get("matte_mode", "associated")
        settings["matte_combine"] = bool(s.get("matte_combine"))

        fmt, _, bits = core.resolve_matte_output(settings)
        self._js("onLog", "mattes · %d object(s) · %s %d-bit · %s"
                 % (len(object_names), fmt.upper(), bits,
                    "combined" if settings["matte_combine"] else "one per object"),
                 "dim")
        self.cancel_flag.clear()
        self.worker = threading.Thread(
            target=self._run_mattes,
            args=(paths, settings, crypto_id, list(object_names)), daemon=True)
        self.worker.start()
        return True

    def _run_mattes(self, paths, settings, crypto_id, object_names):
        total = len(paths)
        ok = fail = 0
        for i, path in enumerate(paths):
            if self.cancel_flag.is_set():
                self._js("onLog", "Cancelled.", "warn")
                break
            try:
                for out in core.convert_mattes(path, settings, crypto_id,
                                               object_names):
                    self._js("onLog", "  OK    " + os.path.basename(out), "ok")
                    ok += 1
            except Exception as e:
                fail += 1
                self._js("onLog", "  FAIL  %s  ->  %s"
                         % (os.path.basename(path), e), "err")
            self._js("onProgress", i + 1, total)
        self._js("onDone", ok, fail, 0)

    # -- files ------------------------------------------------------------

    def add_files_dialog(self):
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("OpenEXR (*.exr)", "All files (*.*)"))
        if res:
            n = self._add_paths(res)
            self._js("onLog", self._added_message(n), "dim")
        self._push_files(getattr(self, "_last_added", None))
        return True

    def add_folder_dialog(self):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            n = self._add_paths(res)
            self._js("onLog", self._added_message(n), "dim")
        self._push_files(getattr(self, "_last_added", None))
        return True

    def pick_outdir(self):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        return res[0] if res else ""

    def clear(self):
        self.files = []
        self._regroup()
        self._push_files()
        return True

    def remove_entry(self, index):
        for p in self._entry_paths(index):
            if p in self.files:
                self.files.remove(p)
        return self._regroup()

    # -- preview ----------------------------------------------------------

    def preview(self, index, s, max_px=512):
        paths = self._entry_paths(index)
        if not paths:
            return {"error": "nothing selected"}
        try:
            settings = self._settings(s)
            uri, info = core.make_thumbnail(paths[0], settings,
                                            max_px=int(max_px))
            src = core.oiio.ImageInput.open(paths[0])
            spec = src.spec()
            full_w, full_h = spec.width, spec.height
            src.close()
            return {
                "uri": uri,
                "layer": info["layer"],
                "note": info["note"],
                "full_width": full_w,
                "full_height": full_h,
            }
        except Exception as e:
            return {"error": str(e)}

    # -- conversion -------------------------------------------------------

    def convert(self, s):
        try:
            settings = self._settings(s)
        except Exception as e:
            self._js("onLog", "Bad settings: %s" % e, "err")
            self._js("onDone", 0, 1, 0)
            return False

        files = list(self.files)
        self.cancel_flag.clear()
        # Report the format actually resolved, not the one requested - they
        # differ whenever the container cannot carry the requested depth.
        fmt, pix_fmt, bits = core.resolve_output(settings)
        if settings.get("transfer") == "linear":
            head = "scene-linear (no display transform) · %s %d-bit float" % (
                fmt.upper(), bits)
        else:
            head = "%s · %s · %s %d-bit" % (
                core.describe_config(settings["config"]), settings["view"],
                fmt.upper(), bits)
        if len(files) > 1:
            head += " · %d threads" % min(core.default_workers(), len(files))
        self._js("onLog", head, "dim")
        self.worker = threading.Thread(target=self._run, args=(files, settings),
                                       daemon=True)
        self.worker.start()
        return True

    def _layer_passes(self, files, settings):
        """
        The (layer, settings) pairs to run.

        One pass normally. With "every layer" ticked, one pass per layer of the
        first file, each carrying the layer in its suffix - without that they
        all resolve to the same filename and only the last survives. The layer
        list comes from the first file because a sequence shares its layers;
        anything missing one reports per file rather than silently falling back.
        """
        if not settings.pop("all_layers", False):
            return [settings]
        try:
            layers = core.probe_layers(files[0]) or [None]
        except Exception:
            return [settings]
        if len(layers) < 2:
            return [settings]
        passes = []
        for layer in layers:
            s = dict(settings)
            s["layer"] = layer
            s["suffix"] = core.layer_tag(layer) + settings.get("suffix", "")
            passes.append(s)
        self._js("onLog", "Converting %d layers, one file each." % len(layers),
                 "dim")
        return passes

    def _run(self, files, settings):
        passes = self._layer_passes(files, settings)
        total = len(files) * len(passes)
        counts = {"ok": 0, "fail": 0, "warned": 0, "done": 0}

        def on_result(i, path, out, info, err):
            counts["done"] += 1
            if err is not None:
                counts["fail"] += 1
                self._js("onLog", "  FAIL  %s  ->  %s"
                         % (os.path.basename(path), err), "err")
            elif out is None:
                return  # skipped by cancel
            else:
                counts["ok"] += 1
                self._js("onLog", "  OK    %s   [%s]"
                         % (os.path.basename(out), info["layer"] or "R,G,B"), "ok")
                if info["note"]:
                    counts["warned"] += 1
                    self._js("onLog", "        ! " + info["note"], "warn")
            self._js("onProgress", counts["done"], total)

        try:
            for s in passes:
                if self.cancel_flag.is_set():
                    break
                core.convert_many(files, s,
                                  on_result=on_result,
                                  should_stop=self.cancel_flag.is_set)
        except Exception as e:
            self._js("onLog", "Batch failed: %s" % e, "err")
            self._js("onLog", traceback.format_exc().splitlines()[-1], "dim")
        if self.cancel_flag.is_set():
            self._js("onLog", "Cancelled.", "warn")
        self._js("onDone", counts["ok"], counts["fail"], counts["warned"])

    def cancel(self):
        self.cancel_flag.set()
        return True


def attach_dnd(window, api):
    """
    Wire native file drop.

    The browser only exposes a File object, not a path - pywebview fills in
    `pywebviewFullPath` on the Python side, which is why this handler lives here
    and pushes the result back to JS rather than the other way round.
    """
    def on_drop(event):
        files = event.get("dataTransfer", {}).get("files", []) or []
        paths = [f["pywebviewFullPath"] for f in files if f.get("pywebviewFullPath")]
        api._js("onDragState", False)
        if files and not paths:
            # The event arrived but WebView2 did not attach real paths, so there
            # is nothing to open. Say so instead of appearing to ignore the drop.
            api._js("onLog",
                    "Dropped %d file(s) but Windows did not supply their paths - "
                    "use Add files instead." % len(files), "err")
            return
        if not paths:
            return
        exrs = [p for p in paths
                if os.path.isdir(p) or p.lower().endswith(".exr")]
        skipped = len(paths) - len(exrs)
        n = api._add_paths(exrs)
        if n:
            api._js("onLog", api._added_message(n), "dim")
        if skipped:
            api._js("onLog",
                    "Ignored %d file(s) - only .exr is accepted" % skipped, "warn")
        api._push_files(getattr(api, "_last_added", None))

    # Only `drop` is handled here. dragenter/dragover/dragleave are handled in
    # JS (see wireDrag in ui/app.js) because they must call preventDefault
    # synchronously, and because dragover fires continuously - routing it over
    # the bridge would be a stream of pointless IPC.
    #
    # Element.on() rather than `events.drop +=`: the events container is built
    # from the properties the DOM node actually reports, and a plain <div> does
    # not advertise a drop event, so the += form raises AttributeError.
    #
    # DOMEventHandler(prevent_default=True) matters: it injects preventDefault
    # into the JS shim, stopping WebView2 from navigating away to the dropped
    # file. Registering the listener is also what makes pywebview attach real
    # paths - it only forwards CoreWebView2File objects when a drop listener
    # exists.
    try:
        body = window.dom.get_element("body")
        if body is None:
            raise RuntimeError("no <body> element to bind drop to")
        body.on("drop", DOMEventHandler(on_drop, prevent_default=True))
    except Exception:
        # drag-and-drop is a convenience; the buttons still work without it
        traceback.print_exc()
        api._js("onLog", "Drag-and-drop unavailable - use Add files.", "warn")


def main():
    argv = sys.argv[1:]

    # `--register` / `--unregister`: what an installer calls, so the shell
    # integration has exactly one implementation rather than a second copy
    # written in the installer's own scripting language that then drifts.
    # Both are per-user by design, so the installer must invoke them as the
    # signed-in user rather than elevated - registering as the admin account
    # would associate .exr for the wrong person.
    # `--diag`: what the app thinks it is and where it thinks it lives. Exists
    # because a shell-integration bug looks identical from outside whether the
    # cause is the path, the hive or the permissions.
    if "--diag" in argv:
        import winreg
        print("version    : %s" % VERSION)
        print("frozen     : %s" % bool(getattr(sys, "frozen", False)))
        print("executable : %s" % sys.executable)
        print("base dir   : %s" % BASE_DIR)
        print("command    : %s" % _exe_command())
        print("prefs      : %s" % prefs_path())
        try:
            print("icon copy  : %s" % _persistent_icon("exr.ico"))
        except Exception as e:                      # noqa: BLE001
            print("icon copy  : FAILED %r" % e)
        print("handler    : %s" % (association_state(),))
        print("userchoice : %s" % _user_choice())
        for label, path, value in (
                ("progid", r"Software\Classes\%s" % PROG_ID, ""),
                ("command", r"Software\Classes\%s\shell\open\command" % PROG_ID, ""),
                # The submenu key carries MUIVerb and has no default value, so
                # asking for the default reports it missing when it is fine.
                ("verbs", CONTEXT_KEY, "MUIVerb")):
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
                    print("%-10s : %r" % (label, winreg.QueryValueEx(k, value)[0]))
            except OSError as e:
                print("%-10s : <%s>" % (label, e.__class__.__name__))
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                CONTEXT_KEY + r"\shell\01png\command") as k:
                print("first verb : %r" % winreg.QueryValueEx(k, "")[0])
        except OSError as e:
            print("first verb : <%s>" % e.__class__.__name__)
        return sys.exit(0)

    # `--choose-default`: what the installer runs after `--register`, and it is
    # a separate step because it is the only one that puts a window on screen.
    # Registering is silent and always safe; making ourselves the *default* can
    # only be done by the user clicking in Windows' own dialog, so it is asked
    # once at install time rather than never.
    if "--choose-default" in argv:
        if association_state()[0]:
            print("already the default for .exr")
            return sys.exit(0)
        return sys.exit(0 if choose_default() else 1)

    if "--register" in argv or "--unregister" in argv:
        on = "--register" in argv
        word = "registered" if on else "unregistered"
        failures = []
        # `--register assoc` / `--register context` select one part, so the
        # installer's two checkboxes are actually independent. Naming neither
        # does both, which is what uninstall wants and what the flag used to
        # mean on its own.
        wanted = [w for w in ("assoc", "context") if w in argv] \
            or ["assoc", "context"]
        parts = [p for p in (("association", set_association, "assoc"),
                             ("context menu", set_context_menu, "context"))
                 if p[2] in wanted]
        # Reported separately. They are independent registrations, and folding
        # both into one try meant a failure in either was indistinguishable -
        # the association updated, the verbs silently did not, and the command
        # still said "registered".
        for name, fn, _key in parts:
            try:
                fn(on)
                print("  %s %s" % (word, name))
            except Exception as e:                  # noqa: BLE001
                failures.append("%s: %s" % (name, e))
                print("  FAILED %s: %s" % (name, e), file=sys.stderr)
        if failures:
            return sys.exit(1)
        print("%s .exr for %s" % (word, os.environ.get("USERNAME", "this user")))
        # Registering is not the same as being the default - see choose_default.
        # Say so rather than letting the installer log read like a success the
        # next double-click will contradict.
        if on and "assoc" in wanted and not association_state()[0]:
            print("  note: Windows still opens .exr with %s - run "
                  "--choose-default to change it" % (association_state()[1],))
        return sys.exit(0)

    # `--cli ...`: the scriptable entry point. First, and before the single
    # instance guard, because a farm may run several at once and none of them
    # want a window.
    if "--cli" in argv:
        import cli
        return sys.exit(cli.main([a for a in argv if a != "--cli"]))

    # `--convert <fmt> --bits N --transfer T <path>`: the right-click verbs.
    # Headless - it writes the file and exits without ever creating a window.
    if "--convert" in argv:
        def opt(name, default):
            return argv[argv.index(name) + 1] if name in argv else default
        fmt = opt("--convert", "png")
        bits = int(opt("--bits", 16))
        transfer = opt("--transfer", "display")
        flags = {"--convert", "--bits", "--transfer"}
        rest = [a for i, a in enumerate(argv)
                if a not in flags and (i == 0 or argv[i - 1] not in flags)]
        for target in rest:
            if os.path.isfile(target):
                convert_cli(target, fmt, bits, transfer)
        return

    # `EXRtoSRGB.exe --view <path>` is what the shell runs on a double-click.
    # A bare path is accepted too, since that is what some launchers send.
    args = [a for a in sys.argv[1:] if a != "--view"]
    if ("--view" in sys.argv or (args and args[0].lower().endswith(".exr"))) \
            and args and os.path.isfile(args[0]):
        open_viewer(args[0])
        icon = os.path.join(BASE_DIR, "app.ico")
        # private_mode=True: no persistent WebView2 profile. Nothing here uses
        # localStorage - preferences live in a JSON file on the Python side - and
        # a persistent profile caches the UI, which after an update can serve a
        # stale viewer.html against a fresh viewer.js. That skew breaks wire()
        # on an element that no longer exists and blanks the whole window.
        webview.start(debug=bool(os.environ.get("EXR2SRGB_DEBUG")),
                      private_mode=True,
                      **({"icon": icon} if os.path.exists(icon) else {}))
        return

    # Only the converter is single-instance. --view and --convert returned
    # above, so viewers and shell conversions are never blocked.
    if not claim_single_instance():
        focus_existing_instance()
        return

    api = Api()
    # Match the saved theme so the window does not flash the wrong colour before
    # the page has applied it. --panel-sunken in each scale.
    bg = "#f5f4f1" if load_prefs().get("theme") == "light" else "#242322"
    saved = load_prefs().get("main_geometry") or {}
    w = int(saved.get("w") or 1180)
    h = int(saved.get("h") or 920)
    if saved.get("x") is not None and saved.get("y") is not None:
        x, y = int(saved["x"]), int(saved["y"])
    else:
        x, y = centre_on_screen(w, h)
    window = webview.create_window(
        "%s  ·  v%s" % (APP_NAME, VERSION),
        os.path.join(UI_DIR, "index.html"),
        js_api=api,
        width=w,
        height=h,
        x=x,
        y=y,
        min_size=(940, 720),
        background_color=bg,
        text_select=False,
    )
    api._window = window
    remember_geometry(window, "main_geometry")
    window.events.loaded += lambda: attach_dnd(window, api)
    # On Windows the taskbar icon comes from the exe (spec: icon='app.ico');
    # this is what gives a source run the same mark instead of a Python one.
    icon = os.path.join(BASE_DIR, "app.ico")
    kwargs = {"icon": icon} if os.path.exists(icon) else {}
    webview.start(debug=bool(os.environ.get("EXR2SRGB_DEBUG")),
                  private_mode=True, **kwargs)


if __name__ == "__main__":
    main()
