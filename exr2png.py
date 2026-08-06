#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EXR -> PNG  (ACES linear -> sRGB / Rec.709)
A small batch converter for renders out of Blender Cycles, C4D Octane and C4D Redshift.

Color is handled with OpenColorIO's built-in ACES configs (the same engine your
renderers use), so the output matches what you see in the viewport instead of a
naive gamma guess.

Dependencies (pip):
    pip install OpenImageIO OpenColorIO
(numpy ships with OpenImageIO. tkinter ships with Python.)

Build a standalone .exe on Windows:
    pip install pyinstaller
    pyinstaller --onefile --windowed --name "EXRtoPNG" exr2png.py
    (optional icon:  --icon app.ico )
"""

import os
import sys
import queue
import threading
import traceback

# ---- third-party imaging/color libs ----
try:
    import numpy as np
except Exception as e:  # pragma: no cover
    np = None
    _NUMPY_ERR = e

try:
    import OpenImageIO as oiio
    _OIIO_OK = True
except Exception as e:
    _OIIO_OK = False
    _OIIO_ERR = e

try:
    import PyOpenColorIO as OCIO
    _OCIO_OK = True
except Exception as e:
    _OCIO_OK = False
    _OCIO_ERR = e

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Optional drag-and-drop (nice to have, not required)
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_OK = True
except Exception:
    _DND_OK = False


APP_NAME = "EXR \u2192 PNG  \u00b7  ACES linear \u2192 sRGB"
VERSION = "1.0"

# Built-in OCIO ACES configs. 1.3 matches the output transform most current
# Blender / Octane / Redshift ACES viewports use; 2.0 is the newer look.
ACES_CONFIGS = {
    "ACES 1.3  (matches most viewports)": "cg-config-v2.2.0_aces-v1.3_ocio-v2.4",
    "ACES 2.0  (newest)": "cg-config-v4.0.0_aces-v2.0_ocio-v2.5",
}

# Input color spaces we expose (filtered against what the chosen config offers).
PREFERRED_INPUT_CS = [
    "ACEScg",
    "ACES2065-1",
    "Linear Rec.709 (sRGB)",
    "Linear Rec.2020",
    "Linear P3-D65",
]

# ----------------------------------------------------------------------------
# Color core
# ----------------------------------------------------------------------------

_config_cache = {}
_proc_cache = {}


def get_config(config_name):
    if config_name not in _config_cache:
        _config_cache[config_name] = OCIO.Config.CreateFromBuiltinConfig(config_name)
    return _config_cache[config_name]


def get_processor(config_name, src, display, view):
    key = (config_name, src, display, view)
    if key not in _proc_cache:
        cfg = get_config(config_name)
        dvt = OCIO.DisplayViewTransform()
        dvt.setSrc(src)
        dvt.setDisplay(display)
        dvt.setView(view)
        _proc_cache[key] = cfg.getProcessor(dvt).getDefaultCPUProcessor()
    return _proc_cache[key]


def list_displays(config_name):
    cfg = get_config(config_name)
    return list(cfg.getDisplays())


def default_display(config_name):
    return get_config(config_name).getDefaultDisplay()


def list_input_spaces(config_name):
    cfg = get_config(config_name)
    names = set(cfg.getColorSpaceNames())
    return [c for c in PREFERRED_INPUT_CS if c in names]


def view_for(config_name, display, tone_mapped):
    """Return the OCIO view name for this display."""
    cfg = get_config(config_name)
    views = list(cfg.getViews(display))
    if tone_mapped:
        # The config's default view IS the ACES output transform (RRT+ODT).
        return cfg.getDefaultView(display)
    # plain colorspace conversion, no filmic tone curve
    for cand in ("Un-tone-mapped", "Raw"):
        if cand in views:
            return cand
    return views[0] if views else cfg.getDefaultView(display)


def _rgba_indices(channelnames):
    """Locate R,G,B,(A) inside an EXR's channel list (handles AOV-heavy files)."""
    low = [n.split(".")[-1].lower() for n in channelnames]

    def fi(c):
        return low.index(c) if c in low else None

    r, g, b = fi("r"), fi("g"), fi("b")
    a = fi("a")
    if r is None or g is None or b is None:
        # fall back to the first channels
        r, g, b = 0, 1, 2
        a = 3 if len(channelnames) >= 4 else None
    return r, g, b, a


def read_exr(path):
    """Return (rgb float32 HxWx3 contiguous, alpha float32 HxW or None, W, H)."""
    src = oiio.ImageInput.open(path)
    if src is None:
        raise IOError("Could not open EXR: %s" % oiio.geterror())
    spec = src.spec()
    W, H, C = spec.width, spec.height, spec.nchannels
    names = list(spec.channelnames)
    pixels = src.read_image(format="float")
    src.close()
    arr = np.array(pixels, dtype=np.float32).reshape(H, W, C)
    ri, gi, bi, ai = _rgba_indices(names)
    rgb = np.ascontiguousarray(arr[..., [ri, gi, bi]])
    alpha = np.ascontiguousarray(arr[..., ai]) if ai is not None else None
    return rgb, alpha, W, H


def write_png(path, arr_uint, W, H, nchannels, fmt):
    spec = oiio.ImageSpec(W, H, nchannels, fmt)
    spec.attribute("oiio:ColorSpace", "sRGB")
    out = oiio.ImageOutput.create(path)
    if out is None:
        raise IOError("Could not create PNG writer: %s" % oiio.geterror())
    if not out.open(path, spec):
        raise IOError("Could not open PNG for writing: %s" % out.geterror())
    if not out.write_image(np.ascontiguousarray(arr_uint)):
        err = out.geterror()
        out.close()
        raise IOError("Failed writing PNG: %s" % err)
    out.close()


def convert_one(in_path, settings):
    """Convert a single EXR to PNG. Returns the output path."""
    rgb, alpha, W, H = read_exr(in_path)

    # Un-premultiply (associated -> straight alpha) BEFORE the display transform,
    # done in linear. Keeps edges correct and matches AE's interpretation.
    if alpha is not None and settings["unpremult"]:
        a = alpha
        mask = a > 1e-6
        if mask.any():
            rgb = rgb.copy()
            inv = np.empty_like(a)
            inv[mask] = 1.0 / a[mask]
            inv[~mask] = 0.0
            rgb *= inv[..., None]

    proc = get_processor(
        settings["config_name"], settings["src"], settings["display"], settings["view"]
    )
    buf = np.ascontiguousarray(rgb, dtype=np.float32)
    proc.apply(OCIO.PackedImageDesc(buf, W, H, 3))
    buf = np.clip(buf, 0.0, 1.0)

    keep_alpha = settings["keep_alpha"] and (alpha is not None)
    if keep_alpha:
        a = np.clip(alpha, 0.0, 1.0).astype(np.float32)
        out_f = np.dstack([buf, a])
        nch = 4
    else:
        out_f = buf
        nch = 3

    if settings["bits"] == 16:
        arr_uint = (out_f * 65535.0 + 0.5).astype(np.uint16)
        fmt = "uint16"
    else:
        arr_uint = (out_f * 255.0 + 0.5).astype(np.uint8)
        fmt = "uint8"

    out_dir = settings["out_dir"] or os.path.dirname(in_path)
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(in_path))[0]
    suffix = settings.get("suffix", "")
    out_path = os.path.join(out_dir, base + suffix + ".png")
    write_png(out_path, arr_uint, W, H, nch, fmt)
    return out_path


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

# VS Code-ish dark palette
BG = "#1e1e1e"
PANEL = "#252526"
FG = "#e6e6e6"
SUBTLE = "#9a9a9a"
ENTRY = "#333337"
BORDER = "#3c3c3c"
ACCENT = "#0e639c"
ACCENT_HI = "#1177bb"
OK = "#4ec96a"
ERR = "#f06b6b"


class App:
    def __init__(self, root):
        self.root = root
        self.files = []
        self.q = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()

        root.title("%s  v%s" % (APP_NAME, VERSION))
        root.configure(bg=BG)
        root.geometry("760x680")
        root.minsize(720, 620)

        self._build_style()
        self._build_ui()
        self._refresh_color_options()
        self.root.after(80, self._poll_queue)

    # ---- styling ----
    def _build_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background=BG, foreground=FG, fieldbackground=ENTRY,
                    bordercolor=BORDER, focuscolor=ACCENT)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Sub.TLabel", background=BG, foreground=SUBTLE)
        s.configure("Panel.TLabel", background=PANEL, foreground=FG)
        s.configure("Title.TLabel", background=BG, foreground=FG,
                    font=("Segoe UI Semibold", 14))
        s.configure("TButton", background=ENTRY, foreground=FG, borderwidth=0,
                    padding=6, focusthickness=0)
        s.map("TButton", background=[("active", BORDER)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    padding=8, font=("Segoe UI Semibold", 10))
        s.map("Accent.TButton", background=[("active", ACCENT_HI),
                                            ("disabled", "#2a4d63")])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TCombobox", fieldbackground=ENTRY, background=ENTRY,
                    foreground=FG, arrowcolor=FG, bordercolor=BORDER, padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", ENTRY)],
              foreground=[("readonly", FG)])
        s.configure("Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=ENTRY, bordercolor=BG)

    # ---- layout ----
    def _build_ui(self):
        pad = dict(padx=14, pady=8)

        header = ttk.Frame(self.root)
        header.pack(fill="x", **pad)
        ttk.Label(header, text="EXR \u2192 PNG converter", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="ACES linear \u2192 sRGB / Rec.709",
                  style="Sub.TLabel").pack(side="left", padx=(10, 0))

        # ---- file list ----
        files_frame = ttk.Frame(self.root, style="Panel.TFrame")
        files_frame.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        bar = ttk.Frame(files_frame, style="Panel.TFrame")
        bar.pack(fill="x", padx=10, pady=10)
        ttk.Button(bar, text="Add files\u2026", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="Add folder\u2026", command=self.add_folder).pack(side="left", padx=6)
        self.recurse_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="include subfolders", variable=self.recurse_var
                        ).pack(side="left", padx=6)
        ttk.Button(bar, text="Clear", command=self.clear_files).pack(side="right")

        list_wrap = tk.Frame(files_frame, bg=PANEL)
        list_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.listbox = tk.Listbox(
            list_wrap, bg=ENTRY, fg=FG, selectbackground=ACCENT,
            selectforeground="#ffffff", highlightthickness=0, borderwidth=0,
            activestyle="none", font=("Consolas", 9),
        )
        self.listbox.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_wrap, command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        if _DND_OK:
            try:
                self.listbox.drop_target_register(DND_FILES)
                self.listbox.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:
                pass

        hint = "Drag .exr files here, or use the buttons above." if _DND_OK \
            else "Use the buttons above to add .exr files."
        ttk.Label(files_frame, text=hint, style="Panel.TLabel"
                  ).pack(anchor="w", padx=10, pady=(0, 8))

        # ---- settings grid ----
        opts = ttk.Frame(self.root)
        opts.pack(fill="x", padx=14)
        for c in range(4):
            opts.columnconfigure(c, weight=1, uniform="opt")

        def label(txt, r, c):
            ttk.Label(opts, text=txt, style="Sub.TLabel").grid(
                row=r, column=c, sticky="w", padx=4, pady=(6, 0))

        def combo(var, values, r, c, cb=None):
            cb_w = ttk.Combobox(opts, textvariable=var, values=values,
                                state="readonly")
            cb_w.grid(row=r + 1, column=c, sticky="ew", padx=4, pady=(0, 4))
            if cb:
                cb_w.bind("<<ComboboxSelected>>", lambda e: cb())
            return cb_w

        # row 0/1
        self.aces_var = tk.StringVar(value=list(ACES_CONFIGS.keys())[0])
        label("ACES version", 0, 0)
        combo(self.aces_var, list(ACES_CONFIGS.keys()), 0, 0,
              cb=self._refresh_color_options)

        self.input_var = tk.StringVar()
        label("Input color space", 0, 1)
        self.input_combo = combo(self.input_var, [], 0, 1)

        self.display_var = tk.StringVar()
        label("Output display", 0, 2)
        self.display_combo = combo(self.display_var, [], 0, 2)

        self.look_var = tk.StringVar(value="Tone-mapped (match viewport)")
        label("Look", 0, 3)
        combo(self.look_var,
              ["Tone-mapped (match viewport)", "Un-tone-mapped (plain convert)"],
              0, 3)

        # row 2/3
        self.bits_var = tk.StringVar(value="8-bit")
        label("Bit depth", 2, 0)
        combo(self.bits_var, ["8-bit", "16-bit"], 2, 0)

        self.alpha_var = tk.StringVar(value="Keep alpha (RGBA)")
        label("Alpha", 2, 1)
        combo(self.alpha_var, ["Keep alpha (RGBA)", "Drop alpha (RGB)"], 2, 1)

        self.unpremult_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Un-premultiply alpha", variable=self.unpremult_var
                        ).grid(row=3, column=2, sticky="w", padx=4, pady=(14, 4))

        self.suffix_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text='Add "_srgb" suffix', variable=self.suffix_var
                        ).grid(row=3, column=3, sticky="w", padx=4, pady=(14, 4))

        # ---- output dir ----
        outrow = ttk.Frame(self.root)
        outrow.pack(fill="x", padx=14, pady=(8, 4))
        ttk.Label(outrow, text="Output:", style="Sub.TLabel").pack(side="left")
        self.outdir_var = tk.StringVar(value="")
        self.outdir_entry = tk.Entry(outrow, textvariable=self.outdir_var, bg=ENTRY,
                                     fg=FG, insertbackground=FG, relief="flat")
        self.outdir_entry.pack(side="left", fill="x", expand=True, padx=8, ipady=4)
        ttk.Button(outrow, text="Browse\u2026", command=self.pick_outdir).pack(side="left")
        ttk.Button(outrow, text="Same as source", command=lambda: self.outdir_var.set("")
                   ).pack(side="left", padx=(6, 0))

        # ---- convert + progress ----
        action = ttk.Frame(self.root)
        action.pack(fill="x", padx=14, pady=(8, 4))
        self.convert_btn = ttk.Button(action, text="Convert", style="Accent.TButton",
                                      command=self.start)
        self.convert_btn.pack(side="left")
        self.cancel_btn = ttk.Button(action, text="Cancel", command=self.cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.status = ttk.Label(action, text="Ready", style="Sub.TLabel")
        self.status.pack(side="right")

        self.progress = ttk.Progressbar(self.root, mode="determinate",
                                        style="Horizontal.TProgressbar")
        self.progress.pack(fill="x", padx=14, pady=(0, 6))

        # ---- log ----
        log_wrap = tk.Frame(self.root, bg=BG)
        log_wrap.pack(fill="both", expand=False, padx=14, pady=(0, 12))
        self.log = tk.Text(log_wrap, height=7, bg="#161616", fg=SUBTLE,
                           insertbackground=FG, relief="flat", borderwidth=0,
                           font=("Consolas", 9), wrap="none")
        self.log.pack(side="left", fill="both", expand=True)
        lsb = ttk.Scrollbar(log_wrap, command=self.log.yview)
        lsb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=lsb.set, state="disabled")
        self.log.tag_config("ok", foreground=OK)
        self.log.tag_config("err", foreground=ERR)

        if not (_OIIO_OK and _OCIO_OK and np is not None):
            self._dependency_warning()

    # ---- dependency check ----
    def _dependency_warning(self):
        missing = []
        if np is None:
            missing.append("numpy")
        if not _OIIO_OK:
            missing.append("OpenImageIO")
        if not _OCIO_OK:
            missing.append("OpenColorIO")
        msg = ("Missing: %s\n\nInstall with:\n    pip install OpenImageIO OpenColorIO"
               % ", ".join(missing))
        self.log_line(msg, "err")
        self.convert_btn.config(state="disabled")

    # ---- color option refresh ----
    def _refresh_color_options(self):
        if not _OCIO_OK:
            return
        cfg_name = ACES_CONFIGS[self.aces_var.get()]
        inputs = list_input_spaces(cfg_name)
        self.input_combo["values"] = inputs
        if self.input_var.get() not in inputs:
            self.input_var.set("ACEScg" if "ACEScg" in inputs else inputs[0])
        displays = list_displays(cfg_name)
        self.display_combo["values"] = displays
        if self.display_var.get() not in displays:
            self.display_var.set(default_display(cfg_name))

    # ---- file management ----
    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="Select EXR files",
            filetypes=[("OpenEXR", "*.exr"), ("All files", "*.*")])
        self._add(paths)

    def add_folder(self):
        d = filedialog.askdirectory(title="Select folder of EXRs")
        if not d:
            return
        found = []
        if self.recurse_var.get():
            for root_, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(".exr"):
                        found.append(os.path.join(root_, f))
        else:
            for f in os.listdir(d):
                if f.lower().endswith(".exr"):
                    found.append(os.path.join(d, f))
        self._add(sorted(found))

    def _on_drop(self, event):
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        exrs = []
        for p in paths:
            p = p.strip("{}")
            if os.path.isdir(p):
                for root_, _, files in os.walk(p):
                    for f in files:
                        if f.lower().endswith(".exr"):
                            exrs.append(os.path.join(root_, f))
            elif p.lower().endswith(".exr"):
                exrs.append(p)
        self._add(exrs)

    def _add(self, paths):
        added = 0
        for p in paths:
            if p and p not in self.files:
                self.files.append(p)
                self.listbox.insert("end", p)
                added += 1
        self.status.config(text="%d file(s) queued" % len(self.files))

    def clear_files(self):
        self.files = []
        self.listbox.delete(0, "end")
        self.status.config(text="Ready")

    def pick_outdir(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.outdir_var.set(d)

    # ---- logging / queue ----
    def log_line(self, text, tag=None):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_line(payload[0], payload[1])
                elif kind == "progress":
                    done, total = payload
                    self.progress["maximum"] = total
                    self.progress["value"] = done
                    self.status.config(text="%d / %d" % (done, total))
                elif kind == "done":
                    ok, fail = payload
                    self._finish(ok, fail)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # ---- conversion ----
    def _gather_settings(self):
        cfg_name = ACES_CONFIGS[self.aces_var.get()]
        display = self.display_var.get()
        tone = self.look_var.get().startswith("Tone")
        return {
            "config_name": cfg_name,
            "src": self.input_var.get(),
            "display": display,
            "view": view_for(cfg_name, display, tone),
            "bits": 16 if self.bits_var.get().startswith("16") else 8,
            "keep_alpha": self.alpha_var.get().startswith("Keep"),
            "unpremult": self.unpremult_var.get(),
            "out_dir": self.outdir_var.get().strip() or None,
            "suffix": "_srgb" if self.suffix_var.get() else "",
        }

    def start(self):
        if not self.files:
            messagebox.showinfo(APP_NAME, "Add some .exr files first.")
            return
        if not (_OIIO_OK and _OCIO_OK and np is not None):
            self._dependency_warning()
            return
        settings = self._gather_settings()
        self.cancel_flag.clear()
        self.convert_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.progress["value"] = 0
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self.log_line("View transform: %s  |  %s" % (settings["display"], settings["view"]))
        files = list(self.files)
        self.worker = threading.Thread(target=self._run, args=(files, settings),
                                       daemon=True)
        self.worker.start()

    def _run(self, files, settings):
        total = len(files)
        ok = fail = 0
        for i, path in enumerate(files):
            if self.cancel_flag.is_set():
                self.q.put(("log", ("Cancelled.", None)))
                break
            try:
                out = convert_one(path, settings)
                ok += 1
                self.q.put(("log", ("  OK   " + os.path.basename(out), "ok")))
            except Exception as e:
                fail += 1
                self.q.put(("log", ("  FAIL " + os.path.basename(path) + "  ->  " + str(e), "err")))
                self.q.put(("log", (traceback.format_exc().splitlines()[-1], "err")))
            self.q.put(("progress", (i + 1, total)))
        self.q.put(("done", (ok, fail)))

    def _finish(self, ok, fail):
        self.convert_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        tag = "ok" if fail == 0 else "err"
        self.log_line("Done. %d converted, %d failed." % (ok, fail), tag)
        self.status.config(text="Done \u00b7 %d ok / %d fail" % (ok, fail))

    def cancel(self):
        self.cancel_flag.set()
        self.cancel_btn.config(state="disabled")
        self.status.config(text="Cancelling\u2026")


def main():
    if _DND_OK:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
