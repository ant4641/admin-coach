@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title 管到飛

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

echo 管到飛啟動中，稍後會自動開啟 http://127.0.0.1:8000/
echo 要結束請關閉這個視窗，或按 Ctrl+C。
echo.
rem 等伺服器起來再開瀏覽器，避免開得太早連不上
start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8000/"

rem 不用 --reload：現場檔案變動時不要自動重啟
python -m uvicorn coach.api:app --host 127.0.0.1 --port 8000

echo.
echo 伺服器已停止。
pause
