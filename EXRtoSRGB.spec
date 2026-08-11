# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller config for EXR -> sRGB.

Three things here are load-bearing:

  * collect_all('OpenImageIO') / collect_all('PyOpenColorIO') pull in the OIIO
    and OCIO binary DLLs. Without them the exe builds fine and dies at launch.
  * the ui/ tree must be shipped as data - it IS the interface. exr2srgb.py
    resolves it through sys._MEIPASS, which only exists in a frozen build.
  * pywebview ships its own hooks under webview/__pyinstaller, which PyInstaller
    discovers automatically and which bring in the WebView2/winforms backend.

The ACES configs are compiled into OpenColorIO, so no colour config ships
alongside the exe.
"""
from PyInstaller.utils.hooks import collect_all

# A stable name at a stable path. The installer's filename carries the version,
# and so do the file properties and Add/Remove Programs - but the exe Windows
# records in the .exr association must not move between releases, or every
# upgrade breaks the double-click. That is exactly what the versioned filename
# did in 3.0.4.
EXE_NAME = 'EXRtoSRGB'

datas = [('ui', 'ui'), ('app.ico', '.'), ('exr.ico', '.')]
binaries = []
# 'cli' is imported inside main() rather than at module scope, so PyInstaller's
# static analysis does not see it and --cli would die with ModuleNotFoundError
# in the exe while working perfectly from source.
hiddenimports = ['webview.platforms.winforms', 'clr_loader', 'cli']

for pkg in ('OpenImageIO', 'PyOpenColorIO', 'webview'):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h


a = Analysis(
    ['exr2srgb.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'PySide6', 'PyQt5', 'PyQt6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=EXE_NAME,
    icon='app.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
