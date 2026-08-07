@echo off
REM ============================================================
REM  Build EXRtoSRGB.exe  (run this on Windows, in this folder)
REM
REM  The exe lands next to this script rather than in dist\, so
REM  it is the first thing you see in the folder.
REM ============================================================

REM 1) install dependencies
python -m pip install --upgrade pip
python -m pip install OpenImageIO OpenColorIO pywebview pyinstaller

REM 2) build from the saved spec
REM    The spec carries the parts that are easy to get wrong: the OIIO/OCIO
REM    DLLs, the ui\ folder (which IS the interface), and the pywebview
REM    WebView2 backend. Prefer it over a bare command line.
REM    --workpath keeps PyInstaller's scratch out of the repo root.
python -m PyInstaller --noconfirm --distpath . --workpath "%TEMP%\EXRtoSRGB-build" EXRtoSRGB.spec

echo.
echo Done.  EXRtoSRGB.exe is in this folder.
pause
