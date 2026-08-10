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

REM 2) refuse to build over a running copy
REM    PyInstaller deletes the old exe before writing the new one, so a running
REM    instance fails it with "Access is denied" - and the build otherwise
REM    carried on and reported success, leaving a stale exe that looks current.
tasklist /FI "IMAGENAME eq EXRtoSRGB.exe" 2>nul | find /I "EXRtoSRGB.exe" >nul
if not errorlevel 1 (
    echo.
    echo EXRtoSRGB.exe is running. Close it first - the build cannot replace a
    echo locked file, and would leave you with the previous version.
    pause
    exit /b 1
)

REM 3) build from the saved spec
REM    The spec carries the parts that are easy to get wrong: the OIIO/OCIO
REM    DLLs, the ui\ folder (which IS the interface), and the pywebview
REM    WebView2 backend. Prefer it over a bare command line.
REM    --workpath keeps PyInstaller's scratch out of the repo root.
python -m PyInstaller --noconfirm --distpath . --workpath "%TEMP%\EXRtoSRGB-build" EXRtoSRGB.spec

REM 4) report what actually happened
REM    "Done." used to print unconditionally, so a failed build read as a
REM    successful one. Never say Done unless PyInstaller said so too.
if errorlevel 1 (
    echo.
    echo BUILD FAILED - EXRtoSRGB.exe was NOT rebuilt. Scroll up for the error.
    pause
    exit /b 1
)

echo.
echo Done.  EXRtoSRGB.exe is in this folder.
pause
