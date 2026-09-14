@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONDONTWRITEBYTECODE=1"

cd /d "%~dp0"

if /I "%~1"=="--help" goto :help
if not "%~1"=="" (
  echo ERROR: Unknown argument "%~1".
  echo Run build.bat --help for usage.
  exit /b 2
)

if not exist "remote_web.py" (
  echo ERROR: build.bat must be run from the Broadcaster Remote source folder.
  exit /b 1
)

set "BUILD_ROOT=%CD%\.build"
set "PY_DIST=%BUILD_ROOT%\pyinstaller-dist"
set "PY_WORK=%BUILD_ROOT%\pyinstaller-work"
set "STAGE=%BUILD_ROOT%\stage"
set "TARGET=%CD%\dist\remote"
set "BACKUP=%BUILD_ROOT%\previous-remote"
set "SAVED_CONFIG=%BUILD_ROOT%\remote_config.json"
set "PUBLISH_STARTED=0"

where python.exe >nul 2>nul
if errorlevel 1 (
  echo ERROR: python.exe is not available on PATH.
  exit /b 1
)

python -c "import PyInstaller, flask, flask_socketio, requests, simple_websocket" >nul 2>nul
if errorlevel 1 (
  echo ERROR: Required build dependencies are missing.
  echo Install pyinstaller, flask, flask-socketio, requests, and simple-websocket.
  exit /b 1
)

echo [1/5] Preserving packaged configuration...
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"
mkdir "%BUILD_ROOT%" || exit /b 1
if exist "%TARGET%\server\remote_config.json" (
  copy /y "%TARGET%\server\remote_config.json" "%SAVED_CONFIG%" >nul || exit /b 1
)

echo [2/5] Running tests...
python -m unittest discover -s "%CD%" -p "test_*.py"
if errorlevel 1 goto :failed

where lua.exe >nul 2>nul
if errorlevel 1 (
  echo NOTE: lua.exe is not on PATH; skipping test_remote_lua.lua.
) else (
  lua.exe "%CD%\test_remote_lua.lua"
  if errorlevel 1 goto :failed
)

echo [3/5] Compiling standalone server...
python -m PyInstaller --noconfirm --clean --onedir --console --name remote ^
  --hidden-import engineio.async_drivers.threading ^
  --distpath "%PY_DIST%" --workpath "%PY_WORK%" --specpath "%BUILD_ROOT%" ^
  "%CD%\remote_web.py"
if errorlevel 1 goto :failed

echo [4/5] Assembling distribution...
mkdir "%STAGE%\remote\server\event_logs" || goto :failed
xcopy "%PY_DIST%\remote\*" "%STAGE%\remote\server\" /e /i /q /y >nul
if errorlevel 1 goto :failed
xcopy "%CD%\static\*" "%STAGE%\remote\server\static\" /e /i /q /y >nul
if errorlevel 1 goto :failed
copy /y "%CD%\remote.lua" "%STAGE%\remote\remote.lua" >nul || goto :failed
copy /y "%CD%\manifest.ini" "%STAGE%\remote\manifest.ini" >nul || goto :failed
copy /y "%CD%\logo.png" "%STAGE%\remote\logo.png" >nul || goto :failed
copy /y "%CD%\stinger.png" "%STAGE%\remote\stinger.png" >nul || goto :failed
copy /y "%CD%\Michroma-Regular.ttf" "%STAGE%\remote\Michroma-Regular.ttf" >nul || goto :failed

if exist "%SAVED_CONFIG%" (
  copy /y "%SAVED_CONFIG%" "%STAGE%\remote\server\remote_config.json" >nul || goto :failed
) else (
  copy /y "%CD%\remote_config.example.json" "%STAGE%\remote\server\remote_config.json" >nul || goto :failed
)

if not exist "%STAGE%\remote\server\remote.exe" (
  echo ERROR: PyInstaller did not produce remote.exe.
  goto :failed
)

echo [5/5] Publishing dist\remote...
if exist "%TARGET%" (
  xcopy "%TARGET%\*" "%BACKUP%\" /e /i /q /y >nul
  if errorlevel 1 goto :failed
  set "PUBLISH_STARTED=1"
  if exist "%TARGET%\static" rmdir /s /q "%TARGET%\static"
  if errorlevel 1 goto :failed
  if exist "%TARGET%\server\static" rmdir /s /q "%TARGET%\server\static"
  if errorlevel 1 goto :failed
  xcopy "%STAGE%\remote\*" "%TARGET%\" /e /i /q /y >nul
) else (
  set "PUBLISH_STARTED=1"
  xcopy "%STAGE%\remote\*" "%TARGET%\" /e /i /q /y >nul
)
if errorlevel 1 goto :failed
if exist "%BUILD_ROOT%" rmdir /s /q "%BUILD_ROOT%"

echo.
echo Build complete: %TARGET%
exit /b 0

:failed
if "%PUBLISH_STARTED%"=="1" if exist "%BACKUP%" (
  echo Restoring the previous distribution...
  xcopy "%BACKUP%\*" "%TARGET%\" /e /i /q /y >nul
)
echo.
echo ERROR: Build failed. Review the output above; staging files remain in .build.
exit /b 1

:help
echo Builds Broadcaster Remote into dist\remote.
echo.
echo Usage: build.bat
echo.
echo The build runs tests, compiles remote.exe with PyInstaller, copies server
echo assets under server\static and Lua assets at the app root, and
echo preserves dist\remote\server\remote_config.json.
exit /b 0
