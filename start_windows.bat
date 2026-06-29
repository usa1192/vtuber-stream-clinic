@echo off
setlocal
cd /d "%~dp0"
set LOG=startup.log

echo VTuber Stream Clinic launcher
echo VTuber Stream Clinic launcher > "%LOG%"
echo.

if not exist ".env" (
  echo .env was not found. Creating it from .env.example...
  echo .env was not found. >> "%LOG%"
  copy ".env.example" ".env" >> "%LOG%" 2>&1
  echo Please open .env, set GEMINI_API_KEY, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python environment...
  echo Creating local Python environment... >> "%LOG%"
  where py >nul 2>nul
  if not errorlevel 1 (
    py -m venv .venv >> "%LOG%" 2>&1
  ) else (
    where python >nul 2>nul
    if not errorlevel 1 (
      python -m venv .venv >> "%LOG%" 2>&1
    ) else (
      echo Python was not found.
      echo Python was not found. >> "%LOG%"
      echo Install Python, then run this file again.
      pause
      exit /b 1
    )
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo Failed to create .venv.
  echo Failed to create .venv. >> "%LOG%"
  pause
  exit /b 1
)

echo Installing/checking packages...
echo Installing/checking packages... >> "%LOG%"
".venv\Scripts\python.exe" -m pip install -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  echo Package install failed. See startup.log in this folder.
  pause
  exit /b 1
)

echo.
echo Server starting...
echo Open this URL in your browser:
echo http://127.0.0.1:8000/
echo.
echo Keep this black window open while using the app.
echo Press Ctrl+C to stop the server.
echo.

".venv\Scripts\python.exe" -m uvicorn backend.app:app --reload --host 127.0.0.1 --port 8000

echo.
echo Server stopped or failed. See startup.log if there was an error.
pause
