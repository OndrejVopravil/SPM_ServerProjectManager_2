@echo off
rem Spusteni SPM - Server Project Manager (dvojklik)
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Prvni spusteni - vytvarim prostredi .venv a instaluji zavislosti...
    py -3.13 -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :error
)

start "" ".venv\Scripts\pythonw.exe" -m spm
exit /b 0

:error
echo.
echo Instalace se nezdarila. Je nainstalovan Python 3.13 (prikaz "py -3.13")?
pause
exit /b 1
