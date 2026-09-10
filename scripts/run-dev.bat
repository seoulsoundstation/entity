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
echo Python executable not found. Install Python 3.10+ or set PYTHON_EXE.
exit /b 1
:explicit
"%PYTHON_EXE%" -m naver_blog_archive %*
exit /b %ERRORLEVEL%
:venv
".venv\Scripts\python.exe" -m naver_blog_archive %*
exit /b %ERRORLEVEL%
:python
python -m naver_blog_archive %*
exit /b %ERRORLEVEL%
:launcher
py -3 -m naver_blog_archive %*
exit /b %ERRORLEVEL%
