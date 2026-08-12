"""
Command-line interface for EXR -> sRGB.

Exists so the converter is usable from a render farm, a build step or a shell
loop without opening a window. Imports `core` only - no pywebview, no UI - so it
runs anywhere core does and starts in the time it takes to import OIIO.

    python cli.py shots/ --format png --bits 16 --out out/
    EXRtoSRGB.exe --cli shots/beauty.0001.exr --all-layers

`--convert` remains a separate, deliberately dumb entry point for the Explorer
right-click verbs; this is the one meant for people and scripts.
"""

import argparse
import os
import sys

import core

# Exit codes, so a farm can tell "nothing to do" from "some frames failed".
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NOTHING = 2
EXIT_BADARGS = 3


def _resolve_config(name):
    """
    Accept a friendly label, a built-in registry name, or a path to config.ocio.

    The UI offers a dropdown; a CLI has to take whatever someone types, and the
    labels carry a bullet that is painful to quote in a shell.
    """
    if not name:
        return list(core.ACES_CONFIGS.values())[0]
    if os.path.exists(name):
        return os.path.abspath(name)
    if name in core.ACES_CONFIGS.values():
        return name
    for label, ref in core.ACES_CONFIGS.items():
        if label == name:
            return ref
    # match on the distinctive part, so "cg-v2.2" or "ACES 2.0" is enough
    key = name.lower().replace(" ", "")
    hits = [ref for label, ref in core.ACES_CONFIGS.items()
            if key in label.lower().replace(" ", "").replace("·", "")
            or key in ref.lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SystemExit("--config %r is ambiguous; it matches %d configs. "
                         "Use --list-configs to see the exact names."
                         % (name, len(hits)))
    raise SystemExit("--config %r matched nothing. Try --list-configs."
                     % name)


def _gather(paths, recurse):
    """Every .exr the given paths refer to, expanded and de-duplicated."""
    out = []
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            out.extend(core.find_exrs(p, recurse))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print("skipping, not found: %s" % p, file=sys.stderr)
    seen = set()
    return [p for p in out if not (p in seen or seen.add(p))]


def build_parser():
    p = argparse.ArgumentParser(
        prog="EXRtoSRGB --cli",
        description="Convert ACES-linear EXR renders to display-ready images.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Defaults match the app: ACES 1.3 CG v2.2, 16-bit PNG, "
               "un-premultiply on, _srgb suffix on.")
    p.add_argument("paths", nargs="*",
                   help="files or folders; folders are searched for .exr")
    p.add_argument("--out", metavar="DIR",
                   help="output folder (default: beside each source)")
    p.add_argument("--format", choices=("png", "jpeg", "tiff"), default="png")
    p.add_argument("--bits", type=int, choices=(8, 16, 32), default=16)
    p.add_argument("--quality", type=int, default=95, help="JPEG quality")
    p.add_argument("--config", metavar="NAME|PATH",
                   help="OCIO config: a built-in name, a substring of one, "
                        "or a path to config.ocio")
    p.add_argument("--input-cs", metavar="NAME", default="ACEScg",
                   help="input colour space (default: ACEScg)")
    p.add_argument("--display", metavar="NAME",
                   help="output display (default: the config's sRGB display)")
    p.add_argument("--look", choices=("tone", "plain", "linear"), default="tone",
                   help="tone: the viewport look. plain: no tone map. "
                        "linear: scene-linear passthrough, forces 32-bit TIFF")
    p.add_argument("--layer", metavar="NAME",
                   help="layer to convert (default: auto-detect the beauty)")
    p.add_argument("--all-layers", action="store_true",
                   help="convert every layer, one file each, named for the layer")
    # `keep` matches Nuke and After Effects; `keep-straight` writes true
    # surface colour with alpha alongside, which is what PNG's spec asks for and
    # what a compositor wants when laying the image over a new background. They
    # differ only on antialiased edges.
    p.add_argument("--alpha",
                   choices=("keep", "keep-straight", "black", "white"),
                   default="keep",
                   help="keep: match Nuke/After Effects (default); "
                        "keep-straight: straight alpha, correct for "
                        "compositing over a new background; "
                        "black/white: flatten")
    p.add_argument("--suffix", metavar="STR", default=None,
                   help="output suffix (default: _srgb, or _linear)")
    p.add_argument("--no-suffix", action="store_true",
                   help="write with the source stem; refuses to overwrite the "
                        "source only because the extension differs")
    p.add_argument("--jobs", type=int, metavar="N",
                   help="worker threads (default: automatic, capped at 8)")
    p.add_argument("--no-recurse", action="store_true",
                   help="do not descend into sub-folders")
    p.add_argument("--dry-run", action="store_true",
                   help="list what would be written and exit")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--list-configs", action="store_true")
    p.add_argument("--list-displays", action="store_true")
    p.add_argument("--list-layers", action="store_true",
                   help="show the layers in the given files and exit")
    return p


def _settings_for(a, config, layer):
    display = a.display or core.default_display(config)
    transfer = "linear" if a.look == "linear" else "display"
    if a.no_suffix:
        suffix = ""
    elif a.suffix is not None:
        suffix = a.suffix
    else:
        suffix = "_linear" if transfer == "linear" else "_srgb"
    # The layer belongs in the name whenever more than one is being written, or
    # every AOV lands on the same file and only the last survives.
    if layer and a.all_layers:
        suffix = core.layer_tag(layer) + suffix
    return {
        "config": config,
        "src": a.input_cs,
        "display": display,
        "view": core.view_for(config, display, a.look == "tone"),
        "format": a.format,
        "quality": a.quality,
        "bits": a.bits,
        # Same split the UI makes: core takes the channel decision and the edge
        # convention as separate keys. Flatten keeps un-premultiply on, which is
        # the colour-correct way to composite over a background.
        "alpha_mode": "keep" if a.alpha == "keep-straight" else a.alpha,
        "layer": layer,
        "unpremult": a.alpha != "keep",
        "transfer": transfer,
        "out_dir": os.path.abspath(a.out) if a.out else None,
        "suffix": suffix,
    }


def main(argv=None):
    a = build_parser().parse_args(argv)

    if a.list_configs:
        for label, ref in core.ACES_CONFIGS.items():
            print("%-40s %s" % (label, ref))
        return EXIT_OK

    config = _resolve_config(a.config)

    if a.list_displays:
        for d in core.list_displays(config):
            mark = "*" if d == core.default_display(config) else " "
            print("%s %s" % (mark, d))
        return EXIT_OK

    files = _gather(a.paths, not a.no_recurse)
    if not files:
        print("nothing to convert", file=sys.stderr)
        return EXIT_NOTHING

    if a.list_layers:
        for f in files:
            print(f)
            for name in core.probe_layers(f):
                print("    %s" % (name or "(R,G,B)"))
        return EXIT_OK

    if a.out:
        os.makedirs(os.path.abspath(a.out), exist_ok=True)

    # One pass per layer. --all-layers reads the layer list from the first file
    # and applies it to the batch, which is what a render sequence needs; files
    # missing a layer report it rather than silently writing the beauty.
    layers = [a.layer]
    if a.all_layers:
        layers = core.probe_layers(files[0]) or [None]

    if not a.quiet:
        n = len(files) * len(layers)
        print("%d file%s%s -> %s" % (
            len(files), "" if len(files) == 1 else "s",
            "" if len(layers) < 2 else " x %d layers (%d outputs)"
            % (len(layers), n),
            os.path.abspath(a.out) if a.out else "beside each source"))

    if a.dry_run:
        for layer in layers:
            s = _settings_for(a, config, layer)
            fmt, pixfmt, bits = core.resolve_output(s)
            for f in files:
                print("%s  [%s %s]" % (core.output_path_for(f, s), fmt, pixfmt))
        return EXIT_OK

    failed = 0
    done = 0

    # on_result is (index, path, out_path, info, error), called in submission
    # order from this thread - so no lock, and the output reads like the input.
    def report(_i, path, out, info, err):
        nonlocal failed, done
        if err:
            failed += 1
            print("FAILED  %s: %s" % (os.path.basename(path), err),
                  file=sys.stderr)
            return
        done += 1
        if not a.quiet:
            note = ""
            if info and info.get("warning"):
                note = "  (%s)" % info["warning"]
            print("%s%s" % (out, note))

    for layer in layers:
        s = _settings_for(a, config, layer)
        core.convert_many(files, s, workers=a.jobs, on_result=report)

    if not a.quiet:
        print("\n%d written, %d failed" % (done, failed))
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
