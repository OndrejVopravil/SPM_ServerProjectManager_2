@echo off
rem Sestaveni samostatne verze SPM (nepotrebuje nainstalovany Python).
rem Vystup: dist\SPM\SPM.exe a zip k distribuci dist\SPM-<verze>.zip
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3.13 -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)
".venv\Scripts\python.exe" -m pip install --quiet pyinstaller || goto :error

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name SPM ^
    --distpath dist --workpath build ^
    --exclude-module pytest ^
    run_spm.py || goto :error

rem Odstraneni nepouzivanych casti Qt (viz tools\trim_build.py)
".venv\Scripts\python.exe" tools\trim_build.py dist\SPM || goto :error

for /f %%v in ('.venv\Scripts\python.exe -c "import spm; print(spm.__version__)"') do set VER=%%v
powershell -NoProfile -Command "Compress-Archive -Path 'dist\SPM' -DestinationPath 'dist\SPM-%VER%.zip' -Force" || goto :error

echo.
echo Hotovo: dist\SPM\SPM.exe a dist\SPM-%VER%.zip
exit /b 0

:error
echo.
echo Sestaveni selhalo.
pause
exit /b 1
