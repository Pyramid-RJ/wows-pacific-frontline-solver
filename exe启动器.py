# -*- coding: utf-8 -*-
"""太平洋战线求解器 · 单文件 exe 启动器

被 `打包exe版.py` 用 PyInstaller 打成 `太平洋战线求解器.exe`（--onefile）。
收包方双击即用：不用装 Python，也不用联网。

运行流程
────────
1. 首次运行 / 版本变化时，把内嵌的发布内容（code/ data/ doc/ examples/ 文档）
   同步到「运行目录」：
       默认   %LOCALAPPDATA%\\太平洋战线求解器
       便携   在 exe 旁边放一个 portable.txt（内容随意），就改用 exe 所在目录
       指定   设环境变量 PZ_SOLVER_HOME=D:\\某处
   已存在的玩家本地产物（plan.json / player_input.json / player_state.json …）永不覆盖。
2. 弹出文字菜单；选中的动作转交给 code/ 下的原脚本执行。
3. 求解、输入、面板的产物都落在「运行目录\\data」下。

为什么不把 code/ 也冻结进 exe
────────────────────────────
code/ 下的模块统一用 `ROOT = Path(__file__).resolve().parent.parent` 定位 data/。
冻结后 __file__ 指向 PyInstaller 的临时解包目录（_MEIPASS），ROOT 会算到临时目录外面，
data/ 就找不到了。把 code/ 落在磁盘上、用 runpy 按真实路径执行，ROOT 永远指向运行目录。
"""
from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

APP = '太平洋战线求解器'
VERSION = '2026-10-13'

# 让 cv2 / onnxruntime 的原生扩展能在解包目录里找到依赖 DLL；句柄必须一直持有
_DLL_DIRS = []
if hasattr(os, 'add_dll_directory'):
    try:
        _mei = getattr(sys, '_MEIPASS', None)
        if _mei:
            _DLL_DIRS.append(os.add_dll_directory(_mei))
    except Exception:                                     # noqa: BLE001
        pass

PAYLOAD_TOP = ('code', 'data', 'doc', 'examples')
PAYLOAD_FILES = ('README.md', '使用说明.md', 'requirements.txt')
MANIFEST = '.payload.json'

# data/ 下属于「玩家本地产物」：升级时只补缺失，绝不覆盖
USER_KEEP = {'player_input.json', 'player_input.json.bak', 'plan.json',
             'panel.html', 'input_editor.html', 'player_state.json'}


# --------------------------------------------------------------------- 环境准备
def setup_console():
    """让控制台按 UTF-8 显示中文与 ✓/✗ 之类的符号"""
    os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
    except Exception:                                     # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                                 # noqa: BLE001
            pass


def frozen_src():
    """内嵌发布内容的根（onefile 下就是临时解包目录）"""
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def app_home():
    """运行目录（release 内容与玩家产物都住这儿）"""
    env = os.environ.get('PZ_SOLVER_HOME')
    if env:
        return Path(env).expanduser().resolve()
    exe_dir = Path(sys.executable).resolve().parent
    if (exe_dir / 'portable.txt').exists():
        return exe_dir
    base = (os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
            or str(Path.home()))
    return Path(base) / APP


def manifest_of(src):
    """目录树模式的清单（本地直接 python exe启动器.py 时用）"""
    out = {}
    for top in PAYLOAD_TOP:
        p = src / top
        if p.is_dir():
            for f in p.rglob('*'):
                if f.is_file():
                    rel = f.relative_to(src).as_posix()
                    if not is_user_file(rel):
                        out[rel] = f.stat().st_size
    for name in PAYLOAD_FILES:
        p = src / name
        if p.is_file():
            out[name] = p.stat().st_size
    return out


def is_user_file(rel):
    """data/ 下属于玩家自己的东西：永远不写进运行目录"""
    if not rel.startswith('data/'):
        return False
    parts = rel.split('/')
    if len(parts) < 2:
        return False
    name = parts[1]
    return (name in USER_KEEP or name.endswith('.bak')
            or name.startswith('mygame_') or name.startswith('panel'))


def payload():
    """内嵌发布内容 -> (清单, read(rel)->bytes)

    优先读 _payload.zip（打包时放进去的，语义确定）；没有就退回读磁盘目录树
    （这样开发机上直接 `python exe启动器.py` 也能跑）。
    """
    src = frozen_src()
    zpath = src / '_payload.zip'
    if zpath.is_file():
        import zipfile
        zf = zipfile.ZipFile(zpath)

        def read(rel):
            return zf.read(rel)

        man = {}
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename.replace('\\', '/')
            if not is_user_file(rel):
                man[rel] = info.file_size
        return man, read
    man = manifest_of(src)
    return man, lambda rel: (src / rel).read_bytes()


def sync_payload(home, man, read):
    """把内嵌内容同步到运行目录；返回 (是否做了同步, 写入文件数)"""
    home.mkdir(parents=True, exist_ok=True)
    marker = home / MANIFEST
    old = None
    if marker.exists():
        try:
            old = json.loads(marker.read_text(encoding='utf-8'))
        except Exception:                                 # noqa: BLE001
            old = None
    if old == man and (home / 'code' / 'm11_plan.py').exists():
        return False, 0

    n = 0
    for rel in sorted(man):
        d = home / rel
        if is_user_file(rel) and d.exists():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(read(rel))
        n += 1
    marker.write_text(json.dumps(man, ensure_ascii=False, sort_keys=True),
                      encoding='utf-8')
    return True, n


# --------------------------------------------------------------------- 执行
def run_code(home, rel, argv=()):
    """在运行目录里执行 rel（如 code/m11_plan.py），返回进程退出码"""
    path = home / rel
    if not path.exists():
        print('✗ 缺少 %s（运行目录可能不完整）' % path)
        return 1
    os.chdir(home)
    sys.argv = [path.name] + [str(a) for a in argv]
    try:
        runpy.run_path(str(path), run_name='__main__')
        return 0
    except SystemExit as e:
        if e.code is None:
            return 0
        return e.code if isinstance(e.code, int) else 1


def open_path(target):
    try:
        os.startfile(str(target))                         # noqa: S606 (Windows)
    except Exception as e:                                # noqa: BLE001
        print('（自动打开失败：%s —— 手动打开 %s 即可）' % (e, target))


def ask_dir(prompt):
    raw = input(prompt).strip().strip('"').strip("'")
    if not raw:
        return None
    p = Path(raw)
    if not p.is_dir():
        print('✗ 文件夹不存在：%s' % p)
        return None
    return p


def do_solve(home, all_stages):
    d = ask_dir('把截图文件夹拖到本窗口后回车（或直接粘路径）: ')
    if d is None:
        return
    argv = (['--all'] if all_stages else []) + ['--stage-dir', str(d)]
    print('\n== %s求解：%s ==' % ('整局' if all_stages else '单阶段', d))
    if run_code(home, 'code/m11_plan.py', argv) != 0:
        print('\n== 求解失败 —— 请查看上方报错 ==')
        print('最常见原因：截图被弹窗遮挡 / 混入了不同行动的截图 / 据点未解锁（无工具栏）')
        print('            / 战力未读出需人工补录 / 截图带进了我方卡组栏')
        return
    if run_code(home, 'code/m12_panel.py') == 0:
        open_path(home / 'data' / 'panel.html')


def do_panel(home):
    if not (home / 'data' / 'plan.json').exists():
        print('还没有求解结果 —— 请先做「整局求解」或「单阶段求解」。')
        return
    if run_code(home, 'code/m12_panel.py') == 0:
        open_path(home / 'data' / 'panel.html')


def do_input(home):
    print('\n== 编写输入 player_input.json ==')
    print('卡牌默认全部拥有，把「自己没有的卡」取消勾选即可；页面里点「保存」直接写入。')
    print('关闭本窗口（或 Ctrl+C）即结束服务。\n')
    run_code(home, 'code/m13_input.py', ['--serve'])


def do_test(home):
    print('\n-- 求解层回归 --')
    return run_code(home, 'code/test_m5.py')


def do_demo(home):
    print('\n-- 端到端演练：用内置样例跑一遍整套求解（约 2 分钟）--')
    code = run_code(home, 'examples/solve_demo.py')
    print('\n（末行应为「整局汇总：5 阶段 · 13 据点 · 未达标 0 个」）')
    return code


def do_selftest(home):
    print('\n== 自检 ==')
    a = do_test(home)
    print('\n回归：%s' % ('通过' if a == 0 else '失败'))
    b = do_demo(home)
    print('演练：%s' % ('通过' if b == 0 else '失败'))
    return a or b


def do_advanced(home):
    raw = input('输入 m11_plan.py 的完整参数（可直接留空回车取消）:\n> ').strip()
    if not raw:
        return
    import shlex
    try:
        argv = shlex.split(raw, posix=False)
    except ValueError as e:
        print('✗ 参数解析失败：%s' % e)
        return
    run_code(home, 'code/m11_plan.py', argv)


MENU = """
==============================================================
  太平洋战线 · 最优解求解器          （exe 版 %s）
  运行目录：%s
==============================================================
  1  整局求解（多阶段截图，自动分组）
  2  单阶段求解
  3  查看方案面板
  4  编写输入（卡池 / 战损 / 阶段 / 变项库存）
  5  自检（求解层回归 + 端到端演练，约 2 分钟）
  6  打开运行目录
  7  高级：直接输入 m11_plan 参数
  0  退出
"""


def menu(home):
    actions = {'1': lambda: do_solve(home, True),
               '2': lambda: do_solve(home, False),
               '3': lambda: do_panel(home),
               '4': lambda: do_input(home),
               '5': lambda: do_selftest(home),
               '6': lambda: open_path(home),
               '7': lambda: do_advanced(home)}
    while True:
        print(MENU % (VERSION, home))
        try:
            choice = input('请选择 [0-7]: ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice in ('0', 'q', 'Q', ''):
            return 0
        fn = actions.get(choice)
        if fn is None:
            print('请输入 0~7 之间的数字。')
            continue
        try:
            fn()
        except KeyboardInterrupt:
            print('\n（已中断）')
        except Exception as e:                            # noqa: BLE001
            import traceback
            traceback.print_exc()
            print('\n✗ 出错了：%s' % e)
        try:
            input('\n—— 按回车回到菜单 ——')
        except (EOFError, KeyboardInterrupt):
            print()
            return 0


# --------------------------------------------------------------------- 入口
def main(argv):
    setup_console()
    home = app_home()

    if argv and argv[0] == 'home':
        print(home)
        return 0
    if argv and argv[0] == 'version':
        print('%s exe 版 %s' % (APP, VERSION))
        return 0

    man, read = payload()
    done, n = sync_payload(home, man, read)
    if done:
        print('已把运行内容准备到：%s（%d 个文件）' % (home, n))

    if not argv:
        return menu(home)

    cmd, rest = argv[0], argv[1:]
    if cmd.startswith('-'):
        return run_code(home, 'code/m11_plan.py', argv)
    if cmd in ('menu', '/?'):
        return menu(home)
    if cmd == 'plan':
        return run_code(home, 'code/m11_plan.py', rest)
    if cmd == 'panel':
        return run_code(home, 'code/m12_panel.py', rest)
    if cmd == 'input':
        return run_code(home, 'code/m13_input.py', rest or ['--serve'])
    if cmd == 'test':
        return do_test(home)
    if cmd == 'demo':
        return do_demo(home)
    if cmd == 'selftest':
        return do_selftest(home)
    print(__doc__.split('\n\n')[0])
    print('用法：')
    print('  %s.exe               打开菜单（双击即此）' % APP)
    print('  %s.exe plan [参数]    直接转给 m11_plan.py（例：plan --all --stage-dir D:\\截图）' % APP)
    print('  %s.exe panel|input|test|demo|selftest|home|version' % APP)
    return 0


if __name__ == '__main__':
    try:
        code = main(sys.argv[1:])
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)
