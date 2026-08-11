#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build the Windows installer.

    python build_installer.py

Wraps Inno Setup rather than a .bat because everything this needs - reading
VERSION out of the source, locating ISCC, checking the payload exists - is
awkward in cmd and cost a broken build once already: a python one-liner inside a
`for /f` dies on its own parentheses.

Expects EXRtoSRGB.exe to be built already (build_exe.bat). Produces
EXRtoSRGB_Setup_v<version>.exe next to it.
"""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Inno Setup installs per-user by default, which is where winget puts it; the
# Program Files locations are there for a machine-wide install.
ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                 "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def version():
    with open(os.path.join(HERE, "exr2srgb.py"), encoding="utf-8") as fh:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', fh.read(), re.M)
    if not m:
        sys.exit("could not read VERSION from exr2srgb.py")
    return m.group(1)


def find_iscc():
    for path in ISCC_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    import shutil
    found = shutil.which("ISCC")
    if found:
        return found
    sys.exit("Inno Setup not found. Install it with:\n"
             "    winget install --id JRSoftware.InnoSetup")


def main():
    ver = version()
    exe = os.path.join(HERE, "EXRtoSRGB.exe")
    if not os.path.isfile(exe):
        sys.exit("EXRtoSRGB.exe is not here - run build_exe.bat first.")

    iscc = find_iscc()
    out = "EXRtoSRGB_Setup_v%s.exe" % ver
    print("Building %s" % out)
    print("  from   %s (%.1f MB)" % (os.path.basename(exe),
                                     os.path.getsize(exe) / 1e6))
    print("  using  %s\n" % iscc)

    r = subprocess.run(
        [iscc, "/DAppVersion=%s" % ver, os.path.join(HERE, "installer.iss")],
        cwd=HERE)
    if r.returncode != 0:
        sys.exit("\nBUILD FAILED - Inno Setup returned %d" % r.returncode)

    produced = os.path.join(HERE, out)
    if not os.path.isfile(produced):
        sys.exit("\nBUILD FAILED - Inno Setup reported success but %s is not "
                 "here." % out)
    print("\nDone.  %s  (%.1f MB)" % (out, os.path.getsize(produced) / 1e6))


if __name__ == "__main__":
    main()
