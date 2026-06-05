@echo off
title Worker Builder
echo.
echo Building image_worker.exe ...
echo.
python -m pip install pyinstaller pillow -q
python -m PyInstaller --noconfirm --onefile --console --name image_worker worker.py
echo.
if exist dist\image_worker.exe (
    echo SUCCESS - dist\image_worker.exe ready
    mkdir ..\resources\image_worker 2>nul
    copy dist\image_worker.exe ..\resources\image_worker\image_worker.exe
) else (
    echo FAILED - check error above
)
