@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "scripts\generate_user_password_hash.py" %*
) else (
  python "scripts\generate_user_password_hash.py" %*
)
pause
