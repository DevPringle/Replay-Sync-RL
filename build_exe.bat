@echo off

rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q "Replay Sync.spec" 2>nul

python -m pip install -r requirements.txt

python -m PyInstaller ^
--noconfirm ^
--onefile ^
--windowed ^
--name "Replay Sync" ^
--icon app.ico ^
--add-data "app.ico;." ^
app.py

echo.
echo Finished build.
echo Launch:
echo dist\Replay Sync.exe
pause
