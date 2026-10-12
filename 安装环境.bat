@echo off
chcp 65001 >nul
cd /d "%~dp0"

set MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple
set MIRROR2=https://mirrors.aliyun.com/pypi/simple/

echo == 太平洋战线求解器 · 环境安装 ==
echo.

rem ---------- 1. 选解释器：优先 3.13~3.10（这些版本各依赖都有现成安装包） ----------
set PY=
for %%V in (3.13 3.12 3.11 3.10) do (
    if not defined PY (
        py -%%V -V >nul 2>nul && set PY=py -%%V
    )
)
if not defined PY (
    where py >nul 2>nul && set PY=py -3
)
if not defined PY (
    where python >nul 2>nul && set PY=python
)
if not defined PY (
    echo 未找到 Python。请先安装 Python 3.10~3.13（安装时勾选 "Add to PATH"）后重试。
    pause & exit /b 1
)

%PY% --version || (echo Python 无法运行 & pause & exit /b 1)
for /f "tokens=2" %%v in ('%PY% -V 2^>^&1') do set PYVER=%%v
echo 使用解释器: %PY%   ^(Python %PYVER%^)

echo %PYVER% | findstr /b "3.14 3.15 3.16 3.17 3.18" >nul
if not errorlevel 1 (
    echo.
    echo [提示] Python %PYVER% 是很新的版本,numpy / onnxruntime 等可能还没有
    echo        对应的安装包。若下面安装失败,请改装 Python 3.12 再运行本脚本。
    echo.
)

rem ---------- 2. 虚拟环境 ----------
if not exist .venv\Scripts\python.exe (
    echo 创建虚拟环境 .venv ...
    %PY% -m venv .venv || (echo venv 创建失败 & pause & exit /b 1)
)

.venv\Scripts\python -m pip install --upgrade pip -q --disable-pip-version-check

rem ---------- 3. 装依赖：默认源 → 清华 → 阿里云（依次回退） ----------
echo 安装依赖（首次约 100MB,包含 OCR 引擎,请耐心等待）...
.venv\Scripts\python -m pip install -r requirements.txt -q --disable-pip-version-check
if not errorlevel 1 goto done

echo.
echo 默认源失败（常见原因:连不上 pypi.org 或网络慢）,改用清华镜像重试...
.venv\Scripts\python -m pip install -r requirements.txt -q --disable-pip-version-check -i %MIRROR%
if not errorlevel 1 goto done

echo.
echo 清华镜像也失败,再试阿里云镜像...
.venv\Scripts\python -m pip install -r requirements.txt -q --disable-pip-version-check -i %MIRROR2%
if not errorlevel 1 goto done

echo.
echo == 安装失败 ==
echo 请检查网络连接(若使用代理,请先设好 http_proxy / https_proxy 环境变量)。
echo 也可手动重试,例如:
echo     .venv\Scripts\python -m pip install -r requirements.txt -i %MIRROR%
pause & exit /b 1

:done
echo.
echo == 安装完成! ==
echo   求解      双击「求解整局.bat」（或「求解单阶段.bat」）
echo   写输入    双击「编写输入.bat」（交互式生成 data\player_input.json）
echo   看建议    双击「查看面板.bat」
echo 自检: .venv\Scripts\python code\test_m5.py  ^(末行应显示「全部通过」^)
pause
