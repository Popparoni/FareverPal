@echo off
setlocal
cd /d %~dp0

REM Build the one-file exe. Bundles the CDB sheets, icons, wiki data and chest
REM positions, plus this app's own assets (UI fonts + chrome SVG icons), so the
REM packaged app is self-contained (paths.py resolves game data from the
REM PyInstaller _MEIPASS dir, and assets_dir() resolves _MEIPASS\assets). Build
REM the Rust ext first (build.bat). To brand the .exe, drop an app_icon.ico in
REM assets\ and add:  --icon assets\app_icon.ico

.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed ^
  --name FareverPal ^
  --icon assets\app_icon.ico ^
  --collect-all PySide6 ^
  --collect-binaries farever_native ^
  --add-data "..\data\sheets;data\sheets" ^
  --add-data "..\htdocs\assets\icons;htdocs\assets\icons" ^
  --add-data "..\htdocs\assets\data;htdocs\assets\data" ^
  --add-data "..\notes;notes" ^
  --add-data "assets\fonts;assets\fonts" ^
  --add-data "assets\icons_ui;assets\icons_ui" ^
  --add-data "assets\map;assets\map" ^
  --add-data "assets\map_icons;assets\map_icons" ^
  --add-data "assets\app_icon.png;assets" ^
  --add-data "assets\app_icon.ico;assets" ^
  run.py

echo.
echo If it built, the exe is in dist\FareverPal.exe
