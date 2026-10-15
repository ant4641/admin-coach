@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title 管到飛（僅本機）

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

echo 管到飛啟動中（只在本機執行，不啟動對外通道）。要結束請按 Ctrl+C 或關閉這個視窗。
echo 請不要在同一台電腦重複開多個 run.bat。
echo.
python -m coach.launch --no-tunnel

echo.
echo 已結束。
pause
