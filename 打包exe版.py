# -*- coding: utf-8 -*-
"""打包「exe 版」发布包 —— 单个可执行文件

用 PyInstaller --onefile 把 `exe启动器.py` 打成 `太平洋战线求解器.exe`：
内嵌 Python 解释器 + 全部依赖（含 OCR 引擎与模型）+ 整个发布内容，
收包方双击即用 —— 不用装 Python，也不用联网。

内嵌的发布内容不以 `--add-data 目录` 的方式塞进去（PyInstaller 对目录的落点语义
容易踩坑），而是先打成一个 `_payload.zip`，运行时由启动器按清单解到「运行目录」。

用法：
    .venv/Scripts/python 打包exe版.py                 # 打包 + 实测（跑包内自检）
    .venv/Scripts/python 打包exe版.py --no-zip        # 不额外产出分发用 zip
    .venv/Scripts/python 打包exe版.py --no-test       # 只打包

产物（dist/）：
    太平洋战线求解器.exe                     单文件，双击即用
    太平洋战线求解器-exe版_YYYY-MM-DD.zip    分发用（内含该 exe + 使用说明.txt）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import 打包公共 as C

LAUNCHER = 'exe启动器.py'
APP = C.PKG + '.exe'

# cv2 / onnxruntime 的原生扩展依赖 MSVC 运行库（VC 可再发行组件允许随程序分发）
EXTRA_DLLS = ['msvcp140.dll', 'concrt140.dll']

# rapidocr 的模型/config 要整包带上；onnxruntime 由官方 hook 处理
COLLECT_ALL = ['rapidocr_onnxruntime']
HIDDEN = ['cv2', 'numpy', 'PIL', 'PIL.Image', 'onnxruntime',
          'rapidocr_onnxruntime', 'pyclipper', 'shapely', 'yaml', 'six']

README_TXT = """太平洋战线求解器（exe 版）
================================

双击「{app}」就会弹出菜单：
  1 整局求解   2 单阶段求解   3 查看方案面板
  4 编写输入   5 自检         6 打开运行目录

本文件是单个可执行文件，不需要安装 Python，也不需要联网。

第一次运行会先把运行内容写到：
    %LOCALAPPDATA%\\{pkg}
（首次约几秒；之后启动很快。）
求解结果、面板 HTML、你写的输入都在那个目录的 data\\ 里，
菜单里选「6 打开运行目录」就能看到。

想在 U 盘里随身带走？在 exe 旁边新建一个空的 portable.txt，
运行目录就改成 exe 所在文件夹（便携模式）。
"""


def make_payload_zip(stage, out):
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        n = 0
        for p in sorted(stage.rglob('*')):
            if p.is_file():
                z.write(p, str(p.relative_to(stage)).replace('\\', '/'))
                n += 1
    print('  内嵌 payload：%d 个文件，%.1f MB' % (n, out.stat().st_size / 1048576))
    return out


def pyinstaller_args(payload):
    sysroot = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    work = C.WORK / 'pyi'
    args = [sys.executable, '-m', 'PyInstaller',
            '--noconfirm', '--clean', '--log-level', 'WARN',
            '--onefile', '--console',
            '--name', C.PKG,
            '--distpath', str(C.DIST),
            '--workpath', str(work),
            '--specpath', str(work),
            '--exclude-module', 'tkinter',
            '--exclude-module', '_tkinter',
            '--add-data', '%s%s.' % (payload, os.pathsep)]
    for mod in COLLECT_ALL:
        args += ['--collect-all', mod]
    for mod in HIDDEN:
        args += ['--hidden-import', mod]
    for dll in EXTRA_DLLS:
        if (sysroot / dll).exists():
            args += ['--add-binary', '%s%s.' % (sysroot / dll, os.pathsep)]
    args.append(str(C.ROOT / LAUNCHER))
    return args


def selftest_exe(exe, tmp_home):
    """真跑一遍产物：把发布内容解到临时运行目录，用包内自检确认能起来"""
    print('  实测：%s selftest（运行目录 %s）' % (exe.name, tmp_home))
    env = dict(os.environ, PZ_SOLVER_HOME=str(tmp_home))
    r = C.run([exe, 'selftest'], env=env, check=False, capture_output=True,
              text=True, encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    for line in out.strip().splitlines()[-6:]:
        print('    | %s' % line)
    if r.returncode != 0 or '全部通过' not in out or '未达标 0 个' not in out:
        print(out[-3000:])
        raise SystemExit('✗ exe 自检未通过（退出码 %s）' % r.returncode)
    print('  ✓ exe 自检通过（回归 + 端到端演练；运行目录里铺开 %d 个文件）'
          % sum(1 for p in tmp_home.rglob('*') if p.is_file()))


def main(argv):
    do_test = '--no-test' not in argv
    do_zip = '--no-zip' not in argv

    print('== exe 版 ==')
    stage = C.snapshot()
    payload = make_payload_zip(stage, C.WORK / '_payload.zip')

    args = pyinstaller_args(payload)
    print('  PyInstaller --onefile ...（几分钟，日志只在出错时打印）')
    r = subprocess.run([str(a) for a in args], cwd=C.ROOT,
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0 or 'ERROR' in (r.stdout or '') + (r.stderr or ''):
        print(r.stdout[-4000:])
        print(r.stderr[-4000:])
        raise SystemExit('✗ PyInstaller 失败（退出码 %d）' % r.returncode)

    exe = C.DIST / APP
    if not exe.exists():
        raise SystemExit('✗ 没有产出 %s' % exe)
    print('  → %s（%.1f MB）' % (exe.name, exe.stat().st_size / 1048576))

    if do_test:
        tmp_home = C.WORK / 'exe_home'
        if tmp_home.exists():
            shutil.rmtree(tmp_home)
        selftest_exe(exe, tmp_home)

    if do_zip:
        note = C.WORK / '使用说明.txt'
        note.write_text(README_TXT.format(app=APP, pkg=C.PKG), encoding='utf-8')
        out = C.dist_path('%s-exe版_%s.zip' % (C.PKG, C.today()))
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(exe, APP)
            z.write(note, '使用说明.txt')
        print('  → %s（%.1f MB）' % (out.name, out.stat().st_size / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
