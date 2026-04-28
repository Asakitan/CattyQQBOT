@echo off
setlocal EnableExtensions

cd /d "%~dp0"
set "PYTHON_CMD="

py -3.11 -c "import sys" >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py -3.11"

if not defined PYTHON_CMD (
  py -3 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
  python -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
  where winget >nul 2>nul
  if errorlevel 1 (
    echo Python was not found and winget is unavailable.
    echo Install Python 3.11 or newer, then run this installer again.
    pause
    exit /b 1
  )
  echo Installing Python 3.11 with winget...
  winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo Python installation failed.
    pause
    exit /b 1
  )
  set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
  py -3.11 -c "import sys" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3.11"
)

if not defined PYTHON_CMD (
  echo Python is still unavailable after installation.
  pause
  exit /b 1
)

echo Creating virtual environment...
%PYTHON_CMD% -m venv .venv
if errorlevel 1 (
  echo Failed to create .venv.
  pause
  exit /b 1
)

echo Installing Catty dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
  echo Failed to upgrade pip.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
  echo Failed to install Catty dependencies.
  pause
  exit /b 1
)

set "START_BAT=start_catty.bat"
> "%START_BAT%" (
  echo @echo off
  echo setlocal EnableExtensions
  echo cd /d "%%~dp0"
  echo if not exist ".venv\Scripts\python.exe" ^(
  echo   echo Missing .venv. Run install_python_env.bat again.
  echo   pause
  echo   exit /b 1
  echo ^)
  echo if "%%CATTY_NO_HOT_RELOAD%%"=="1" ^(
  echo   ".venv\Scripts\python.exe" bot.py
  echo ^) else ^(
  echo   ".venv\Scripts\python.exe" scripts\catty_hot_reload.py
  echo ^)
  echo pause
)

echo Environment is ready.
echo Created %START_BAT%.

set "SELF=%~f0"
start "" cmd /c ping 127.0.0.1 -n 2 ^>nul ^& del /f /q "%SELF%"
endlocal
exit /b 0
