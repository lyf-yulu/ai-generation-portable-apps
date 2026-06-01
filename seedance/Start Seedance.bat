@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

title Seedance Launcher - China Mirror

set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

echo.
echo Seedance Launcher - China Mirror
echo App dir: %APP_DIR%
echo.

if not exist "%APP_DIR%app.py" (
  echo ERROR: app.py not found.
  echo Please put this launcher in the same folder as app.py and static.
  echo.
  pause
  exit /b 1
)

if not exist "%APP_DIR%static" (
  echo ERROR: static folder not found.
  echo Please keep the whole tool folder complete. Do not copy only this launcher.
  echo.
  pause
  exit /b 1
)

mkdir "%APP_DIR%outputs" 2>nul
mkdir "%APP_DIR%archives" 2>nul
mkdir "%APP_DIR%state" 2>nul
mkdir "%APP_DIR%logs" 2>nul
mkdir "%APP_DIR%.portable_python\windows" 2>nul

set "PYTHON=%APP_DIR%.portable_python\windows\python.exe"
set "PYZIP=%APP_DIR%.portable_python\windows\python-3.11.9-embed-amd64.zip"
set "RH_PY_DIR=%APP_DIR%.portable_python\windows"
set "RH_PY_ZIP=%APP_DIR%.portable_python\windows\python-3.11.9-embed-amd64.zip"

if not exist "%PYTHON%" (
  echo Portable Python not found. Downloading from China mirrors now.
  echo First run needs internet. It will be saved inside this folder.
  echo.

  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $dir=$env:RH_PY_DIR; $zip=$env:RH_PY_ZIP; New-Item -ItemType Directory -Force -Path $dir | Out-Null; $urls=@('https://registry.npmmirror.com/-/binary/python/3.11.9/python-3.11.9-embed-amd64.zip','https://repo.huaweicloud.com/python/3.11.9/python-3.11.9-embed-amd64.zip','https://repo.huaweicloud.com/python/3.11.9/python-3.11.9-embeddable-amd64.zip','https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip'); $ok=$false; foreach($u in $urls){ try { Write-Host ('Trying: ' + $u); if(Test-Path $zip){ Remove-Item -Force $zip }; Invoke-WebRequest -Uri $u -OutFile $zip -TimeoutSec 300; if((Test-Path $zip) -and ((Get-Item $zip).Length -gt 5000000)){ $ok=$true; break } } catch { Write-Host ('Failed: ' + $_.Exception.Message) } }; if(-not $ok){ throw 'All Python download mirrors failed.' }; Expand-Archive -Force -Path $zip -DestinationPath $dir;"

  if errorlevel 1 (
    echo.
    echo ERROR: Failed to download Portable Python from all mirrors.
    echo Check internet, or ask the maker to include the .portable_python folder.
    echo.
    pause
    exit /b 1
  )
)

if not exist "%PYTHON%" (
  echo.
  echo ERROR: Portable Python is not ready:
  echo %PYTHON%
  echo.
  pause
  exit /b 1
)

echo Searching for an available local port...

set "PORT="
for /f "usebackq delims=" %%p in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "for($p=8787; $p -lt 8899; $p++){try{$listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse('127.0.0.1'),$p);$listener.Start();$listener.Stop();Write-Output $p;break}catch{}}"`) do set "PORT=%%p"

if "%PORT%"=="" (
  echo ERROR: No available local port found.
  echo Close other local services and try again.
  echo.
  pause
  exit /b 1
)

set "URL=http://127.0.0.1:%PORT%"
set "PORT=%PORT%"

echo.
echo Starting local service: %URL%
echo Keep this window open. Closing it will stop the service.
echo Log file: logs\seedance_windows.log
echo.

start "" "%URL%"

"%PYTHON%" "%APP_DIR%app.py" >> "%APP_DIR%logs\seedance_windows.log" 2>&1

echo.
echo Program exited. If the webpage cannot open, send logs\seedance_windows.log to the maker.
echo.
pause
