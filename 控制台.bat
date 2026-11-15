@echo off
rem 双击这个就能开控制台。用 pythonw 是为了不留一个黑框(出错会写 wxduo_console.err.log)
chcp 65001 >nul
cd /d "%~dp0"
set "PYW=%USERPROFILE%\AppData\Local\Programs\Python\Python313\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"
start "" "%PYW%" "%~dp0wxduo_console.py"

