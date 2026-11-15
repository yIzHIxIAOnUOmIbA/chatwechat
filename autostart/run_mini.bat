@echo off
cd /d "%USERPROFILE%\Desktop\wxduo\chains\agent"
"%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe" -u guard.py >> run.log 2>&1
