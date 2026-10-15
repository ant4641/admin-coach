@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title 管到飛：發表前檢查

if not exist ".venv\Scripts\activate.bat" (
  echo [提醒] 找不到 .venv 虛擬環境。
  echo 請先在這個資料夾執行：
  echo   python -m venv .venv
  echo   .venv\Scripts\activate
  echo   pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"

python -m coach.check
set CHECK_EXIT=%ERRORLEVEL%

echo.
if "%CHECK_EXIT%"=="0" (echo 檢查結果：全部 OK) else (echo 檢查結果：有項目需要處理)
pause
exit /b %CHECK_EXIT%
