@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
    echo 尚未安装环境 —— 请先双击「安装环境.bat」
    pause & exit /b 1
)

echo == 编写 player_input.json（交互式页面）==
echo 卡牌默认全部拥有,把「自己没有的卡」取消勾选即可。
echo 页面里点「保存到 data\player_input.json」直接写入（旧文件备份为 .bak）。
echo 关闭本窗口即结束服务。
echo.
.venv\Scripts\python code\m13_input.py --serve
pause
