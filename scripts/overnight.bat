@echo off
rem For Windows Task Scheduler: runs from the project folder no matter where the task starts.
rem   schtasks /Create /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 06:30 /TN "CloseDesk morning" /TR "\"C:\path\to\outlook-email\scripts\overnight.bat\""
cd /d "%~dp0\.."
if not exist "data\overnight" mkdir "data\overnight"
".venv\Scripts\python.exe" -m controller_inbox overnight >> "data\overnight\scheduler.log" 2>&1
