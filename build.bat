@echo off
rem Builds the portable single-file exe: dist\GLTF to Enscape Custom Asset.exe
rem Needs: pip install pyinstaller tkinterdnd2 sv-ttk pillow
rem Work files go to %TEMP% (the cloud-synced folder locks them mid-build).
cd /d "%~dp0"
set WORK=%TEMP%\gltf2enscape_build
python -m PyInstaller --noconfirm --clean --onefile --windowed --name "GLTF to Enscape Custom Asset" ^
  --icon "%~dp0icon.ico" --workpath "%WORK%" --specpath "%WORK%" --distpath "%~dp0dist" ^
  --collect-all tkinterdnd2 --collect-data sv_ttk --add-data "%~dp0icon.ico;." --exclude-module numpy --exclude-module matplotlib --exclude-module pandas "%~dp0gltf2enscape.py"
