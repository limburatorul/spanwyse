@echo off
rem Builds dist\Spanwyse.exe: one file, no console, the icon embedded and bundled for the window.
rem Needs Python 3.11+ with Pillow and PyInstaller (pip install pillow pyinstaller).
cd /d "%~dp0"
python spanwyse.py --test || exit /b 1
python -m PyInstaller --noconfirm --onefile --windowed --name Spanwyse --icon "%~dp0spanwyse.ico" --add-data "%~dp0spanwyse.ico;." --distpath dist --workpath build --specpath build spanwyse.py
