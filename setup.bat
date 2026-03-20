@echo off
setlocal enabledelayedexpansion

:: --- Quad4 Reticulum node ---
set "RETICULUM_CONFIG=%APPDATA%\Local\RETICULUM\config"
if not exist "%RETICULUM_CONFIG%" set "RETICULUM_CONFIG=%USERPROFILE%\.reticulum\config"

if exist "%RETICULUM_CONFIG%" (
    findstr /c:"[[Quad4]]" "%RETICULUM_CONFIG%" >nul 2>&1
    if not errorlevel 1 (
        echo Quad4 interface already present in Reticulum config, skipping.
    ) else (
        set /p ADD_QUAD4="Add Quad4 TCP node to Reticulum config? (y/N) "
        if /i "!ADD_QUAD4!"=="y" (
            echo.>> "%RETICULUM_CONFIG%"
            echo   [[Quad4]]>> "%RETICULUM_CONFIG%"
            echo     type = TCPClientInterface>> "%RETICULUM_CONFIG%"
            echo     interface_enabled = true>> "%RETICULUM_CONFIG%"
            echo     target_host = 62.151.179.77>> "%RETICULUM_CONFIG%"
            echo     target_port = 45657>> "%RETICULUM_CONFIG%"
            echo     mode = full>> "%RETICULUM_CONFIG%"
            echo Quad4 interface added.
        ) else (
            echo Skipping Quad4 interface.
        )
    )
) else (
    echo Reticulum config not found -- skipping Quad4 setup.
    echo ^(Run the app once to generate the config, then re-run this script.^)
)

echo.

:: Find Python
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10 or newer from https://python.org and try again.
    exit /b 1
)

:: Check Python version
for /f "tokens=*" %%v in ('python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"') do set PY_VERSION=%%v
for /f "tokens=1 delims=." %%a in ("%PY_VERSION%") do set PY_MAJOR=%%a
for /f "tokens=2 delims=." %%b in ("%PY_VERSION%") do set PY_MINOR=%%b

if %PY_MAJOR% LSS 3 (
    echo ERROR: Python 3.10+ required ^(found %PY_VERSION%^).
    exit /b 1
)
if %PY_MAJOR% EQU 3 if %PY_MINOR% LSS 10 (
    echo ERROR: Python 3.10+ required ^(found %PY_VERSION%^).
    exit /b 1
)

echo Using Python %PY_VERSION%

:: Create virtual environment
if not exist ".venv\" (
    echo Creating virtual environment...
    python -m venv .venv
) else (
    echo Virtual environment already exists, skipping creation.
)

:: Install dependencies
echo Installing dependencies...
.venv\Scripts\pip install --upgrade pip --quiet
.venv\Scripts\pip install -r requirements.txt --quiet

echo.
echo Setup complete. Launching TrenchChat...
echo.
.venv\Scripts\python main.py %*

endlocal
