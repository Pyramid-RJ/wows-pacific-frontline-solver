# -*- coding: utf-8 -*-
"""发布打包公共逻辑

发布内容的唯一来源是 `git archive HEAD`：`.gitattributes` 里的 `export-ignore`
定义了「发布包瘦身」口径（排除示例大图、规则手册配图、docx、评估标注集）。
本模块只负责取内容 / 打包 / 校验，不含任何求解或识别逻辑。

被这两个脚本复用：
    打包批处理版.py   —— 批处理版（不含 / 含初始环境两种）
    打包exe版.py     —— 单文件 exe 版
"""
from __future__ import annotations

import shutil
import subprocess
import tarfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'
WORK = ROOT / '.work' / 'pkg'
PKG = '太平洋战线求解器'
PREFIX = PKG + '/'

# 发布包里必须存在的关键文件（缺一即打包失败，避免打出空包/残包）
KEY_FILES = [
    'README.md', '使用说明.md', 'requirements.txt',
    '安装环境.bat', '编写输入.bat', '求解整局.bat', '求解单阶段.bat', '查看面板.bat',
    'code/m11_plan.py', 'code/m12_panel.py', 'code/m13_input.py', 'code/m7_assign.py',
    'data/our_fleet.json', 'data/var_config.json', 'data/player_input.template.json',
    'data/stage_own/truth.json', 'data/stage/glyph_tpl.npz',
]

# 绝不允许混进发布包的本地产物 / 开发资料（子串匹配；.venv/ 内单独放宽）
FORBIDDEN = [
    '.git/', '__pycache__', '.pyc',
    'data/plan.json', 'data/panel.html', 'data/player_state.json',
    'data/player_input.json', 'data/input_editor.html', 'data/player_input.json.bak',
    'data/stage_real/', 'data/weak_labeled/images/', 'data/stage_own/s',
    '/figures/', '.docx',
]


def snapshot(ref='HEAD'):
    """把 git 跟踪内容导出到 .work/pkg/stage/（已按 export-ignore 瘦身）

    返回 stage 目录。每次调用都重建，保证打包内容 = 该提交的内容。
    """
    stage = WORK / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    tar = WORK / '_payload.tar'
    if tar.exists():
        tar.unlink()
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(tar), ref],
                   cwd=ROOT, check=True)
    with tarfile.open(tar) as tf:
        tf.extractall(stage, filter='data')
    tar.unlink()

    names = [str(p.relative_to(stage)).replace('\\', '/')
             for p in stage.rglob('*') if p.is_file()]
    print('  从 %s 导出 %d 个文件（%.1f MB）'
          % (ref, len(names), sum((stage / n).stat().st_size
                                  for n in names) / 1048576))
    return stage


def zip_dir(src, out, extra=None):
    """把 src 目录打成 zip（统一加 PKG/ 前缀）

    extra: [(磁盘文件, 包内相对路径)] 附加的生成文件
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(src.rglob('*')):
            if not p.is_file():
                continue
            rel = str(p.relative_to(src)).replace('\\', '/')
            z.write(p, PREFIX + rel)
            count += 1
        for disk, rel in (extra or []):
            z.write(disk, PREFIX + rel)
            count += 1
    print('  → %s（%d 个文件，%.1f MB）'
          % (out.name, count, out.stat().st_size / 1048576))
    return out


def check_zip(out, forbid=True):
    """校验成品 zip：关键文件齐全、无本地产物、无 pyvenv.cfg 之类的残留"""
    with zipfile.ZipFile(out) as z:
        names = [n for n in z.namelist() if not n.endswith('/')]
    rel = [n[len(PREFIX):] for n in names if n.startswith(PREFIX)]
    stray = [n for n in names if not n.startswith(PREFIX)]
    missing = [p for p in KEY_FILES if p not in rel]
    if missing:
        raise SystemExit('✗ %s 缺关键文件：%s' % (out.name, missing))
    if stray:
        raise SystemExit('✗ %s 顶层混入其它路径：%s' % (out.name, stray[:5]))
    if forbid:
        # .venv/ 是「含环境」包的正经内容，只挡它的本机残留（pyvenv.cfg 会指向打包机的 Python）
        bad = [r for r in rel
               if (r.startswith('.venv/') and r.endswith('pyvenv.cfg'))
               or (not r.startswith('.venv/') and any(s in r for s in FORBIDDEN))]
        if bad:
            raise SystemExit('✗ %s 混入本地产物：%s' % (out.name, bad[:5]))
    print('  ✓ 校验通过（%d 个文件）' % len(rel))
    return rel


def today():
    return date.today().isoformat()


def dist_path(name):
    DIST.mkdir(exist_ok=True)
    return DIST / name


def run(argv, **kw):
    print('[cmd]', ' '.join(str(a) for a in argv))
    kw.setdefault('check', True)
    return subprocess.run([str(a) for a in argv], cwd=ROOT, **kw)


__all__ = ['ROOT', 'DIST', 'WORK', 'PKG', 'PREFIX', 'KEY_FILES', 'FORBIDDEN',
           'snapshot', 'zip_dir', 'check_zip', 'today', 'dist_path', 'run']
