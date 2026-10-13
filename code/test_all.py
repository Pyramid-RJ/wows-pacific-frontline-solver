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
    # 光板旗（无斜纹）= 驱逐舰（2026-10-13 用户实拍确认）。加模板前它被
    # 巡洋舰模板抢走（实测 0.70 胜出）—— 见下面的 ban_glyph_unit()
    'ban_destroyer': {'bans': ['驱逐舰'], 'applied_bans': ['驱逐舰']},
    # **三行**禁用条目（带入=飞机 / 区域=战列舰+驱逐舰 / 指定=巡洋舰）。
    # 2026-10-13 之前只读第一行（带入），区域段压根没被看过 → 玩家看到
    # 「区域禁了战列舰和驱逐舰，程序照样上驱逐舰」。行归属见 m9._read_ban_rows
    'ban_rows_p3kp7': {'bans': ['战列舰', '驱逐舰'],
                       'applied_bans': ['战列舰', '驱逐舰']},
    'debuff50_p2n7': {'debuff': -50},
}


def ban_glyph_unit():
    """禁用图标识别自检（只用入库的模板 / 旗标，不依赖本地截图）

    锁四件事：① 四类舰种模板齐备；② 按**形体分族**（实心旗 vs 飞机细臂）
    与**斜纹条数**（0/1/2 道）选对类型；③「实心旗」模板不许把飞机的机影
    抢走、双斜纹也不许被读成单斜纹；④ 多行禁用条目只认「区域」行。
    """
    import numpy as np
    from PIL import Image
    from m9_condition import (_glyph_templates, _match_glyph, _solidity,
                              _stripe_class, SOLID_ARM_MIN, THIN_ARM_MAX,
                              ban_effective, ban_scope_label)
    from m11_plan import cond_ban_types
    bad = []
    tpls = _glyph_templates()
    for t in ('战列舰', '巡洋舰', '驱逐舰', '飞机'):
        if t not in tpls:
            bad.append('缺少 %s 的禁用图标模板' % t)
    if bad:
        return bad

    # ② 分族：旗形（含斜纹）实心，飞机机影细臂
    for t in ('战列舰', '巡洋舰', '驱逐舰'):
        s = max(_solidity((np.array(Image.open(p).convert('L')) < 150))
                for p in Path('data/type_glyphs').glob('ban_%s*.png' % t))
        if s < SOLID_ARM_MIN:
            bad.append('%s 模板实心度 %.2f < %.2f（会被误判成细臂族）'
                       % (t, s, SOLID_ARM_MIN))
    for p in Path('data/type_glyphs').glob('ban_飞机*.png'):
        s = _solidity(np.array(Image.open(p).convert('L')) < 150)
        if s > THIN_ARM_MAX:
            bad.append('%s 实心度 %.2f > %.2f（会被误判成实心族）'
                       % (p.name, s, THIN_ARM_MAX))

    # ②b 斜纹条数：驱逐舰 0 道 / 巡洋舰 1 道 / 战列舰 2 道（实心族）
    for t, want in (('驱逐舰', 0), ('巡洋舰', 1), ('战列舰', 2)):
        got = max(_stripe_class((np.array(Image.open(p).convert('L')) < 150))
                  for p in Path('data/type_glyphs').glob('ban_%s.png' % t))
        if got != want:
            bad.append('%s 模板斜纹应 %d 道，实得 %d' % (t, want, got))

    # ③ 匹配：把模板源图本身当 blob（灰度→暗像素=形）
    cases = []
    for p in Path('data/type_glyphs').glob('ban_*.png'):
        cases.append((p, p.stem[4:].split('_')[0]))
    # 旗标是**深底浅形**，反相后才是面板的浅底深形
    flag = np.array(Image.open('data/type_glyphs/驱逐舰.png').convert('L'))
    ys, xs = np.where(flag > 150)
    cases.append(((255 - flag[ys.min():ys.max() + 1,
                             xs.min():xs.max() + 1]).astype('uint8'), '驱逐舰'))
    for src, want in cases:
        blob = ((src if isinstance(src, np.ndarray)
                 else np.array(Image.open(src).convert('L'))) < 150)
        hit = _match_glyph(blob.astype(np.uint8), tpls)
        got = hit[1] if hit else None
        if got != want:
            bad.append('%s 图标应识别为 %s，实得 %s'
                       % (getattr(src, 'name', '驱逐舰旗'), want, got))

    # ④ 多行禁用条目：只有「区域」行拦人（带入/指定只记账不生效）
    rows = [{'scope': '带入', 'types': ['飞机']},
            {'scope': '区域', 'types': ['战列舰', '驱逐舰']},
            {'scope': '指定', 'types': ['巡洋舰']}]
    if ban_effective(rows) != ['战列舰', '驱逐舰']:
        bad.append('多行禁用：生效集合应=战列舰+驱逐舰，实得 %s'
                   % ban_effective(rows))
    if ban_scope_label(rows) != '区域':
        bad.append('多行禁用：兼容标签应=区域，实得 %s' % ban_scope_label(rows))
    if cond_ban_types({'cond': {'ban_rows': rows}}) != ['战列舰', '驱逐舰']:
        bad.append('多行禁用：cond_ban_types 应只收区域行')
    # 只有带入行 → 不生效；旧数据（无 ban_rows）退回单值 + 节点级闸门
    if cond_ban_types({'cond': {'ban_rows': [{'scope': '带入',
                                              'types': ['巡洋舰']}]}}):
        bad.append('多行禁用：只有带入行时不该拦人')
    if cond_ban_types({'cond': {'ban_types': ['巡洋舰'],
                                'ban_scope': '带入'}}):
        bad.append('旧数据：带入段的禁用不该生效')
    if cond_ban_types({'cond': {'ban_types': ['巡洋舰'],
                                'ban_scope': '区域'}}) != ['巡洋舰']:
        bad.append('旧数据：区域段的禁用应生效')
    return bad


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
    print('\n== 禁用图标识别自检（模板齐备 / 形体分族 / 匹配选型）==')
    bu = ban_glyph_unit()
    for x in bu:
        print('  FAIL ' + x)
    if not bu:
        print('  OK   模板齐备、分族正确、匹配选型全部正确')
    fails += bu
    if fails:
        print('\n失败明细：')
        for x in fails:
            print('  ' + x)
        return 1
    print('\n全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())