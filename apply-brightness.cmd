@echo off
rem 双击本文件即可把显示器亮度设成 brightness.txt 里的数值
setlocal
set "PY=D:\env\Anaconda\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0set_brightness.py" apply
echo.
pause
