@echo off
rem Double-click to read the drop folder, let the local model read, write today's digest,
rem and open the dashboard. The first run sets everything up (a minute or two).
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  call :setup
  if errorlevel 1 goto :fail
)

rem An update can add or raise a package (the add-ons' too); install it before starting. The add-ons are
rem kept where they install, and left out where they can't.
".venv\Scripts\python.exe" scripts\check_deps.py >nul 2>&1
if errorlevel 1 (
  call :update
  if errorlevel 1 goto :fail
)

rem The second table reader (Camelot) is optional: installed when it can be, skipped when it can't. It is tried
rem once; .venv\.tables-tried remembers that, so a computer that can't install it doesn't try on every start.
if not exist ".venv\.tables-tried" (
  ".venv\Scripts\python.exe" -c "import camelot" >nul 2>&1 || ".venv\Scripts\python.exe" -m pip install --quiet -e ".[tables]" >nul 2>&1
  type nul > ".venv\.tables-tried"
)
".venv\Scripts\python.exe" -m controller_inbox run %*
if errorlevel 1 goto :fail
exit /b 0

:update
rem Reinstall with both add-ons, else with OCR only, else without them; errorlevel says if the last try failed.
echo Installing what this update needs...
".venv\Scripts\python.exe" -m pip install --quiet -e ".[ocr,tables]" >nul 2>&1 && exit /b 0
".venv\Scripts\python.exe" -m pip install --quiet -e ".[ocr]" >nul 2>&1 && exit /b 0
".venv\Scripts\python.exe" -m pip install --quiet -e .
exit /b

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
rem The OCR add-on reads scanned PDFs; if it can't install here, CloseDesk runs without it.
".venv\Scripts\python.exe" -m pip install --quiet -e ".[ocr]" || ".venv\Scripts\python.exe" -m pip install --quiet -e .
if errorlevel 1 exit /b 1
echo Setup finished.
exit /b 0

:fail
echo.
echo CloseDesk stopped. The message above says why.
pause
exit /b 1
