@echo off
rem Double-click to read the drop folder, let the local model read, write today's digest,
rem and open the dashboard.
rem
rem The first run sets everything up, and every start checks the set-up and puts right what isn't (after an
rem update, a moved or replaced folder, a Python that went away).
rem
rem Python 3.11 or newer is the one thing CloseDesk needs on the computer. The Python that is there is used;
rem none is ever installed unasked. Where there is none, CloseDesk says so and how to get it.
rem
rem   CloseDesk.bat               start CloseDesk
rem   CloseDesk.bat --check       say what is and isn't in order, without starting it
rem   CloseDesk.bat --update      fetch the newest CloseDesk (mail and settings are kept)
rem   CloseDesk.bat --shortcut    add the CloseDesk shortcut (Desktop and Start menu) again
rem   CloseDesk.bat --get-python  only when asked: download a Python for CloseDesk alone into your user
rem                               folder (no admin rights; nothing is installed on the computer), and use it
setlocal EnableExtensions
rem Into CloseDesk's folder. With pushd, not cd: cmd can't work in a folder on a network share (\\server\share, which
rem is how Windows names a mapped drive such as H: to CloseDesk's shortcut and window), and pushd lends it a drive
rem letter meanwhile. Every way out of this file gives the letter back (popd).
pushd "%~dp0"

rem Where --get-python keeps the one tool it downloads (uv, which fetches Python): in your own folder.
set "TOOLS=%LOCALAPPDATA%\CloseDesk"
set "UV="
set "PY="
set /a TRIES=0

rem --get-python is for this file, not for CloseDesk: it comes first on the line when used. Asked for, the
rem environment is made again, with the downloaded Python.
set "GETPY="
if /i "%~1"=="--get-python" set "GETPY=1"
if defined GETPY if exist ".venv" rmdir /s /q ".venv"

:again
set /a TRIES+=1
if %TRIES% GTR 3 goto :gave_up
set "PY="
if exist ".venv\Scripts\python.exe" call :try ".venv\Scripts\python.exe"
if defined PY goto :start

rem No environment, or one whose Python is gone (it was removed, upgraded, or made on another computer).
if exist ".venv" set "CLOSEDESK_AGAIN=1"
if exist ".venv" rmdir /s /q ".venv"
if defined GETPY goto :get_python
call :find_python
if defined PY goto :start
goto :no_python

:get_python
call :managed
if defined PY goto :start
echo.
echo The download didn't work. Check the internet connection and try again.
echo Nothing was changed, except that CloseDesk will set itself up again on its next start.
pause
popd
exit /b 1

:start
if defined UV set "CLOSEDESK_UV=%UV%"
if defined GETPY goto :start_plain
rem An update replaces this file while it runs, so that one is run from a block read whole beforehand. With
rem call, a Python that is a .bat file comes back here too. (In a block, the exit code is kept with call set and
rem given back with cmd /c exit, after popd.)
if /i "%~1"=="--update" (
  call %PY% scripts\launch.py %*
  call set "CODE=%%ERRORLEVEL%%"
  echo.
  pause
  popd
  call cmd /d /c exit %%CODE%%
  exit /b
)
call %PY% scripts\launch.py %*
goto :ran

rem After --get-python: the rest of the line is passed on, without it.
:start_plain
if /i "%~2"=="--update" (
  call %PY% scripts\launch.py %2 %3 %4 %5 %6 %7 %8 %9
  call set "CODE=%%ERRORLEVEL%%"
  echo.
  pause
  popd
  call cmd /d /c exit %%CODE%%
  exit /b
)
call %PY% scripts\launch.py %2 %3 %4 %5 %6 %7 %8 %9

:ran
set "CODE=%ERRORLEVEL%"
if "%CODE%"=="0" (popd & exit /b 0)
if "%CODE%"=="130" (popd & exit /b 0)
rem 75: Settings' Update button. CloseDesk stopped so that its newest version can be fetched and started.
if "%CODE%"=="75" goto :update_and_reopen
rem 76: launch.py says the environment is beyond repair; it is made again, once.
if "%CODE%"=="76" set "CLOSEDESK_REBUILT=1"
if "%CODE%"=="76" goto :remake
rem 77: an update runs in this folder now; launch.py said so, and nothing was started.
if "%CODE%"=="77" goto :updating
goto :fail

:remake
if exist ".venv" rmdir /s /q ".venv"
goto :again

rem Settings' Update button: fetch the newest CloseDesk (mail and settings are kept), then start the new one.
rem The update replaces this file while it runs, and cmd reads a .bat from the disk as it goes, so the update
rem and the start of the new file are one block, read whole before it runs. The environment is there now and
rem stays, so the new start is without --get-python. The page that asked for the update is still open and
rem reloads itself, so no second one is opened (CLOSEDESK_REOPEN). The new file goes into the folder itself (popd
rem first: it lends itself a drive letter of its own where it needs one).
:update_and_reopen
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
set "CLOSEDESK_REOPEN=1"
echo.
if defined GETPY (
  call %PY% scripts\launch.py --update
  popd
  call "%~f0" %2 %3 %4 %5 %6 %7 %8 %9
  exit /b
)
(
  call %PY% scripts\launch.py --update
  popd
  call "%~f0" %*
  exit /b
)

rem Sets PY to the command given when it is a Python 3.11 or newer that runs. (The "python" Windows offers
rem before one is installed only points at the Store; it fails this, and nothing opens.) Only an exit of exactly 0
rem counts: the "py" of Windows' Python install manager ends below 0 for a version it hasn't got ("No runtime
rem installed that matches 3.12"). A name (py, python) is run with call, so one that is a .bat file (pyenv-win's)
rem comes back here; a path to an .exe is run as it is, so a % or ^ in it is kept.
:try
if defined PY exit /b 0
set "RUNPY=call"
if /i "%~x1"==".exe" set "RUNPY="
%RUNPY% %* -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if "%ERRORLEVEL%"=="0" set PY=%*
exit /b 0

rem A Python 3.11 or newer already on this computer.
:find_python
set "PY="
if defined CLOSEDESK_PYTHON call :try "%CLOSEDESK_PYTHON%"
call :try py -3.12
call :try py -3.11
call :try py -3.13
call :try py -3.14
call :try py -3
call :try python
call :try python3
exit /b 0

rem uv: already installed, already fetched, or fetched now from its GitHub releases (one file, no installer).
:get_uv
set "UV="
for %%I in (uv.exe) do if not "%%~$PATH:I"=="" set "UV=%%~$PATH:I"
if defined UV exit /b 0
if exist "%TOOLS%\uv.exe" set "UV=%TOOLS%\uv.exe"
if defined UV exit /b 0
where curl >nul 2>&1 || exit /b 0
where tar >nul 2>&1 || exit /b 0
set "ASSET=uv-x86_64-pc-windows-msvc"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ASSET=uv-aarch64-pc-windows-msvc"
set "UVURL=https://github.com/astral-sh/uv/releases/latest/download"
if defined CLOSEDESK_UV_URL set "UVURL=%CLOSEDESK_UV_URL%"
set "TMPUV=%TEMP%\closedesk-uv-%RANDOM%"
mkdir "%TMPUV%" >nul 2>&1
curl -fsSL --retry 2 -o "%TMPUV%\uv.zip" "%UVURL%/%ASSET%.zip" >nul 2>&1
if exist "%TMPUV%\uv.zip" tar -xf "%TMPUV%\uv.zip" -C "%TMPUV%" >nul 2>&1
if not exist "%TOOLS%" mkdir "%TOOLS%" >nul 2>&1
if exist "%TMPUV%\uv.exe" move /y "%TMPUV%\uv.exe" "%TOOLS%\uv.exe" >nul 2>&1
if exist "%TMPUV%\%ASSET%\uv.exe" move /y "%TMPUV%\%ASSET%\uv.exe" "%TOOLS%\uv.exe" >nul 2>&1
rmdir /s /q "%TMPUV%" >nul 2>&1
if exist "%TOOLS%\uv.exe" set "UV=%TOOLS%\uv.exe"
exit /b 0

rem --get-python: a Python for CloseDesk alone, in your user folder. Nothing is installed on the computer and
rem no rights are asked. Never done unasked.
:managed
set "PY="
call :get_uv
if not defined UV exit /b 0
echo Downloading a Python for CloseDesk into your user folder, no admin rights needed. This takes a minute...
if exist ".venv" rmdir /s /q ".venv"
rem The computer's own certificates, so a company proxy's is trusted as it is by the browser.
set "UV_SYSTEM_CERTS=1"
set "UV_NATIVE_TLS=1"
set "UV_PYTHON_PREFERENCE=only-managed"
"%UV%" venv --quiet --seed --python 3.12 .venv >nul 2>&1
set "UV_PYTHON_PREFERENCE="
if exist ".venv\Scripts\python.exe" call :try ".venv\Scripts\python.exe"
exit /b 0

:no_python
echo.
echo CloseDesk needs Python 3.11 or newer (3.12 is the best choice), and this computer has none it can use.
echo.
echo Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
echo "Install for me only" needs no admin rights. Then double-click CloseDesk again.
echo.
echo If you can't install it, CloseDesk can download a copy for itself into your own folder instead,
echo with nothing installed on the computer. Run this in a Command Prompt:
echo.
echo   "%~f0" --get-python
pause
popd
exit /b 1

:gave_up
echo.
echo CloseDesk couldn't finish setting up. Double-click it again; if this repeats, run: CloseDesk.bat --check
pause
popd
exit /b 1

:fail
echo.
echo CloseDesk stopped. The message above says why.
pause
popd
exit /b 1

:updating
echo.
pause
popd
exit /b 77
