@echo off
chcp 65001 >nul 2>&1
setlocal
cd /d "%~dp0"

REM 启动平台（自动检测环境和依赖已集成在 app.py 中）
python app.py

if errorlevel 1 (
    echo.
    echo 工具平台启动失败，请检查上方错误信息。
    pause
)
