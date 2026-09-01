@echo off
chcp 65001 >nul
cd /d "%~dp0"
title WINTICKET Mail PC Agent

echo ==========================================
echo   WINTICKET Mail PC Agent
echo ==========================================
echo.
echo ※「WINTICKET 全データ保存版」は終了しないでOKです。
echo ※このメール通知版だけを専用ロックで二重起動防止します。
echo.

if not exist "config.txt" (
  echo [ERROR] config.txt が見つかりません。
  echo.
  pause
  exit /b 1
)

echo config.txt を読み込んで起動します...
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  py pc_agent.py
) else (
  python pc_agent.py
)

set EXITCODE=%errorlevel%
echo.
echo PC Agent が終了しました。終了コード=%EXITCODE%
echo この画面は自動では閉じません。
pause
