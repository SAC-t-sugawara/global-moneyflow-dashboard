@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 先に setup_windows.bat を実行してください。
  pause
  exit /b 1
)
.venv\Scripts\python.exe generate_dashboard.py
if errorlevel 1 pause
if exist "public\index.html" start "" "public\index.html"
