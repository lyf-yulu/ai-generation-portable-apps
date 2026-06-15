@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion

title AI Generation Portal

set "ROOT=%~dp0"
cd /d "%ROOT%portal" || goto fail

echo ========================================
echo   AI Generation Portal
echo ========================================
echo.

if not exist "app.py" (
  echo ERROR: portal\app.py not found.
  echo Run this launcher from the project root folder.
  pause
  exit /b 1
)

call :find_python
if defined PYTHON (
  echo Python: %PYTHON%
) else (
  echo ERROR: Python 3.9-3.12 not found.
  echo Install Python from https://www.python.org/downloads/
  echo Make sure to check "Add Python to PATH" during installation.
  pause
  exit /b 1
)

"%PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) and sys.version_info[:2] <= (3, 12) else 1)" >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python 3.9-3.12 required. Current:
  "%PYTHON%" --version
  pause
  exit /b 1
)

echo.
echo Starting sub-apps and portal on port 9090...
echo Keep this window open. Closing it will stop all services.
echo.

start "AI Portal Server" /B "%PYTHON%" "app.py"

:: Wait for portal to be ready (HTTPS on 9090, HTTP redirect on 9089)
set "PORTAL_URL=https://127.0.0.1:9090"
set "PORTAL_FALLBACK=http://127.0.0.1:9089"
echo Waiting for portal to start...
for /l %%I in (1,1,60) do (
  :: Try HTTPS first
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%PORTAL_URL%/api/platform/status'; if ($r.StatusCode -eq 200) { exit 0 } } catch { }" >nul 2>nul
  if not errorlevel 1 (
    echo Portal ready.
    goto :opened
  )
  :: Try HTTP redirect port
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%PORTAL_FALLBACK%/api/platform/status'; if ($r.StatusCode -eq 200 -or $r.StatusCode -eq 301) { exit 0 } } catch { }" >nul 2>nul
  if not errorlevel 1 (
    echo Portal ready ^(HTTP redirect port^).
    set "PORTAL_URL=%PORTAL_FALLBACK%"
    goto :opened
  )
  timeout /t 1 >nul
)

:opened
start "" "%PORTAL_URL%"
echo Opened: %PORTAL_URL%
echo.
echo Press Ctrl+C or close this window to stop all services.

:keep_alive
timeout /t 3600 >nul
goto keep_alive

:find_python
for %%C in ("py -3" "python" "python3") do (
  for /f "usebackq delims=" %%P in (`%%~C -c "import sys; print(sys.executable)" 2^>nul`) do (
    set "PYTHON=%%P"
    exit /b 0
  )
)
exit /b 0

:fail
echo.
echo Startup failed.
echo.
echo Troubleshooting:
echo   1. Make sure Python 3.9-3.12 is installed
echo   2. Run: pip install pyOpenSSL  (optional, for HTTPS)
echo   3. Check that firewall allows port 9090
echo.
pause
exit /b 1
