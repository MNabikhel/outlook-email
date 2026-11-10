@echo off
rem Double-click to update CloseDesk to the newest version. Your mail and settings are kept, and the files
rem it replaces are put in .previous. Then double-click CloseDesk as usual.
cd /d "%~dp0"
rem This file is replaced by the update too, so the rest is one block, read whole before it starts.
(
  call CloseDesk.bat --update
  exit /b
)
