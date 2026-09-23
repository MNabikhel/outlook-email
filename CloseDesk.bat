@echo off
rem Double-click to read the drop folder, let the local model read, write today's digest,
rem and open the dashboard. The first run sets everything up (a minute or two).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call :setup
  if errorlevel 1 goto :fail
)

".venv\Scripts\python.exe" -m controller_inbox run %*
if errorlevel 1 goto :fail
exit /b 0

:setup
echo First run: setting up CloseDesk. This takes a minute or two.
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo Python 3.11 or newer is needed.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then double-click CloseDesk again.
  exit /b 1
)
%PY% -m venv .venv
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -e .
if errorlevel 1 exit /b 1
echo Setup finished.
exit /b 0

:fail
echo.
echo CloseDesk stopped. The message above says why.
pause
exit /b 1
