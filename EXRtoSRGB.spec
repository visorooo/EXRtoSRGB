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

datas = [('ui', 'ui'), ('app.ico', '.'), ('exr.ico', '.')]
binaries = []
hiddenimports = ['webview.platforms.winforms', 'clr_loader']

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
    name='EXRtoSRGB',
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
