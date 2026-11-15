@echo off
cd /d "%USERPROFILE%\Desktop\wxduo\chains\chat"
rem 日志轮转: 单文件超过 20MB 就先改名留一份, 再重新开始写。
rem 之前没有轮转, 快通道异常刷屏把 run.log 顶到了 56MB。
if exist run.log for %%F in ("run.log") do if %%~zF GTR 20971520 move /y "run.log" "run.log.1" >nul
"%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe" -u main.py >> run.log 2>&1
