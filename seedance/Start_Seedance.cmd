@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

title Seedance Launcher

set "APP_TITLE=Seedance"
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%" || goto fail

if not exist "%APP_DIR%logs" mkdir "%APP_DIR%logs" >nul 2>nul
set "LOG_FILE=%APP_DIR%logs\seedance_windows.log"
set "START_LOG=%APP_DIR%logs\seedance_windows_startup.log"

echo [%date% %time%] Starting %APP_TITLE% > "%START_LOG%"
echo %APP_TITLE% Launcher
echo App dir: %APP_DIR%
echo Startup log: %START_LOG%
echo Runtime log: %LOG_FILE%
echo.

if not exist "%APP_DIR%app.py" (
  echo ERROR: app.py not found.
  echo Put this launcher in the same folder as app.py and static.
  goto fail
)

if not exist "%APP_DIR%static" (
  echo ERROR: static folder not found.
  echo Keep the whole app folder complete. Do not copy only this launcher.
  goto fail
)

mkdir "%APP_DIR%outputs" 2>nul
mkdir "%APP_DIR%archives" 2>nul
mkdir "%APP_DIR%state" 2>nul

call :find_python
if not defined PYTHON call :install_portable_python
if not defined PYTHON goto fail

echo Using Python: %PYTHON%
"%PYTHON%" -c "import sys, cgi; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) and sys.version_info[:2] <= (3, 12) else 1)" >> "%START_LOG%" 2>&1
if errorlevel 1 (
  echo ERROR: Python check failed. Python 3.9-3.12 is required.
  goto fail
)

call :find_port
if not defined PORT (
  echo ERROR: No available local port found.
  goto fail
)

set "URL=http://127.0.0.1:%PORT%"
echo Starting local service: %URL%
echo Keep this window open. Closing it will stop the service.
echo.

set "PORT=%PORT%"
start "Seedance Server" /B "%PYTHON%" "%APP_DIR%app.py" >> "%LOG_FILE%" 2>&1

call :wait_until_ready
if not "%READY%"=="1" (
  echo ERROR: local service did not become ready.
  echo See log: %LOG_FILE%
  goto fail
)

start "" "%URL%"
echo Opened: %URL%
echo Press Ctrl+C or close this window to stop the local service.
echo.

:keep_alive
timeout /t 3600 >nul
goto keep_alive

:find_python
for %%C in ("py -3" "python" "python3") do (
  for /f "usebackq delims=" %%P in (`%%~C -c "import sys, cgi; print(sys.executable) if sys.version_info[:2] >= (3, 9) and sys.version_info[:2] <= (3, 12) else sys.exit(1)" 2^>nul`) do (
    set "PYTHON=%%P"
    exit /b 0
  )
)
exit /b 0

:install_portable_python
set "PY_DIR=%APP_DIR%.portable_python\windows"
set "PYTHON=%PY_DIR%\python.exe"
set "PY_ZIP=%PY_DIR%\python-3.11.9-embed-amd64.zip"
if exist "%PYTHON%" exit /b 0

echo No suitable system Python found. Downloading portable Python...
mkdir "%PY_DIR%" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $dir=$env:PY_DIR; $zip=$env:PY_ZIP; $urls=@('https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip','https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-embed-amd64.zip','https://repo.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip','https://repo.huaweicloud.com/python/3.11.9/python-3.11.9-embeddable-amd64.zip'); $ok=$false; foreach($u in $urls){ try { Write-Host ('Trying: ' + $u); if(Test-Path $zip){ Remove-Item -Force $zip }; Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 300; if((Test-Path $zip) -and ((Get-Item $zip).Length -gt 5000000)){ $ok=$true; break } } catch { Write-Host ('Failed: ' + $_.Exception.Message) } }; if(-not $ok){ throw 'All Python download mirrors failed.' }; Expand-Archive -Force -Path $zip -DestinationPath $dir;"
if errorlevel 1 (
  set "PYTHON="
  echo ERROR: Failed to download portable Python.
  exit /b 1
)
if not exist "%PYTHON%" (
  set "PYTHON="
  echo ERROR: Portable Python was not found after extraction.
  exit /b 1
)
exit /b 0

:find_port
set "PORT="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "for($p=8787; $p -le 8899; $p++){try{$listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse('127.0.0.1'),$p);$listener.Start();$listener.Stop();Write-Output $p;break}catch{}}"`) do set "PORT=%%P"
exit /b 0

:wait_until_ready
set "READY=0"
for /l %%I in (1,1,80) do (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 '%URL%/api/config'; if ($r.StatusCode -eq 200) { exit 0 } } catch { exit 1 }" >nul 2>nul
  if not errorlevel 1 (
    set "READY=1"
    exit /b 0
  )
  timeout /t 1 >nul
)
exit /b 0

:fail
echo.
echo Startup failed or stopped.
echo Startup log: %START_LOG%
echo Runtime log: %LOG_FILE%
echo.
pause
exit /b 1
