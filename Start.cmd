@echo off
setlocal
chcp 65001 >nul
set "SUBTITLE_BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if not exist "%SUBTITLE_BUNDLED_PYTHON%" goto try_py
"%SUBTITLE_BUNDLED_PYTHON%" --version >nul 2>&1
if errorlevel 1 goto try_py
"%SUBTITLE_BUNDLED_PYTHON%" -c "import sys, tkinter; assert sys.version_info >= (3, 9)" >nul 2>&1
if errorlevel 1 goto try_py
"%SUBTITLE_BUNDLED_PYTHON%" "%~dp0bootstrap.py" %*
exit /b %errorlevel%

:try_py
where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 --version >nul 2>&1
if errorlevel 1 goto try_python
py -3 -c "import sys, tkinter; assert sys.version_info >= (3, 9)" >nul 2>&1
if errorlevel 1 goto try_python
py -3 "%~dp0bootstrap.py" %*
exit /b %errorlevel%

:try_python
where python >nul 2>&1
if errorlevel 1 goto no_python
python --version >nul 2>&1
if errorlevel 1 goto no_python
python -c "import sys, tkinter; assert sys.version_info >= (3, 9)" >nul 2>&1
if errorlevel 1 goto no_python
python "%~dp0bootstrap.py" %*
exit /b %errorlevel%

:no_python
echo 未找到可运行并包含 Tkinter 的 Python 3.9 或更新版本。
echo 请安装 python.org 的 Windows Python，并在安装时保留 Tcl/Tk 组件。
echo 官方下载：https://www.python.org/downloads/windows/
echo 安装后再次双击 Start.cmd。
pause
exit /b 1
