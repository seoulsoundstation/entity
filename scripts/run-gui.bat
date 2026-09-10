@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.." || exit /b 1
set "PYTHONPATH=%CD%\src"
set "PYTHONUTF8=1"
if not defined PLAYWRIGHT_BROWSERS_PATH if exist ".browsers" set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.browsers"
if defined PYTHON_EXE goto explicit
if exist ".venv\Scripts\python.exe" goto venv
where python >nul 2>nul
if not errorlevel 1 goto python
where py >nul 2>nul
if not errorlevel 1 goto launcher
goto missing
:explicit
set "NBA_PYTHON=%PYTHON_EXE%"
goto check
:venv
set "NBA_PYTHON=%CD%\.venv\Scripts\python.exe"
goto check
:python
for /f "delims=" %%P in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "NBA_PYTHON=%%P"
goto check
:launcher
for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "NBA_PYTHON=%%P"
:check
if not defined NBA_PYTHON goto missing
if not exist "%NBA_PYTHON%" goto missing
"%NBA_PYTHON%" -c "import sys; import tkinter; import naver_blog_archive.gui; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 goto setup
for %%P in ("%NBA_PYTHON%") do set "NBA_PYTHONW=%%~dpPpythonw.exe"
if not exist "%NBA_PYTHONW%" goto console
start "" "%NBA_PYTHONW%" -m naver_blog_archive gui %*
exit /b %ERRORLEVEL%
:console
echo pythonw.exe was not found; the program will use this console window.
"%NBA_PYTHON%" -m naver_blog_archive gui %*
exit /b %ERRORLEVEL%
:missing
echo Python executable not found. Install Python 3.10+ or set PYTHON_EXE.
echo Then double-click setup.bat in the project folder.
exit /b 1
:setup
echo The program could not start. Double-click setup.bat in the project folder.
echo Python must include Tcl/Tk. If using PYTHON_EXE, install dependencies for that interpreter.
exit /b 1
