@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0.." || exit /b 1
set "PYTHONUTF8=1"
if not defined PLAYWRIGHT_BROWSERS_PATH set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.browsers"
if exist ".venv\Scripts\python.exe" goto install
if defined PYTHON_EXE goto explicit
where python >nul 2>nul
if not errorlevel 1 goto python
where py >nul 2>nul
if not errorlevel 1 goto launcher
echo Install Python 3.10+ or set PYTHON_EXE.
exit /b 1
:explicit
"%PYTHON_EXE%" -m venv .venv
if errorlevel 1 exit /b 1
goto install
:python
python -m venv .venv
if errorlevel 1 exit /b 1
goto install
:launcher
py -3 -m venv .venv
if errorlevel 1 exit /b 1
:install
".venv\Scripts\python.exe" -m pip install -e ".[dev]"
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 exit /b 1
if not exist config.toml copy /y config.example.toml config.toml >nul
echo Optional local speech recognition: use the GUI preparation button or run prepare-transcription.
".venv\Scripts\python.exe" -m naver_blog_archive doctor
exit /b %ERRORLEVEL%
