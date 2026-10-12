# -*- coding: utf-8 -*-
"""打包「exe 版」发布包 —— 单个可执行文件（图形门户 · 绿色版）

用 PyInstaller --onefile --windowed 把 `exe启动器.py` 打成 `太平洋战线求解器.exe`：
内嵌 Python 解释器 + 全部依赖（含 OCR 引擎与模型）+ 整个发布内容，
收包方双击出一个图形门户，六个入口点一下就行 ——
不装 Python、不联网、不写注册表；运行内容就铺在 exe 旁边（绿色版）。

内嵌的发布内容不以 `--add-data 目录` 的方式塞进去（PyInstaller 对目录的落点语义
容易踩坑），而是先打成一个 `_payload.zip`，运行时由启动器按清单解到「运行目录」。

用法：
    .venv/Scripts/python 打包exe版.py                 # 打包 + 实测（包内自检）
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
REPORT = '自检报告.txt'

# cv2 / onnxruntime 的原生扩展依赖 MSVC 运行库（VC 可再发行组件允许随程序分发）
EXTRA_DLLS = ['msvcp140.dll', 'concrt140.dll']

# rapidocr 的模型/config 要整包带上；onnxruntime 由官方 hook 处理
COLLECT_ALL = ['rapidocr_onnxruntime']
HIDDEN = ['cv2', 'numpy', 'PIL', 'PIL.Image', 'onnxruntime',
          'rapidocr_onnxruntime', 'pyclipper', 'shapely', 'yaml', 'six',
          'tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.ttk']

README_TXT = """太平洋战线求解器（exe 便携版）
================================

双击「{app}」，出现一个窗口，上面六个入口：

    整局求解      选一个装着整局截图的文件夹，自动按阶段分组逐关求解
    单阶段求解    只求解当前一个阶段
    查看方案面板   打开图形面板，看每个据点的编队、变项与胜率
    编写输入      填你有哪些卡、战损格数、当前阶段与变项库存
    自检          求解层回归 + 端到端演练（出问题先跑这个）
    打开运行目录   求解结果与面板都在该目录的 data\\ 里

不需要安装 Python，也不需要联网。第一次运行会把运行内容铺到
**exe 所在文件夹**，之后整个软件就是「这个 exe + 它旁边那几个文件夹」，
拷到 U 盘、换台电脑都能直接用（绿色版）。

  第一次使用建议先点「编写输入」：卡牌默认按「全部拥有」处理，
  把「自己没有的卡」取消勾选、填上战损格数即可。

如果 exe 放在不能写的位置（例如 C:\\Program Files），运行内容会自动放到
%LOCALAPPDATA%\\{pkg}，窗口顶部会显示实际位置。
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
            '--onefile', '--windowed',          # 图形门户：不要黑框控制台
            '--name', C.PKG,
            '--distpath', str(C.DIST),
            '--workpath', str(work),
            '--specpath', str(work),
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
    """真跑一遍产物：把发布内容解到临时运行目录，用包内自检确认能起来

    窗口模式的 exe 没有 stdout 可抓，所以看它写出来的 `自检报告.txt` + 退出码。
    """
    print('  实测：%s --selftest（运行目录 %s）' % (exe.name, tmp_home))
    env = dict(os.environ, PZ_SOLVER_HOME=str(tmp_home))
    r = C.run([exe, '--selftest', '--quiet'], env=env, check=False,
              capture_output=True, text=True, encoding='utf-8', errors='replace')
    report = tmp_home / REPORT
    text = report.read_text(encoding='utf-8') if report.exists() else ''
    for line in text.strip().splitlines()[-7:]:
        print('    | %s' % line)
    need = ('图形门户：正常', '全部通过', '未达标 0 个')
    missing = [m for m in need if m not in text]
    if r.returncode != 0 or missing:
        print('（stdout: %r stderr: %r）' % ((r.stdout or '')[-800:],
                                            (r.stderr or '')[-800:]))
        raise SystemExit('✗ exe 自检未通过（退出码 %s，缺 %s，报告 %s）'
                         % (r.returncode, missing or '—',
                            report if report.exists() else '未生成'))
    print('  ✓ exe 自检通过（门户 + 回归 + 端到端演练；'
          '运行目录里铺开 %d 个文件）'
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
