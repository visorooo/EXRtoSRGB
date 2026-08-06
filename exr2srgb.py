#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXR -> sRGB  ·  ACES linear -> display-ready PNG / JPEG

A batch converter for renders out of Blender Cycles, C4D Octane and C4D Redshift.
Colour is handled with OpenColorIO's built-in ACES configs - the same engine the
renderers use - so output matches the viewport instead of a naive gamma guess.

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

import core

APP_NAME = "EXR → sRGB"
VERSION = "2.0"

UI_DIR = os.path.join(
    getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "ui")


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
        }

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

    def preview(self, index, s):
        paths = self._entry_paths(index)
        if not paths:
            return {"error": "nothing selected"}
        try:
            settings = self._settings(s)
            uri, info = core.make_thumbnail(paths[0], settings, max_px=512)
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
        self._js("onLog", "%s · %s · %s"
                 % (core.describe_config(settings["config"]),
                    settings["view"], settings["format"].upper()), "dim")
        self.worker = threading.Thread(target=self._run, args=(files, settings),
                                       daemon=True)
        self.worker.start()
        return True

    def _run(self, files, settings):
        total = len(files)
        ok = fail = warned = 0
        for i, path in enumerate(files):
            if self.cancel_flag.is_set():
                self._js("onLog", "Cancelled.", "warn")
                break
            try:
                out, info = core.convert_one(path, settings)
                ok += 1
                layer = info["layer"] or "R,G,B"
                self._js("onLog", "  OK    %s   [%s]"
                         % (os.path.basename(out), layer), "ok")
                if info["note"]:
                    warned += 1
                    self._js("onLog", "        ! " + info["note"], "warn")
            except Exception as e:
                fail += 1
                self._js("onLog", "  FAIL  %s  ->  %s"
                         % (os.path.basename(path), e), "err")
                self._js("onLog", "        "
                         + traceback.format_exc().splitlines()[-1], "dim")
            self._js("onProgress", i + 1, total)
        self._js("onDone", ok, fail, warned)

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
        paths = []
        for f in event.get("dataTransfer", {}).get("files", []):
            p = f.get("pywebviewFullPath")
            if p:
                paths.append(p)
        api._js("onDragState", False)
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

    def on_over(event):
        api._js("onDragState", True)

    def on_leave(event):
        api._js("onDragState", False)

    # Element.on() rather than `events.drop +=`: the events container is built
    # from the properties the DOM node actually reports, and a plain <div> does
    # not advertise a drop event, so the += form raises AttributeError.
    try:
        body = window.dom.get_element("body")
        if body is None:
            return
        body.on("dragenter", on_over)
        body.on("dragover", on_over)
        body.on("dragleave", on_leave)
        body.on("drop", on_drop)
    except Exception:
        # drag-and-drop is a convenience; the buttons still work without it
        traceback.print_exc()


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
        height=880,
        min_size=(940, 720),
        background_color=bg,
        text_select=False,
    )
    api._window = window
    window.events.loaded += lambda: attach_dnd(window, api)
    webview.start(debug=bool(os.environ.get("EXR2SRGB_DEBUG")), private_mode=False)


if __name__ == "__main__":
    main()
