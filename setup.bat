@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0scripts\setup.bat"
set "NBA_EXIT=%ERRORLEVEL%"
if "%NBA_EXIT%"=="0" echo Setup complete. Double-click start-gui.bat to open the program.
if not "%NBA_EXIT%"=="0" echo Setup failed. Read the error above, correct it, and run setup.bat again.
pause
exit /b %NBA_EXIT%
