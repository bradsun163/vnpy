@echo off
setlocal

cd /d "%~dp0"

set "RUNTIME_PYTHON=C:\Users\bradsun\AppData\Local\Programs\Python\Python312\python.exe"
set "LAUNCHER_SCRIPT=%~dp0scripts\launch_parallel_ctp_panel.py"

if not exist "%RUNTIME_PYTHON%" (
    echo Runtime python not found: %RUNTIME_PYTHON%
    pause
    exit /b 1
)

if not exist "%LAUNCHER_SCRIPT%" (
    echo Panel launcher script not found: %LAUNCHER_SCRIPT%
    pause
    exit /b 1
)

echo Launching vn.py parallel CTP panel...
"%RUNTIME_PYTHON%" "%LAUNCHER_SCRIPT%"
if errorlevel 1 (
    echo.
    echo Panel launcher exited with an error.
    pause
    exit /b 1
)

endlocal