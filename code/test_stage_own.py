"""stage_own 实拍回归：程序识别 vs 人工全字段真值（data/stage_own/truth.json）

运行：python code/test_stage_own.py
期望：7 张截图全部字段一致，末行打印「全部通过」。

真值来源：2026-10-11 人工标注（s2_kp4~s4_kp7 逐字段放大判读；
s1_kp1~3 弱点与 weak_labeled 逐枚核对、工具栏/战力在 4.1b 端到端实测核对）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m11_plan import parse_node

ROOT = Path(__file__).resolve().parent.parent
TRUTH = ROOT / 'data' / 'stage_own' / 'truth.json'
FAIL = []


def ck(cond, msg):
    print('  %s %s' % ('OK  ' if cond else 'FAIL', msg))
    if not cond:
        FAIL.append(msg)


def main():
    truth = json.loads(TRUTH.read_text(encoding='utf-8'))
    images = {k: v for k, v in truth['images'].items()}

    for name in sorted(images):
        lab = images[name]
        print('\n== %s ==' % name)
        n = parse_node(name, str(ROOT / 'data' / 'stage_own' / name))

        # --- 阶段面板 ---
        p = lab['panel']
        ck(n['stage'] == p['stage'] and n['node'] == p['node'],
           '阶段/据点 %s/%s' % (p['stage'], p['node']))
        ck(n['difficulty_label'] == p['difficulty'],
           '难度 %s' % p['difficulty'])
        ck(n['star'] == p['star'], '星级 %s（程序 %s）' % (p['star'], n['star']))

        # --- 战斗条件 ---
        c, lc = lab['condition'], n['cond']
        ck(lc['attr'] == c['attr'] and lc['pct'] == c['pct']
           and lc['scope'] == c['scope']
           and lc['ban_item'] == c['ban_item'],
           '条件 attr/pct/scope/ban %s' %
           ({k: v for k, v in c.items() if v not in (None, False)} or '无'))
        ck((lc['directive'] or None) == (c['directive'] or None),
           '指令 %s' % (c['directive'] or '无'))

        # --- 敌方卡：战力 + 弱点（集合比较） ---
        for i, card in enumerate(lab['cards']):
            got_pw = n['powers'][i]
            got_wk = set(n['weak'][i])
            want_wk = set(card['weak'])
            ck(got_pw == card['power'],
               '卡%d 战力 %s（程序 %s）' % (i + 1, card['power'], got_pw))
            ck(got_wk == want_wk,
               '卡%d 弱点 %s（程序 %s）'
               % (i + 1, sorted(want_wk), sorted(got_wk) or '空'))

        # --- 进度代币 ---
        ck(n['progress_tokens'] == lab['progress_tokens'],
           '进度代币 %s（程序 %s）' % (lab['progress_tokens'],
                                   n['progress_tokens']))

        # --- 变项工具栏 ---
        inv = {int(k): v for k, v in (n['inventory'] or {}).items()}
        want = {int(k): v for k, v in lab['inventory'].items()}
        ck(inv == want, '工具栏库存 %s' % (want or '空'))

    print()
    if FAIL:
        print('FAIL：%d 处不一致' % len(FAIL))
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
