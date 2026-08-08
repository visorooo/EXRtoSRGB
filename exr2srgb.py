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
import sys
import threading
import traceback

import webview
from webview.dom import DOMEventHandler

import core

APP_NAME = "EXR → sRGB"
VERSION = "2.1"

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

    def _push_files(self):
        import json
        payload = json.dumps(self._entries_payload())
        if self._window:
            try:
                self._window.evaluate_js("window.onFilesChanged(%s)" % payload)
            except Exception:
                pass

    def _add_paths(self, paths, recurse=True):
        added = 0
        for p in paths:
            if not p:
                continue
            p = os.path.abspath(p)
            if os.path.isdir(p):
                for f in core.find_exrs(p, recurse):
                    if f not in self.files:
                        self.files.append(f)
                        added += 1
            elif p.lower().endswith(".exr") and p not in self.files:
                self.files.append(p)
                added += 1
        self.files.sort()
        self._regroup()
        return added

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
        }

    # -- lifecycle --------------------------------------------------------

    def init(self):
        return {
            "version": VERSION,
            "config": core.ACES_CONFIGS[core.DEFAULT_CONFIG_LABEL],
            "hint": "Drop .exr files or folders anywhere in this window.",
            "theme": load_prefs().get("theme", "dark"),
            "preview_size": load_prefs().get("preview_size", "m"),
        }

    def set_preview_size(self, size):
        if size in ("s", "m", "l"):
            save_prefs({"preview_size": size})
        return True

    def set_theme(self, theme):
        """Persist the light/dark choice. See load_prefs for why not localStorage."""
        if theme in ("dark", "light"):
            save_prefs({"theme": theme})
        return True

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

    def view(self, index, s, exposure=0.0, gamma=1.0, channel="rgb",
             max_px=512):
        """
        Render through the cached viewer session.

        The session keeps the decoded layer, so exposure, gamma and channel
        changes never re-read the file: measured 4.1x faster at 1080p and 5.6x
        on a 2160 square 80-channel frame.
        """
        paths = self._entry_paths(index)
        if not paths:
            return {"error": "nothing selected"}
        try:
            settings = self._settings(s)
            layer = settings.get("layer")
            self._viewer.load(paths[0], layer)
            uri, w, h = self._viewer.render(
                settings, exposure=float(exposure), gamma=float(gamma),
                channel=str(channel), max_px=int(max_px))
            full_w, full_h = self._viewer.size
            return {"uri": uri, "width": w, "height": h,
                    "full_width": full_w, "full_height": full_h,
                    "layer": self._viewer.layer}
        except Exception as e:
            return {"error": str(e)}

    def probe(self, index, u, v):
        """Linear scene values under the cursor, from the full-res layer."""
        paths = self._entry_paths(index)
        if not paths:
            return {}
        try:
            return self._viewer.sample(float(u), float(v)) or {}
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
            self._js("onLog", "Added %d file(s)." % n, "dim")
        self._push_files()
        return True

    def add_folder_dialog(self):
        res = self._window.create_file_dialog(webview.FOLDER_DIALOG)
        if res:
            n = self._add_paths(res)
            self._js("onLog", "Added %d file(s)." % n, "dim")
        self._push_files()
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

    def _run(self, files, settings):
        total = len(files)
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
            core.convert_many(files, settings,
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
            api._js("onLog", "Added %d file(s)." % n, "dim")
        if skipped:
            api._js("onLog",
                    "Ignored %d file(s) - only .exr is accepted" % skipped, "warn")
        api._push_files()

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
    api = Api()
    # Match the saved theme so the window does not flash the wrong colour before
    # the page has applied it. --panel-sunken in each scale.
    bg = "#f5f4f1" if load_prefs().get("theme") == "light" else "#242322"
    window = webview.create_window(
        "%s  ·  v%s" % (APP_NAME, VERSION),
        os.path.join(UI_DIR, "index.html"),
        js_api=api,
        width=1180,
        height=920,
        min_size=(940, 720),
        background_color=bg,
        text_select=False,
    )
    api._window = window
    window.events.loaded += lambda: attach_dnd(window, api)
    # On Windows the taskbar icon comes from the exe (spec: icon='app.ico');
    # this is what gives a source run the same mark instead of a Python one.
    icon = os.path.join(BASE_DIR, "app.ico")
    kwargs = {"icon": icon} if os.path.exists(icon) else {}
    webview.start(debug=bool(os.environ.get("EXR2SRGB_DEBUG")),
                  private_mode=False, **kwargs)


if __name__ == "__main__":
    main()
