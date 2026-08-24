@echo off
REM KIN Mail license generator wrapper - Windows.
setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv"
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo Error: Python not found on PATH. Install Python 3.9+ from python.org.
    exit /b 1
  )
  set "PYBIN=py"
) else (
  set "PYBIN=python"
)
if not exist "%VENV%\Scripts\python.exe" (
  echo First run: setting up a local Python environment in %VENV% ...
  %PYBIN% -m venv "%VENV%"
  "%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip
  "%VENV%\Scripts\python.exe" -m pip install --quiet cryptography
  echo Done. This only happens once.
)
"%VENV%\Scripts\python.exe" "%HERE%generate_license.py" %*
endlocal
