# -*- coding: utf-8 -*-
"""太平洋战线求解器 · 图形门户（exe 便携版）

被 `打包exe版.py` 用 PyInstaller 打成 `太平洋战线求解器.exe`（--onefile --windowed）。
收包方双击即出一个窗口，上面是六个入口 —— 不装 Python、不联网、不写注册表。

绿色版（非安装）
────────────────
首次运行会把内嵌的内容（code/ data/ doc/ examples/ 文档）铺到**exe 所在目录**，
之后求解结果、面板 HTML、你写的输入都在那个目录的 data/ 里 —— 整个软件就是
「exe + 它旁边那几个文件夹」，拷走即走。
exe 所在目录不可写时（例如放在 Program Files）自动退回
`%LOCALAPPDATA%\\太平洋战线求解器`；也可用环境变量 `PZ_SOLVER_HOME` 指定别处。

为什么不把 code/ 冻进 exe
────────────────────────
code/ 下的模块统一用 `ROOT = Path(__file__).resolve().parent.parent` 定位 data/。
冻结后 __file__ 指向 PyInstaller 的临时解包目录（_MEIPASS），ROOT 会算到临时目录外面，
data/ 就找不到了。把 code/ 落在磁盘上、用 runpy 按真实路径执行，ROOT 永远正确。

命令行（仅排障/打包验收用，不是给用户的界面）
────────────────────────────────────────────
    太平洋战线求解器.exe                出图形门户（双击即此）
    太平洋战线求解器.exe --selftest     无窗口跑自检，写 自检报告.txt，按结果给退出码
    太平洋战线求解器.exe home           打印运行目录
    太平洋战线求解器.exe version        打印版本
"""
from __future__ import annotations

import json
import os
import queue
import runpy
import sys
import threading
import time
import traceback
from pathlib import Path

APP = '太平洋战线求解器'
VERSION = '2026-10-13'

PAYLOAD_TOP = ('code', 'data', 'doc', 'examples')
PAYLOAD_FILES = ('README.md', '使用说明.md', 'requirements.txt')
MANIFEST = '.payload.json'

# data/ 下属于「玩家本地产物」：升级时只补缺失，绝不覆盖
USER_KEEP = {'player_input.json', 'player_input.json.bak', 'plan.json',
             'panel.html', 'input_editor.html', 'player_state.json'}
REPORT = '自检报告.txt'

# 与 data/panel.html 同一套配色，视觉上是一家人
BG, CARD, LINE = '#12161d', '#1b212b', '#2a3240'
FG, DIM, ACC = '#dde4ee', '#8b96a8', '#4a9eff'
OK, BAD, WARN = '#3fb96a', '#e05656', '#e0a03c'

# --------------------------------------------------------------------- 运行目录
def frozen_src():
    """内嵌发布内容的根（onefile 下就是临时解包目录）"""
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def _writable(d):
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / '.write-probe'
        probe.write_bytes(b'x')
        probe.unlink()
        return True
    except Exception:                                     # noqa: BLE001
        return False


def app_home():
    """运行目录：绿版本优先 —— exe 旁边；不可写才退回 LOCALAPPDATA"""
    env = os.environ.get('PZ_SOLVER_HOME')
    if env:
        return Path(env).expanduser().resolve()
    if not getattr(sys, 'frozen', False):                 # 开发机上直接跑
        return Path(__file__).resolve().parent
    exe_dir = Path(sys.executable).resolve().parent
    if _writable(exe_dir):
        return exe_dir
    base = (os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
            or str(Path.home()))
    return Path(base) / APP


def is_user_file(rel):
    if not rel.startswith('data/'):
        return False
    parts = rel.split('/')
    if len(parts) < 2:
        return False
    name = parts[1]
    return (name in USER_KEEP or name.endswith('.bak')
            or name.startswith('mygame_') or name.startswith('panel'))


def _export_ignored():
    """开发模式读 .gitattributes 的 export-ignore，让「目录模式」与发布包内容一致

    发布包的内容由 `git archive` 按 export-ignore 裁剪；直接读磁盘目录树时若不管它，
    会把示例大图、规则手册配图这些都当成运行内容拷一遍。读不到就返回空清单。
    """
    import fnmatch
    ga = Path(__file__).resolve().parent / '.gitattributes'
    pats = []
    try:
        for line in ga.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or 'export-ignore' not in line:
                continue
            pats.append(line.split()[0])
    except Exception:                                     # noqa: BLE001
        pass

    def ignored(rel):
        for p in pats:
            if p.endswith('/'):
                if rel == p[:-1] or rel.startswith(p):
                    return True
            elif fnmatch.fnmatch(rel, p):
                return True
        return False

    return ignored


def payload():
    """内嵌发布内容 -> (清单, read(rel)->bytes)

    优先读 _payload.zip（打包时放进去的，语义确定）；没有就退回读磁盘目录树
    （开发机上直接 `python exe启动器.py` 也能跑，且按 export-ignore 裁剪）。
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

    ignored = _export_ignored()
    man = {}
    for top in PAYLOAD_TOP:
        p = src / top
        if p.is_dir():
            for f in p.rglob('*'):
                if not f.is_file():
                    continue
                rel = f.relative_to(src).as_posix()
                if is_user_file(rel) or ignored(rel):
                    continue
                man[rel] = f.stat().st_size
    for name in PAYLOAD_FILES:
        if (src / name).is_file():
            man[name] = (src / name).stat().st_size
    return man, lambda rel: (src / rel).read_bytes()


def sync_payload(home, man, read, log=print):
    """把内嵌内容铺到运行目录；返回 (是否做了同步, 写入文件数)"""
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
    log('运行内容已就绪：%s（%d 个文件）' % (home, n))
    return True, n


# --------------------------------------------------------------------- 输出转发
class LogRouter:
    """把子脚本的 print 同时送到门户日志面板与内存缓冲"""

    def __init__(self):
        self.buffer = []
        self.queue = queue.Queue()
        self.lock = threading.Lock()

    def write(self, text):
        if not text:
            return
        with self.lock:
            self.buffer.append(text)
            self.queue.put(text)

    def flush(self):
        pass

    def text(self):
        with self.lock:
            return ''.join(self.buffer)


class _Stream:
    """冒充 sys.stdout / sys.stderr（窗口模式下它们本来是 None）"""

    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, router):
        self._router = router

    def write(self, text):
        self._router.write(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def writable(self):
        return True

    def reconfigure(self, **kw):                          # 子脚本可能会调
        pass


class _Stdin:
    """非交互：isatty 为假 → 脚本里的 input() 走 EOF 分支而不是卡死"""

    encoding = 'utf-8'

    def isatty(self):
        return False

    def readline(self, *a):
        return ''

    def read(self, *a):
        return ''

    def writable(self):
        return False


ROUTER = LogRouter()
_DLL_DIRS = []          # 持有 add_dll_directory 句柄，GC 掉就失效了


def install_streams():
    """窗口模式没有控制台：把标准流换成转发流，脚本的 print 才看得见"""
    stream = _Stream(ROUTER)
    sys.stdout = stream
    sys.stderr = stream
    sys.stdin = _Stdin()
    if hasattr(os, 'add_dll_directory'):
        try:
            mei = getattr(sys, '_MEIPASS', None)
            if mei:
                _DLL_DIRS.append(os.add_dll_directory(mei))
        except Exception:                                 # noqa: BLE001
            pass


def run_script(home, rel, argv=()):
    """在运行目录里执行 code/<rel>，返回退出码（不抛异常）"""
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
    """用系统默认程序打开文件或文件夹"""
    try:
        os.startfile(str(target))                         # noqa: S606
        return True
    except Exception as e:                                # noqa: BLE001
        print('（自动打开失败：%s —— 手动打开 %s 即可）' % (e, target))
        return False


# --------------------------------------------------------------------- 任务
class Tasks:
    """门户上的六个动作（跑在后台线程，输出走 ROUTER）"""

    def __init__(self, home):
        self.home = home
        self.service_running = False

    # --- 求解 ---
    def solve(self, stage_dir, all_stages):
        argv = (['--all'] if all_stages else []) + ['--stage-dir', str(stage_dir)]
        print('\n===== %s求解：%s =====' % ('整局' if all_stages else '单阶段', stage_dir))
        if run_script(self.home, 'code/m11_plan.py', argv) != 0:
            print('\n✗ 求解失败 —— 请查看上面的报错')
            print('最常见原因：截图被弹窗遮挡 / 混入了不同行动的截图')
            print('            据点未解锁（无工具栏）/ 战力未读出需人工补录')
            print('            截图带进了我方卡组栏')
            return 1
        if run_script(self.home, 'code/m12_panel.py') != 0:
            print('✗ 面板生成失败')
            return 1
        self.open_panel()
        return 0

    def open_panel(self):
        panel = self.home / 'data' / 'panel.html'
        if not (self.home / 'data' / 'plan.json').exists():
            print('还没有求解结果 —— 请先做「整局求解」或「单阶段求解」。')
            return 1
        if run_script(self.home, 'code/m12_panel.py') != 0:
            print('✗ 面板生成失败')
            return 1
        print('已生成面板：%s' % panel)
        return 0 if open_path(panel) else 1

    # --- 编写输入（常驻本地服务；daemon 线程，关掉窗口就结束）---
    def start_input_service(self):
        if self.service_running:
            print('输入编辑服务已在运行（关闭本窗口即停止）。')
            return 1
        self.service_running = True
        print('\n===== 编写输入 player_input.json =====')
        print('卡牌默认全部拥有，把「自己没有的卡」取消勾选即可；')
        print('页面里点「保存」直接写入 data/player_input.json（旧文件自动备份 .bak）。')
        print('浏览器会自动打开；关闭本窗口即结束这个服务。\n')
        return run_script(self.home, 'code/m13_input.py', ['--serve'])

    # --- 自检 ---
    def selftest(self):
        print('\n===== 自检 1/2：求解层回归 =====')
        a = run_script(self.home, 'code/test_m5.py')
        print('\n===== 自检 2/2：端到端演练（整套求解，约 2 分钟）=====')
        b = run_script(self.home, 'examples/solve_demo.py')
        ok = (a == 0 and b == 0)
        print('\n===== 自检结果：%s =====' % ('通过' if ok else '失败'))
        print('（回归末行应为「全部通过」；演练末行应为'
              '「整局汇总：5 阶段 · 13 据点 · 未达标 0 个」）')
        self.write_report(ok)
        return 0 if ok else 1

    def write_report(self, ok):
        path = self.home / REPORT
        try:
            path.write_text(
                '%s exe 便携版 %s\n自检时间：%s\n结果：%s\n%s\n'
                % (APP, VERSION, _now(), '通过' if ok else '失败', '=' * 60)
                + ROUTER.text(), encoding='utf-8')
            print('自检报告：%s' % path)
        except Exception as e:                            # noqa: BLE001
            print('（写自检报告失败：%s）' % e)


def _now():
    import datetime
    return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# --------------------------------------------------------------------- 图形门户
class Portal:
    def __init__(self, home):
        import tkinter as tk
        self.tk = tk
        self.home = home
        self.tasks = Tasks(home)
        self.busy = True          # 内容铺好之前不接活
        self.cards = []
        self.events = queue.Queue()   # 后台线程 -> 主线程的控制消息

        root = tk.Tk()
        self.root = root
        root.title('%s（便携版 %s）' % (APP, VERSION))
        root.configure(bg=BG)
        root.minsize(920, 640)
        root.geometry('980x700')

        font = ('Microsoft YaHei UI', 10)
        font_s = ('Microsoft YaHei UI', 9)
        font_t = ('Microsoft YaHei UI', 12, 'bold')
        self.font, self.font_s, self.font_t = font, font_s, font_t

        self._header(font_t, font_s)
        self._cards()
        self._log(font)
        self._status(font_s)

        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.after(60, self._drain)
        root.after(120, self._startup)

    # ------------------------------------------------------------ 布局
    def _header(self, font_t, font_s):
        tk = self.tk
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill='x', padx=18, pady=(16, 10))
        tk.Label(bar, text='太平洋战线 · 最优解求解器', bg=BG, fg=FG,
                 font=font_t).pack(side='left')
        tk.Label(bar, text='  便携版', bg=BG, fg=ACC, font=font_s).pack(side='left')
        tk.Button(bar, text='使用说明', bg=BG, fg=DIM, font=font_s, bd=0,
                  activebackground=BG, activeforeground=FG, cursor='hand2',
                  command=self.act_readme).pack(side='right')
        tk.Button(bar, text='打开运行目录', bg=BG, fg=DIM, font=font_s, bd=0,
                  activebackground=BG, activeforeground=FG, cursor='hand2',
                  command=lambda: open_path(self.home)).pack(side='right', padx=8)
        tk.Label(bar, text='运行目录：%s' % self.home, bg=BG, fg=DIM,
                 font=font_s).pack(side='right', padx=10)

    def _cards(self):
        tk = self.tk
        grid = tk.Frame(self.root, bg=BG)
        grid.pack(fill='x', padx=14, pady=4)
        spec = [
            ('整局求解', '把整局截图文件夹交给它\n按阶段自动分组逐关求解', self.act_solve_all),
            ('单阶段求解', '只求解当前一个阶段\n截图放同一个文件夹', self.act_solve_stage),
            ('查看方案面板', '打开图形面板\n看编队、变项与胜率', self.act_panel),
            ('编写输入', '填你有哪些卡、战损格数\n当前阶段与变项库存', self.act_input),
            ('自检', '求解层回归 + 端到端演练\n出问题先跑这个', self.act_selftest),
            ('打开运行目录', '求解结果与面板都在\n该目录的 data\\ 里', lambda: open_path(self.home)),
        ]
        for i, (title, desc, fn) in enumerate(spec):
            self.cards.append(self._card(grid, i // 3, i % 3, title, desc, fn))

    def _card(self, parent, row, col, title, desc, fn):
        tk = self.tk
        outer = tk.Frame(parent, bg=CARD, highlightbackground=LINE,
                         highlightthickness=1, cursor='hand2')
        outer.grid(row=row, column=col, padx=7, pady=7, sticky='nsew')
        parent.columnconfigure(col, weight=1, uniform='c')
        parent.rowconfigure(row, weight=1)

        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill='both', expand=True, padx=16, pady=13)
        lbl_t = tk.Label(inner, text=title, bg=CARD, fg=FG, anchor='w',
                         font=('Microsoft YaHei UI', 11, 'bold'))
        lbl_t.pack(fill='x')
        lbl_d = tk.Label(inner, text=desc, bg=CARD, fg=DIM, anchor='w',
                         justify='left', font=self.font_s)
        lbl_d.pack(fill='x', pady=(5, 0))

        widgets = [outer, inner, lbl_t, lbl_d]

        def enter(_e=None):
            if self.busy:
                return
            for w in widgets:
                w.configure(bg='#232b38')
            outer.configure(highlightbackground=ACC)

        def leave(_e=None):
            for w in widgets:
                w.configure(bg=CARD)
            outer.configure(highlightbackground=LINE)

        def click(_e=None):
            if not self.busy:
                fn()

        for w in widgets:
            w.bind('<Enter>', enter)
            w.bind('<Leave>', leave)
            w.bind('<Button-1>', click)
        return outer

    def _log(self, font):
        tk = self.tk
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill='both', expand=True, padx=18, pady=(8, 6))
        tk.Label(wrap, text='运行日志', bg=BG, fg=DIM, font=self.font_s,
                 anchor='w').pack(fill='x')
        box = tk.Frame(wrap, bg=LINE, highlightbackground=LINE, highlightthickness=1)
        box.pack(fill='both', expand=True)
        self.text = tk.Text(box, bg='#0d1117', fg=FG, bd=0, wrap='word',
                            font=('Microsoft YaHei UI', 9), insertbackground=FG,
                            padx=10, pady=8, state='disabled')
        sb = tk.Scrollbar(box, command=self.text.yview, bg=CARD,
                          troughcolor=BG, bd=0, activebackground=LINE)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.text.pack(side='left', fill='both', expand=True)
        self.text.tag_configure('err', foreground=BAD)
        self.text.tag_configure('ok', foreground=OK)

    def _status(self, font_s):
        tk = self.tk
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill='x', padx=18, pady=(0, 14))
        self.status = tk.Label(bar, text='正在准备运行内容…', bg=BG, fg=DIM,
                               font=font_s, anchor='w')
        self.status.pack(side='left')
        from tkinter import ttk
        self.bar = ttk.Progressbar(bar, mode='indeterminate', length=180)
        self.bar.pack(side='right')

    # ------------------------------------------------------------ 日志/状态
    def log(self, text):
        self.text.configure(state='normal')
        tag = None
        if text.startswith('✗') or 'Traceback' in text or '失败' in text:
            tag = 'err'
        elif text.startswith('✓') or '通过' in text:
            tag = 'ok'
        self.text.insert('end', text, tag or ())
        self.text.see('end')
        self.text.configure(state='disabled')

    def _drain(self):
        try:
            while True:
                self.log(ROUTER.queue.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                kind, a, b = self.events.get_nowait()
                if kind == 'busy':
                    self.set_busy(a, b)
                elif kind == 'status':
                    self.set_status(a, b)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def set_busy(self, busy, text=None):
        self.busy = busy
        if busy:
            self.bar.start(12)
            self.status.configure(text=text or '正在运行…', fg=WARN)
        else:
            self.bar.stop()
            self.status.configure(text=text or '就绪', fg=DIM)

    def set_status(self, text, color=None):
        """只改状态文字，不进忙碌态（输入编辑服务这类常驻任务用）"""
        self.status.configure(text=text, fg=color or DIM)

    # ------------------------------------------------------------ 启动
    def _startup(self):
        def work():
            try:
                man, read = payload()
                sync_payload(self.home, man, read, log=print)
                if not (self.home / 'data' / 'player_input.json').exists():
                    print('提示：第一次使用建议先点「编写输入」，'
                          '把「自己没有的卡」取消勾选。')
            except Exception:                             # noqa: BLE001
                traceback.print_exc()
            finally:
                self.events.put(('busy', False, '就绪'))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 动作
    def _run(self, status, fn):
        if self.busy:
            return
        self.set_busy(True, status)

        def work():
            code = 1
            try:
                code = fn() or 0
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 0
            except Exception:                             # noqa: BLE001
                traceback.print_exc()
            finally:
                msg = '就绪' if code == 0 else '上次任务失败（见日志）'
                self.events.put(('busy', False, msg))

        threading.Thread(target=work, daemon=True).start()

    def act_solve_all(self):
        self._ask_and_solve(True)

    def act_solve_stage(self):
        self._ask_and_solve(False)

    def _ask_and_solve(self, all_stages):
        from tkinter import filedialog
        d = filedialog.askdirectory(
            title='选择截图文件夹（%s）' % ('整局，含全部阶段' if all_stages else '只含当前阶段'))
        if not d:
            return
        print('已选择截图文件夹：%s' % d)
        self._run('%s求解中…' % ('整局' if all_stages else '单阶段'),
                  lambda: self.tasks.solve(Path(d), all_stages))

    def act_panel(self):
        self._run('生成面板…', self.tasks.open_panel)

    def act_input(self):
        # 输入编辑是常驻本地服务，和门户并存 —— 不要进忙碌态，否则其它入口都被锁住
        self.set_status('输入编辑服务运行中（关窗口即停止）', WARN)
        print('正在启动输入编辑服务…')

        def work():
            try:
                if self.tasks.start_input_service() != 0:
                    self.events.put(('status', '就绪', None))
            except Exception:                             # noqa: BLE001
                traceback.print_exc()
                self.events.put(('status', '就绪', None))

        threading.Thread(target=work, daemon=True).start()

    def act_selftest(self):
        self._run('自检中（约 2 分钟）…', self.tasks.selftest)

    def act_readme(self):
        for name in ('使用说明.md', 'README.md'):
            p = self.home / name
            if p.exists():
                open_path(p)
                return
        print('没找到使用说明（%s）' % self.home)

    def _on_close(self):
        if self.tasks.service_running:
            pass                                          # daemon 线程随进程结束
        self.root.destroy()

    def mainloop(self):
        self.root.mainloop()


# --------------------------------------------------------------------- 入口
def ui_smoke(home, seconds=2.5):
    """把图形门户真建出来、泵一段事件、再关掉

    窗口模式的 exe 没法用「看一眼窗口」来验收；只有真把窗口构造出来并让事件循环
    跑一会儿，才能证明 tcl/tk、字体、卡片布局在他这台机器上是好的。
    """
    ok = True
    try:
        portal = Portal(home)
        end = time.time() + seconds
        while time.time() < end:
            portal.root.update()
            time.sleep(0.02)
        log = portal.text.get('1.0', 'end').strip()
        if portal.busy or not log:
            ok = False
            print('门户自检异常：busy=%s 日志长度=%d' % (portal.busy, len(log)))
        portal.root.destroy()
    except Exception:                                     # noqa: BLE001
        traceback.print_exc()
        ok = False
    print('图形门户：%s' % ('正常' if ok else '异常'))
    return ok


def headless_selftest(home, quiet=False):
    """无窗口自检：门户 + 求解层回归 + 端到端演练，写 自检报告.txt，返回退出码

    打包脚本用它验收 exe（窗口模式的 exe 没有 stdout 可抓，所以看报告 + 退出码）。
    """
    man, read = payload()
    sync_payload(home, man, read, log=(lambda s: None) if quiet else print)
    tasks = Tasks(home)

    print('\n===== 自检 1/3：图形门户 =====')
    ui_ok = ui_smoke(home)
    print('\n===== 自检 2/3：求解层回归 =====')
    a = run_script(home, 'code/test_m5.py')
    print('\n===== 自检 3/3：端到端演练（整套求解，约 2 分钟）=====')
    b = run_script(home, 'examples/solve_demo.py')

    ok = ui_ok and a == 0 and b == 0
    print('\n===== 自检结果：%s =====' % ('通过' if ok else '失败'))
    print('（门户应显示「图形门户：正常」；回归末行应为「全部通过」；'
          '演练末行应为「整局汇总：5 阶段 · 13 据点 · 未达标 0 个」）')
    tasks.write_report(ok)
    if not quiet:
        print(ROUTER.text())
    return 0 if ok else 1


def fatal(message):
    """窗口还没起来就崩了：弹个框，并留一份日志在 exe 旁边"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror(APP, message[:2000])
        r.destroy()
    except Exception:                                     # noqa: BLE001
        pass
    try:
        side = (Path(sys.executable).resolve().parent
                if getattr(sys, 'frozen', False)
                else Path(__file__).resolve().parent)
        (side / '启动错误.log').write_text(message, encoding='utf-8')
    except Exception:                                     # noqa: BLE001
        pass


def main(argv):
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)     # 高 DPI 下不糊
    except Exception:                                     # noqa: BLE001
        pass
    install_streams()

    if argv and argv[0] == 'home':
        print(app_home())
        return 0
    if argv and argv[0] == 'version':
        print('%s exe 便携版 %s' % (APP, VERSION))
        return 0

    home = app_home()
    if argv and argv[0] == '--selftest':
        return headless_selftest(home, quiet='--quiet' in argv)

    Portal(home).mainloop()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception:                                     # noqa: BLE001
        fatal('%s\n\n%s' % (APP, traceback.format_exc()))
        sys.exit(1)
