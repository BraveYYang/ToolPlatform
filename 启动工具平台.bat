@echo off
setlocal
cd /d "%~dp0"
python app.py
if errorlevel 1 (
    echo.
    echo 工具平台启动失败，请检查上方错误信息。
    pause
)
