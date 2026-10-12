"""回归测试：全部样本截图 ×全部字段

运行：python code/test_all.py
期望：战力 / 代币 / 档位 / 代币类型 四项全对，且无低置信标记

新增截图时请同步更新本文件与 m4_pipeline.py 中的 TRUTH_* 真值表。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m4_pipeline import parse, DATA, TRUTH_POWER, TRUTH_TOKEN, TRUTH_TIER
from m2_layout import find_card_columns, load_screen

# 代币类型真值：'progress' = 金牌（进度代币），'secondary' = 银牌（次级代币）
TRUTH_COIN = {
    'kp1':  ['secondary'] * 4,
    'kp2':  ['secondary'] * 4,
    'kp3':  ['secondary'] * 4,
    'boss': ['progress'] * 4,
    # 新示例（与 kp1/kp2/kp3 同编成，牌面样式不同）
    'ex_kp1': ['secondary'] * 4,
    'ex_kp2': ['secondary'] * 4,
    'ex_kp3': ['secondary'] * 4,
}

FILES = list(TRUTH_POWER.keys())

# 关键样本回归（本地样本，与 kp*/boss 同属 gitignore，缺失时本节跳过）。
# 每项以**列宽一致**为基线，再按需断言：
#   `power`   整排战力（4.24：卡右侧掉落徽章与卡连成一段 → 该列被撑宽
#             276 vs 209 → ROI 右移、首位数字被挤出窗口 147→47）；
#   `tokens`  指定卡的代币（4.25：进度代币「1100」被 split_wide 从「0」的
#             内孔切成 6 块 → 读成 114313）；
#   `bans`    「区域·禁用的单位类型」图标匹配（4.27：模板只有战列舰/飞机，
#             巡洋舰被硬配成战列舰）；
#   `debuff`  「区域·减益效果 −X%」（4.27：新增条件，折进我方减益强度）。
KEY_SAMPLES = {
    's5_13-57-57': {'power': [137, 147, 155, 120]},
    's5_13-59-31': {'power': [122, 155, 126, 163]},
    # 禁用条目在**带入**段 → 按「只认区域」口径**不生效**（4.28）
    's4_00-18-13': {'tokens': {0: 1100, 1: 4750}, 'applied_bans': []},
    # 禁用条目在**区域**段 → 生效
    'ban_cruiser_plane': {'bans': ['巡洋舰', '飞机'],
                          'applied_bans': ['巡洋舰', '飞机']},
    'debuff50_p2n7': {'debuff': -50},
}


def key_sample_regression():
    from m9_condition import parse as cond_parse
    bad, n = [], 0
    for name, spec in KEY_SAMPLES.items():
        path = '%s/%s.jpg' % (DATA, name)
        if not Path(path).exists():
            print('SKIP 关键样本回归 %s（文件不存在）' % name)
            continue
        n += 1
        ws = [e - s for s, e in find_card_columns(load_screen(path))]
        notes = []
        ok = len(ws) == 4 and max(ws) - min(ws) <= 8
        if not ok:
            notes.append('列宽不齐 %s' % ws)
        need = spec.keys() & {'power', 'tokens'}
        if need:
            res = parse(path)
            if 'power' in spec:
                got = [r['power'] for r in res]
                if got != spec['power']:
                    ok = False
                    notes.append('战力应=%s 实得=%s' % (spec['power'], got))
            for i, v in sorted((spec.get('tokens') or {}).items()):
                got = res[i]['token']
                if got != v:
                    ok = False
                    notes.append('卡%d代币应=%s 实得=%s' % (i + 1, v, got))
        if spec.keys() & {'bans', 'debuff', 'applied_bans'}:
            from m11_plan import cond_ban_types
            co = cond_parse(path)
            if 'bans' in spec and list(co.get('ban_types') or []) != spec['bans']:
                ok = False
                notes.append('禁用舰种应=%s 实得=%s'
                             % (spec['bans'], co.get('ban_types')))
            if 'applied_bans' in spec:
                got = cond_ban_types({'cond': co})
                if got != spec['applied_bans']:
                    ok = False
                    notes.append('生效禁用应=%s 实得=%s（条目分段 %s）'
                                 % (spec['applied_bans'], got,
                                    co.get('ban_scope')))
            if 'debuff' in spec:
                pcts = [pr['pct'] for pr in (co.get('our_pairs') or [])
                        if pr.get('attr') == '减益效果'
                        and pr.get('scope') in (None, '区域')]
                got = sum(pcts)
                if got != spec['debuff']:
                    ok = False
                    notes.append('减益效果应=%s 实得=%s' % (spec['debuff'], got))
        print('  %-18s 列宽=%-22s %s'
              % (name, ws, 'OK' if ok else 'FAIL ' + '；'.join(notes)))
        if not ok:
            bad.append('%s：%s' % (name, '；'.join(notes)))
    if n:
        print('关键样本回归：列宽一致性 + 战力/代币/禁用舰种/减益效果，%d 张截图' % n)
    return bad


def main():
    total = 0
    score = {'power': 0, 'token': 0, 'tier': 0, 'coin': 0}
    low_conf = 0
    fails = []

    for f in FILES:
        path = DATA + '/%s.jpg' % f
        if not Path(path).exists():
            print('SKIP %s（文件不存在）' % f)
            continue
        res = parse(path)
        for i, r in enumerate(res):
            total += 1
            gp = r['power'] == TRUTH_POWER[f][i]
            gt = r['token'] == TRUTH_TOKEN[f][i]
            gz = r['tier'] == TRUTH_TIER[f][i]
            gc = r['token_type'] == TRUTH_COIN[f][i]
            score['power'] += gp
            score['token'] += gt
            score['tier'] += gz
            score['coin'] += gc
            if r['power_low'] or r['token_low']:
                low_conf += 1
            flags = ''
            if not gp:
                flags += ' 战力应=%s' % TRUTH_POWER[f][i]
            if not gt:
                flags += ' 代币应=%s' % TRUTH_TOKEN[f][i]
            if not gz:
                flags += ' 档位应=%s' % TRUTH_TIER[f][i]
            if not gc:
                flags += ' 币种应=%s' % TRUTH_COIN[f][i]
            if flags:
                fails.append('%s 卡%d:%s' % (f, i + 1, flags))
            print('  %-7s 卡%d  战力=%-4s 代币=%-5s 档位=%-5s 币种=%-9s %s'
                  % (f, i + 1, r['power'], r['token'], r['tier'],
                     r['token_type'], 'OK' if not flags else 'FAIL' + flags))

    print()
    print('样本 %d 张卡' % total)
    for k in ('power', 'token', 'tier', 'coin'):
        print('  %-6s %d/%d' % (k, score[k], total))
    print('  低置信标记 %d 处' % low_conf)

    print('\n== 关键样本回归（列宽 / 战力 / 代币 / 禁用舰种 / 减益效果）==')
    fails += key_sample_regression()
    if fails:
        print('\n失败明细：')
        for x in fails:
            print('  ' + x)
        return 1
    print('\n全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())