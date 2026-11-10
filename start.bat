@echo off
REM Launcher do JARVIS — clique duplo aqui pra ligar.
REM Inicia o servidor + Edge em modo --app (parece um aplicativo desktop).
REM Janela do console fica minimizada; logs vão pra jarvis.log.

cd /d "%~dp0"

REM Se houver venv local, usa ele
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

REM Roda minimizado, redirecionando logs
start "JARVIS" /MIN cmd /c "%PY% main.py >> jarvis.log 2>&1"

exit /b 0
