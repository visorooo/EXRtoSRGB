@echo off
REM ============================================================
REM  Build EXRtoSRGB_v<version>.exe  (run on Windows, in this folder)
REM
REM  The exe lands next to this script rather than in dist\, so it is
REM  the first thing you see in the folder. The version is part of the
REM  filename so a downloaded copy says what it is without being run.
REM
REM  EXRtoSRGB.spec owns the name - it reads VERSION out of exr2srgb.py.
REM  This script deliberately does NOT work the name out for itself:
REM  doing that in cmd needs a python one-liner inside a for /f, and the
REM  parentheses in it break cmd's parser. One source of truth, and the
REM  filename is discovered afterwards instead.
REM ============================================================

REM 1) install dependencies
python -m pip install --upgrade pip
python -m pip install OpenImageIO OpenColorIO pywebview pyinstaller

REM 2) refuse to build over a running copy
REM    PyInstaller deletes the old exe before writing the new one, so a running
REM    instance fails it with "Access is denied" - and the build otherwise
REM    carried on and reported success, leaving a stale exe that looks current.
REM    Matches any version: an older build is just as capable of holding a lock.
tasklist /FI "IMAGENAME eq EXRtoSRGB*.exe" 2>nul | find /I "EXRtoSRGB" >nul
if not errorlevel 1 (
    echo.
    echo An EXR to sRGB window is running. Close it first - the build cannot
    echo replace a locked file, and would leave you with the previous version.
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
    echo BUILD FAILED - nothing was rebuilt. Scroll up for the error.
    pause
    exit /b 1
)

dir /b EXRtoSRGB_v*.exe >nul 2>&1
if errorlevel 1 (
    echo.
    echo BUILD FAILED - PyInstaller reported success but no versioned exe
    echo is here. Check that VERSION in exr2srgb.py is readable.
    pause
    exit /b 1
)

echo.
echo Done.  Built:
for %%f in (EXRtoSRGB_v*.exe) do echo    %%f
pause
