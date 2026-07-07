@echo off
setlocal

cd /d %~dp0

if not exist .venv\Scripts\python.exe (
  echo [build] Missing virtual environment. Create .venv first.
  exit /b 1
)

echo [build] Installing build dependencies...
.venv\Scripts\python.exe -m pip install -r requirements-build.txt

echo [build] Installing runtime dependencies...
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo [build] Running PyInstaller build...
.venv\Scripts\python.exe build.py

if errorlevel 1 (
  echo [build] Build failed.
  exit /b 1
)

echo [build] Build succeeded.
echo [build] Output: dist\DuplicateFinder\DuplicateFinder.exe
endlocal
