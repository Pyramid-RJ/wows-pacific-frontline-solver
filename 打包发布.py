"""打包发布版：git archive 生成 zip（只含跟踪文件 = .gitignore 策划过的内容）

运行：python 打包发布.py
产物：dist/太平洋战线求解器_YYYYMMDD.zip

发布包含：code/ data/（模板与标注基线,不含玩家本地产物）/ doc/ 一键脚本 使用说明.md
不含：.venv / .work / __pycache__ / 玩家输入与运行产物 / 示例大图（gitignore 排除）

收包方只需：装 Python 3.10+ → 双击「安装环境.bat」→ 拖文件夹到「求解整局.bat」。
"""
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / 'dist'


def main():
    # 干净性检查：未提交的改动不进包
    st = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT,
                        capture_output=True, text=True)
    dirty = [l for l in st.stdout.splitlines()
             if l.strip() and not l.startswith('??')]
    if dirty:
        print('⚠ 工作区有未提交改动（不会进入发布包）:')
        for l in dirty:
            print('  ', l)
        r = input('仍要继续打包? [y/N] ').strip().lower()
        if r != 'y':
            return 1

    DIST.mkdir(exist_ok=True)
    out = DIST / ('太平洋战线求解器_%s.zip' % date.today().isoformat())
    subprocess.run(['git', 'archive', '--format=zip',
                    '--prefix=太平洋战线求解器/', '-o', str(out), 'HEAD'],
                   cwd=ROOT, check=True)

    # 内容校验：关键文件必须在包里
    import zipfile
    with zipfile.ZipFile(out) as z:
        names = z.namelist()

        def has(p):
            return any(n.startswith('太平洋战线求解器/') and
                       n[len('太平洋战线求解器/'):] == p for n in names)

        need = ['使用说明.md', '安装环境.bat', '求解整局.bat',
                '求解单阶段.bat', '查看面板.bat', 'requirements.txt',
                'code/m11_plan.py', 'code/m12_panel.py',
                'data/our_fleet.json', 'data/var_config.json',
                'data/player_input.template.json',
                'data/stage_own/truth.json']
        missing = [p for p in need if not has(p)]
        if missing:
            print('✗ 发布包缺关键文件：%s' % missing)
            return 1
        bad = [n for n in names
               if '.venv/' in n or '__pycache__' in n
               or n.endswith('player_state.json')
               or n.endswith('plan.json')
               or n.endswith('.docx') or '/figures/' in n
               or '/stage_own/s' in n and n.endswith('.png')
               or '/weak_labeled/images/' in n or '/stage_real/' in n]
        if bad:
            print('✗ 发布包混入本地产物：%s' % bad[:5])
            return 1
    print('✓ %s（%d 个文件,%.1f MB）'
          % (out.name, len(names), out.stat().st_size / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main())
