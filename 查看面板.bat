@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo 尚未安装环境 —— 请先双击「安装环境.bat」
    pause & exit /b 1
)
if not exist data\plan.json (
    echo 还没有求解结果 —— 请先双击「求解整局.bat」
    pause & exit /b 1
)

.venv\Scripts\python code\m12_panel.py
start "" data\panel.html
pause
