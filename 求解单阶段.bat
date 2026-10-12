@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo 尚未安装环境 —— 请先双击「安装环境.bat」
    pause & exit /b 1
)

set DIR=%~1
if "%DIR%"=="" set /p DIR=把**当前阶段**的截图文件夹拖到本窗口后回车:
if not exist "%DIR%" (
    echo 文件夹不存在:%DIR%
    pause & exit /b 1
)

echo == 单阶段求解:%DIR% ==
echo （多阶段截图请改用「求解整局.bat」）
.venv\Scripts\python code\m11_plan.py --stage-dir "%DIR%"
if errorlevel 1 (
    echo.
    echo == 求解失败 —— 请查看上方的报错信息 ==
    echo 最常见原因：截图被弹窗遮挡（如「保存成功」）/ 混入了不同行动的截图
    echo             / 据点未解锁（无工具栏）/ 战力未读出需人工补录
    echo             / 截图带进了我方卡组栏（会干扰变项工具栏定位）
    pause & exit /b 1
)
.venv\Scripts\python code\m12_panel.py
if errorlevel 1 (
    echo == 面板生成失败，请截图上面的报错反馈 ==
    pause & exit /b 1
)
start "" data\panel.html
pause
