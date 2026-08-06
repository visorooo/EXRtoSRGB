@echo off
REM ============================================================
REM  Build EXRtoPNG.exe  (run this on Windows, in this folder)
REM ============================================================

REM 1) install dependencies
python -m pip install --upgrade pip
python -m pip install OpenImageIO OpenColorIO pyinstaller

REM 2) build a single-file windowed exe
REM    Use "python -m PyInstaller" so it works even when the
REM    pyinstaller command isn't on your PATH.
REM    --collect-all pulls in the OIIO / OCIO binary DLLs.
python -m PyInstaller --onefile --windowed --name "EXRtoPNG" ^
  --collect-all OpenImageIO ^
  --collect-all PyOpenColorIO ^
  exr2png.py

echo.
echo Done. Your exe is in the  dist\  folder:  dist\EXRtoPNG.exe
pause
