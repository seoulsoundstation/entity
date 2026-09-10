@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0scripts\run-gui.bat" %*
set "NBA_EXIT=%ERRORLEVEL%"
if not "%NBA_EXIT%"=="0" pause
exit /b %NBA_EXIT%
