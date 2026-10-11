@echo off
rem 双击本文件即可打开"亮度悬浮条"（无黑框窗口）
setlocal
set "PYW=D:\env\Anaconda\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0brightness_widget.py"
