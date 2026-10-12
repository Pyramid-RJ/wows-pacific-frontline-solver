# -*- coding: utf-8 -*-
"""打包「批处理版」发布包 —— 两种形态

    不含初始环境   只含源码与数据；收包方需自备 Python 3.10+ 并双击「安装环境.bat」联网装依赖
    含初始环境     额外内置便携运行时（嵌入式 Python 3.12 + 全部依赖，含 OCR 引擎），
                   收包方解压即用：不装 Python、不联网

用法：
    .venv/Scripts/python 打包批处理版.py              # 两种都打
    .venv/Scripts/python 打包批处理版.py --light      # 只打「不含环境」
    .venv/Scripts/python 打包批处理版.py --full       # 只打「含环境」
    .venv/Scripts/python 打包批处理版.py --selftest   # 打完后用包内运行时跑自检
    .venv/Scripts/python 打包批处理版.py --deep       # 自检 + 端到端演练（整套求解，约 2 分钟）

产物（dist/）：
    太平洋战线求解器-批处理版-不含环境_YYYY-MM-DD.zip
    太平洋战线求解器-批处理版-含环境_YYYY-MM-DD.zip

「含环境」为什么不用本机 .venv 直接拷贝：Windows 的 venv 不可搬迁 ——
.venv/Lib 里只有 site-packages，标准库在基础 Python 安装目录（pyvenv.cfg 的 home）。
所以这里改用官方「嵌入式发行版」当解释器，把依赖装进 .venv/Lib/site-packages，
再用 .venv/Scripts/python312._pth 把它接上；这样 .venv\\Scripts\\python.exe 这个
路径不变，现有 5 个 .bat 一行都不用改。
"""
from __future__ import annotations

import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

import 打包公共 as C

# 嵌入式发行版（比 python.org 直连稳；逐个回退）
EMBED_VER = '3.12.10'
EMBED_URLS = [
    'https://mirrors.huaweicloud.com/python/%s/python-%s-embed-amd64.zip' % (EMBED_VER, EMBED_VER),
    'https://registry.npmmirror.com/-/binary/python/%s/python-%s-embed-amd64.zip' % (EMBED_VER, EMBED_VER),
    'https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip' % (EMBED_VER, EMBED_VER),
]

# cv2 / onnxruntime 依赖 MSVC 运行库；VC 可再发行组件允许随程序分发
EXTRA_DLLS = ['msvcp140.dll', 'concrt140.dll']

# 打包机器上可能有、但绝不该发给用户的构建工具（含它们的 dist-info）
BUILD_TOOLS = [
    'PyInstaller', 'pyinstaller*', 'pyinstaller_hooks_contrib*',
    'altgraph*', 'pefile*', 'pywin32_ctypes*', 'setuptools*',
    'pkg_resources', '_distutils_hack', 'distutils-precedence.pth',
    '__pycache__', '*.pyc', '*.pyo',
]

PTH_BODY = ('python312.zip\n'
            '.\n'
            '..\\Lib\\site-packages\n'
            '..\\..\\code\n'
            'import site\n')

NOTE_NAME = '本包已内置运行环境-请先看这个.txt'
NOTE_TEXT = """本包已内置运行环境（不用装 Python、不用联网）
=================================================

这是「批处理版 · 含初始环境」发布包。与「不含环境」版的区别只有一条：

    .venv\\  目录里已经装好了 Python 3.12 解释器与全部依赖（含 OCR 引擎）。

所以：直接双击「求解整局.bat」就能用，不需要先装 Python、也不需要联网。

  第一次使用前，仍建议先双击「编写输入.bat」，把「自己没有的卡」取消勾选、
  填上战损格数与当前阶段（卡牌默认按全部拥有处理）。

关于「安装环境.bat」：
  那是给「不含环境」版（自备 Python + 联网装依赖）用的。
  在本包里它会去找系统 Python，找不到会提示「未找到 Python」——
  那不是故障，本包不需要运行它。

运行目录就是本文件夹；求解结果写在 data\\ 下（plan.json / panel.html 等）。
"""


# ---------------------------------------------------------------- 嵌入式解释器
def ensure_embed():
    """下载并解出嵌入式 Python，返回解释器所在目录（缓存于 .work/pkg/）"""
    out = C.WORK / 'pyembed'
    zip_path = C.WORK / 'pyembed.zip'
    if (out / 'python.exe').exists():
        return out
    C.WORK.mkdir(parents=True, exist_ok=True)
    if not zip_path.exists():
        last = None
        for url in EMBED_URLS:
            try:
                print('  下载嵌入式 Python: %s' % url)
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=120) as r, \
                        zip_path.open('wb') as f:
                    shutil.copyfileobj(r, f)
                break
            except Exception as e:                       # noqa: BLE001
                last = e
                print('    失败：%s' % e)
        else:
            raise SystemExit('✗ 嵌入式 Python 下载失败（已试 %d 个源）：%s'
                             % (len(EMBED_URLS), last))
    if out.exists():
        shutil.rmtree(out)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    return out


def build_runtime(stage):
    """在 stage/.venv 下拼出便携运行时；返回该目录"""
    embed = ensure_embed()
    venv = stage / '.venv'
    scripts = venv / 'Scripts'
    sp_dst = venv / 'Lib' / 'site-packages'
    if venv.exists():
        shutil.rmtree(venv)
    scripts.mkdir(parents=True)

    # 1) 嵌入式解释器（python.exe / python312.dll / python312.zip / 扩展模块）
    shutil.copytree(embed, scripts, dirs_exist_ok=True)

    # 2) MSVC 运行库（cv2、onnxruntime 的原生扩展要它）
    sysroot = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    for dll in EXTRA_DLLS:
        if (sysroot / dll).exists():
            shutil.copy2(sysroot / dll, scripts / dll)

    # 3) 把依赖接到解释器上：._pth 的条目相对 .venv/Scripts
    #    必须写字节：Windows 下 write_text 会把 \n 翻成 \r\n
    pth = list(scripts.glob('*._pth'))
    if not pth:
        raise SystemExit('✗ 嵌入式发行版里没有 ._pth（版本变了？）')
    pth[0].write_bytes(PTH_BODY.encode('ascii'))

    # 4) 依赖（本机 .venv 的 site-packages，就是回归验证过的那一套）
    src = C.ROOT / '.venv' / 'Lib' / 'site-packages'
    if not src.exists():
        raise SystemExit('✗ 找不到 .venv/Lib/site-packages，请先建好开发环境')
    shutil.copytree(src, sp_dst,
                    ignore=shutil.ignore_patterns(*BUILD_TOOLS))
    n = sum(1 for _ in sp_dst.rglob('*') if _.is_file())
    print('  便携运行时：嵌入式 Python %s + %d 个依赖文件' % (EMBED_VER, n))
    return venv


def check_runtime(zip_path):
    """校验「含环境」成品：解释器与依赖在、构建工具不在、._pth 接对了"""
    with zipfile.ZipFile(zip_path) as z:
        names = [n[len(C.PREFIX):] for n in z.namelist()
                 if n.startswith(C.PREFIX) and not n.endswith('/')]
        body = ''
        try:
            body = z.read(C.PREFIX + '.venv/Scripts/python312._pth').decode()
        except KeyError:
            pass
    rel = set(names)
    for must in ('.venv/Scripts/python.exe',
                 '.venv/Scripts/python312.zip',
                 '.venv/Scripts/msvcp140.dll',
                 '.venv/Lib/site-packages/numpy/__init__.py',
                 '.venv/Lib/site-packages/cv2/__init__.py',
                 '.venv/Lib/site-packages/onnxruntime/capi/onnxruntime.dll',
                 '.venv/Lib/site-packages/rapidocr_onnxruntime/models/'
                 'ch_PP-OCRv4_det_infer.onnx'):
        if must not in rel:
            raise SystemExit('✗ 含环境包缺少运行时文件：%s' % must)

    # 构建工具（含 dist-info）绝不能发给用户
    def pkg_name(rel_path):
        parts = rel_path.split('/')
        if parts[:3] == ['.venv', 'Lib', 'site-packages']:
            return parts[3] if len(parts) > 3 else ''
        if parts[:2] == ['.venv', 'Scripts']:
            return parts[2] if len(parts) > 2 else ''
        return ''
    banned = ('pyinstaller', 'altgraph', 'pefile', 'pywin32_ctypes',
              'setuptools', 'pkg_resources', '_distutils_hack')
    stray = [r for r in names if pkg_name(r).lower().startswith(banned)]
    if stray:
        raise SystemExit('✗ 含环境包混入构建工具：%s' % stray[:5])

    if body != PTH_BODY:
        raise SystemExit('✗ ._pth 内容不对：%r' % body)
    print('  ✓ 运行时校验通过（嵌入式解释器 + 依赖 + ._pth）')


def selftest(stage, deep=False):
    """用「包内运行时」跑自检：导入识别层依赖 + 求解层回归（+ 端到端演练）"""
    py = stage / '.venv' / 'Scripts' / 'python.exe'
    if not py.exists():
        print('  （跳过自检：本包不含运行时）')
        return
    code = ('import json, http.server, unittest, ssl, argparse\n'
            'import numpy, PIL, cv2, onnxruntime, rapidocr_onnxruntime\n'
            'print("外挂依赖 OK:", cv2.__version__, numpy.__version__)')
    C.run([py, '-c', code])
    C.run([py, str(stage / 'code' / 'test_m5.py')])
    if deep:
        r = C.run([py, str(stage / 'examples' / 'solve_demo.py')],
                  check=False, capture_output=True, text=True,
                  encoding='utf-8', errors='replace')
        out = (r.stdout or '') + (r.stderr or '')
        last = out.strip().splitlines()[-1] if out.strip() else '(无输出)'
        print('  端到端演练末行：%s' % last)
        if r.returncode != 0 or '未达标 0 个' not in out:
            print(out[-3000:])
            raise SystemExit('✗ 端到端演练未通过')


# ---------------------------------------------------------------- 两种产物
def build_light(do_test=False, deep=False):
    print('== 批处理版 · 不含初始环境 ==')
    stage = C.snapshot()
    out = C.dist_path('%s-批处理版-不含环境_%s.zip' % (C.PKG, C.today()))
    C.zip_dir(stage, out)
    C.check_zip(out)
    if do_test:
        selftest(stage, deep)
    return out


def build_full(do_test=False, deep=False):
    print('== 批处理版 · 含初始环境 ==')
    stage = C.snapshot()
    build_runtime(stage)
    note = C.WORK / NOTE_NAME
    note.write_bytes(NOTE_TEXT.encode('utf-8'))
    out = C.dist_path('%s-批处理版-含环境_%s.zip' % (C.PKG, C.today()))
    C.zip_dir(stage, out, extra=[(note, NOTE_NAME)])
    C.check_zip(out)
    check_runtime(out)
    if do_test:
        selftest(stage, deep)
    return out


def main(argv):
    want_light = '--full' not in argv
    want_full = '--light' not in argv
    do_test = '--selftest' in argv or '--deep' in argv
    deep = '--deep' in argv
    outs = []
    if want_light:
        outs.append(build_light(do_test, deep))
    if want_full:
        outs.append(build_full(do_test, deep))
    print('\n完成：')
    for o in outs:
        print('  %s（%.1f MB）' % (o.name, o.stat().st_size / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
