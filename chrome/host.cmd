@echo off
rem Native messaging host launcher for com.kincaid.hands (Chrome runs this; see install_host.py).
set "HANDS_PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%HANDS_PY%" set "HANDS_PY=python"
"%HANDS_PY%" -X utf8 -u "%~dp0host.py" %*
