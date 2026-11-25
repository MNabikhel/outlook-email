@echo off
rem Double-click to update CloseDesk to the newest version. Your mail and settings are kept, and the files
rem it replaces are put in .previous. Then double-click CloseDesk as usual.
rem This file is replaced by the update too, so the rest is one block, read whole before it starts. CloseDesk.bat is
rem named by its whole path and goes into its folder itself (a folder on a network share too, which cmd can't be in).
(
  call "%~dp0CloseDesk.bat" --update
  exit /b
)
