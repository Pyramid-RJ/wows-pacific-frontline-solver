"""M5 回归测试：输入层 / 变项分配 / 战力留存 / 局部搜索

运行：python code/test_m5.py
期望：全部通过（末行打印「全部通过」）

与 test_all.py 的分工：
  test_all.py —— 识别层（战力/代币/档位/币种 28/28）
  test_m5.py  —— 求解层 M5（本文件），不依赖截图，纯合成用例，秒级完成
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from weaktypes import WEAK_ZH, to_en, to_zh, normalize, validate_fleet, check_against_m8
from m7_assign import debuff_hits, win_rate
from m11_plan import (HP_MAX, TARGET_RATE, power_from_stats, build_pool,
                      advance_hp, load_fleet, load_var_config,
                      inventory_by_name, stage_plan, plan_variations,
                      min_rate, local_search, _objective, eval_cards,
                      cond_pct, has_cond, cond_all_pct, cond_ban_types,
                      cond_buff_suppress, enemy_pct_of, cond_enemy_pct,
                      search_node, plan_to_json, plan_all_stages,
                      save_state, load_state, snapshot_of, rollback,
                      list_snapshots, load_player_input, apply_banned_types,
                      normalize_variations, stage_inventory, var_stock_ok,
                      INV_SRC_BAR, INV_SRC_INPUT, INV_SRC_NONE)

ROOT = Path(__file__).resolve().parent.parent
FAIL = []


def ck(cond, msg):
    print('  %s %s' % ('OK  ' if cond else 'FAIL', msg))
    if not cond:
        FAIL.append(msg)


def mkcard(cid, stats, hp=3, debuff=None, buff=None):
    """合成一张我方卡（power 由 stats 推出，与真实卡库同口径）"""
    return {'id': cid, 'name': 'C%d' % cid, 'stats': list(stats),
            'hp': hp, 'base_power': power_from_stats(stats, HP_MAX),
            'power': power_from_stats(stats, hp),
            'debuff': debuff or [], 'buff': buff or []}


def mknode(name, powers, weak=None, progress=False, tokens=0, label=None):
    return {'name': name, 'powers': list(powers),
            'weak': weak or [set() for _ in powers],
            'progress_tokens': tokens, 'secondary_tokens': 0,
            'is_progress': progress, 'difficulty': sum(powers),
            'stage': 1, 'node': 1, 'difficulty_label': label, 'star': None,
            'cond': {'attr': None, 'pct': None, 'scope': None,
                     'ban_item': False, 'directive': None},
            'inventory': {}, 'has_bar': True, 'review': []}


def t_map():
    print('\n== 1. 弱点类型中英映射（4.7） ==')
    ck(len(WEAK_ZH) == 6, '映射表 6 类')
    ck(to_en('攻击机') == 'atkair' and to_zh('rapid') == '速射', '中↔英互转')
    ck(to_en('atkair') == 'atkair', '英文原样通过')
    ck(to_en('不存在') is None, '未知类型返回 None')
    ck(normalize(['速射', 'rapid', '烟幕']) == {'rapid', 'smoke'}, '混用归一化去重')
    ck(check_against_m8(), '与 m8_weak.WEAK_TYPES 完全一致')
    ck(validate_fleet(load_fleet()) == [], 'our_fleet.json 全部可映射')


def t_crossdomain():
    print('\n== 2. 跨域减益匹配（静默错误回归） ==')
    card = mkcard(1, [75, 30, 75], debuff=[{'type': '攻击机', 'dir': 'up'}])
    en = [{'atkair'}, set(), set(), set()]
    ck('atkair' in debuff_hits(card, 0, en), '中文卡 × 英文敌方弱点 → 命中')
    ck('atkair' in debuff_hits(card, 0, [{'攻击机'}, set(), set(), set()]),
       '中文卡 × 中文敌方弱点 → 同样命中')
    ck(debuff_hits(card, 0, [set(), set(), set(), set()]) == {},
       '弱点不匹配 → 无命中')


def t_power():
    print('\n== 3. 战力模型（stats → 区间 / 生命值 / 变项） ==')
    ck(power_from_stats([80, 80, 40]) == [190, 210], '密苏里 80+80+40 → 190-210')
    ck(power_from_stats([55, 60, 65]) == [170, 190], '拉菲 55+60+65 → 170-190')
    ck(power_from_stats([50, 50, 50], 2) == [125, 145], '2 格 = 90% 参数（150→135）')
    ck(power_from_stats([50, 50, 50], 1) == [110, 130], '1 格 = 80% 参数（150→120）')
    ck(power_from_stats([50, 50, 50], 3, attr_idx=0, attr_pct=0.20) == [150, 170],
       '炮术训练（火力 +20%）→ 区间 +10')
    ck(power_from_stats([50, 50, 50], 3, all_pct=0.40) == [200, 220],
       '心无旁骛（全参数 +40%）')

    print('  — 实测对决（对照手册胜率公式） —')
    ck(round(win_rate(140, 160, 145) * 100) == 76, '区间 140-160 vs 145 → 76%')
    ck(round(win_rate(150, 170, 145) * 100) == 100, '区间 150-170 vs 145 → 100%')


def t_buff():
    print('\n== 3b. 增益 = 提升相邻友军的**一条参数**（用户实拍对照） ==')
    from m7_assign import stats_power as sp
    # 实拍（用户 2026-10-10）：USS圣地亚哥 火力35/生存35/机动70、hp2（90%），
    # 受相邻友军「火力」增益后，游戏内显示 41/32/63 = 136 → 总战力 126–146。
    ck(sp([35, 35, 70], 2) == ([32, 32, 63], [117, 137]),
       '生命值折减作用于**参数**并逐项取整（31.5→32 / 63）')
    ck(sp([35, 35, 70], 2, {'火力': 0.30}) == ([41, 32, 63], [126, 146]),
       '「火力」+30% 只加火力一项 → 41/32/63 = 126-146（与游戏实拍逐项一致）')
    ck(sp([35, 35, 70], 3, {'火力': 0.30}) == ([46, 35, 70], [141, 161]),
       '同一张卡满血时 +30% 火力 → 46/35/70 = 141-161')
    # 旧算法（把区间整体 ×1.3）得出的 151-177 与实拍不符，必须不再出现
    ck([151, 177] not in (sp([35, 35, 70], 2, {'火力': 0.30})[1],
                          sp([35, 35, 70], 2, {'火力': 0.30})[1]),
       '旧「区间整体 ×1.3」的错误结果 151-177 已被彻底否定')

    print('  — 端到端：乔治五世（火力·左）增益其左邻 —')
    f = {c['id']: c for c in load_fleet()}
    cards = [dict(f[20], hp=3), dict(f[15], hp=2),
             dict(f[8], hp=3), dict(f[2], hp=1)]
    node = mknode('n', [154, 150, 114, 133])
    st, _ = eval_cards(cards, node)
    ck(st['buff_active'] == [('HMS英王乔治五世', '火力', 'left', 1)],
       '生效增益 = 乔治五世（方向「左」）→ 位置2')
    ck(st['buff_by_pos'] == {1: {'火力': 1}}, '增益按位置/属性归并')
    ck(st['our_stats'][1] == [41, 32, 63] and st['our_eff'][1] == (126, 146),
       '被增益位置的有效参数与区间正确（126-146）')
    ck(st['our_eff'][0] == (170, 190) and st['our_eff'][3] == (134, 154),
       '无增益位置不受影响（拉菲 170-190 / CORSAIR 134-154）')
    # 拉菲（存活性·左）在位置1 左侧无友军 → 不生效
    ck(all(x[0] != 'USS拉菲' for x in st['buff_active']),
       '位置1 左侧无友军 → 该增益不生效')

    st2, _ = eval_cards(cards, node, teamwork=True)
    ck(st2['our_stats'][1] == sp([35, 35, 70], 2, {'火力': 0.60})[0],
       '「团队合作」→ 同一属性 +60%')

    st3, _ = eval_cards(cards, node, disable_effects=True)
    ck(st3['buff_active'] == [] and st3['our_stats'][1] == [32, 32, 63],
       '「心无旁骛」→ 停用增益（该位置回到 32/32/63）')

    print('  — 第二实拍锚点：机动变项 + 团队合作 → 该位置 90%（用户实际打出） —')
    # 用户在阶段3 用「方向舵操作训练（机动+20%）」+「团队合作（增益×2）」
    # 保住上面这套阵容，游戏内该位置胜率显示为 **90%**（不是 100%）。
    # 因此计算器（目标 95%）必须**拒绝**它 —— 这正是它被否掉的原因。
    st4, wr4 = eval_cards(cards, node, attr_idx=2, attr_pct=0.20,
                          teamwork=True)
    ck(st4['our_stats'][1] == [50, 32, 76] and st4['our_eff'][1] == (148, 168),
       '机动+20% 且增益×2 → 50/32/76 = 148-168（31.5×1.6=50 / 63×1.2=76）')
    ck(wr4[1] < 0.95, '该方案胜率 %.0f%% < 95%% → 计算器必须拒绝'
       % round(wr4[1] * 100))
    ck(round(wr4[1] * 100) == 90, '胜率与游戏内实拍一致（148-168 vs 150 → 19/21 = 90%）')
    # 单独装其中一个都更差，只有「两个一起」才到 90% —— 仍不够
    _, wr_a = eval_cards(cards, node, attr_idx=2, attr_pct=0.20)
    _, wr_b = eval_cards(cards, node, teamwork=True)
    ck(wr_a[1] < wr4[1] and wr_b[1] < wr4[1],
       '单装任一项都低于「机动+团队合作」（48%% / 29%% < 90%%）')


def t_cond():
    print('\n== 3e. 战斗条件折算进胜率（修 4.12：此前只展示不折算，胜率偏乐观） ==')
    cards = [mkcard(i, [100, 100, 100]) for i in range(1, 5)]
    plain = mknode('plain', [280, 280, 280, 280])
    cond_node = mknode('cond', [280, 280, 280, 280])
    cond_node['cond'] = {'attr': '存活性', 'pct': -24, 'scope': '区域',
                         'ban_item': False, 'directive': None}

    ck(cond_pct(cond_node) == {'存活性': -0.24}, 'cond_pct：−24%% → −0.24')
    ck(cond_pct(plain) == {}, '无条件面板 → 不折算')
    bad = mknode('bad', [150] * 4)
    bad['cond'] = dict(plain['cond'], attr='被侦察性', pct=-10)
    ck(cond_pct(bad) == {}, '「被侦察性」不在三项参数内 → 不折算（输出会要求人工确认）')

    # 多行削弱：our_pairs 只由 1x OCR 行配对生成，而单值 attr/pct 取自
    # 1x + 2x 重读的合并文本 —— 某条区域属性行 1x 漏读、被 2x 重读救回时
    # pairs 里没有它，必须并入单值，否则静默漏折算（胜率偏乐观）。
    rescue = mknode('rescue', [280] * 4)
    rescue['cond'] = {'our_pairs': [{'attr': '战力', 'pct': 16, 'scope': '区域'}],
                      'attr': '火力', 'pct': -24, 'scope': '区域'}
    ck(cond_pct(rescue) == {'火力': -0.24},
       '2x 救回：pairs 未覆盖火力 → 并入单值 −0.24（修复前为 {}）')

    # 同一条被 pairs 覆盖则不并入（防重复计数）
    dup = mknode('dup', [280] * 4)
    dup['cond'] = {'our_pairs': [{'attr': '火力', 'pct': -12, 'scope': '区域'}],
                   'attr': '火力', 'pct': -12, 'scope': '区域'}
    ck(cond_pct(dup) == {'火力': -0.12},
       'pairs 已覆盖火力 → 单值不并入（−0.12 而非 −0.24）')

    # 节点级闸门（带入/指定/全场）对并入的单值同样生效
    gate = mknode('gate', [280] * 4)
    gate['cond'] = {'our_pairs': [{'attr': '机动性', 'pct': -12,
                                   'scope': '区域'}],
                    'attr': '火力', 'pct': -24, 'scope': '带入'}
    ck(cond_pct(gate) == {'机动性': -0.12},
       '带入节点的单值火力不并入（只留 pairs 的区域机动性）')

    # 4.21 #2：区域「增益效果 −100%」+ 全域「火力/机动性 −12%」并存 ——
    # 兼容单值由「同一行配对」取（增益效果），不再跨节错配成「火力 −100%」。
    # 「全域」不折算（4.21 #1）；增益效果按 pairs 折算；幽灵火力消失。
    mixed = mknode('mixed', [170] * 4)
    mixed['cond'] = {
        'our_pairs': [{'attr': '增益效果', 'pct': -100, 'scope': '区域'},
                      {'attr': '机动性', 'pct': -12, 'scope': '全域'}],
        'attr': '增益效果', 'pct': -100, 'scope': '区域', 'ban_item': False}
    ck(cond_pct(mixed) == {}, '全域段机动性 −12% 不折算（只算区域）')
    ck(cond_buff_suppress(mixed) == -1.0, '区域「增益效果 −100%」按 pairs 折算')
    ck(cond_all_pct(mixed) == 0.0, '增益效果不走「战力」全参数通道')
    ck('火力' not in cond_pct(mixed), '未折出幽灵「火力 −100%」')

    # 增益效果不在 pairs 首位（兼容单值仍是别的属性）也能命中
    mixed2 = mknode('mixed2', [170] * 4)
    mixed2['cond'] = {
        'our_pairs': [{'attr': '火力', 'pct': -12, 'scope': '区域'},
                      {'attr': '增益效果', 'pct': -100, 'scope': '区域'}],
        'attr': '火力', 'pct': -12, 'scope': '区域', 'ban_item': False}
    ck(cond_buff_suppress(mixed2) == -1.0, '增益效果非首位 → 仍按 pairs 折算')
    ck(cond_pct(mixed2) == {'火力': -0.12}, '同节点区域火力 −12% 正常折算')

    # 既有口径不破：无条件面板 / 非三参数属性（「被侦察性」）
    ck(cond_pct(plain) == {} and cond_pct(bad) == {},
       '既有口径不破：无条件面板 / 「被侦察性」单值 → {}')

    st0, wr0 = eval_cards(cards, plain)
    ck(st0['our_stats'][0] == [100, 100, 100]
       and st0['our_eff'][0] == (290, 310), '无条件：100/100/100 → 290-310')
    st, wr = eval_cards(cards, cond_node)
    ck(st['our_stats'][0] == [100, 76, 100] and st['our_eff'][0] == (266, 286),
       '存活性 −24% → 100/76/100 = 266-286（条件作用在**参数**上）')
    ck(min_rate(wr) < min_rate(wr0), '条件使胜率下降（不再偏乐观）')

    # 与属性变项**加算**（与 stats_power 的加成约定一致）
    stv, _ = eval_cards(cards, cond_node, attr_idx=1, attr_pct=0.20)
    ck(stv['our_stats'][0] == [100, 96, 100],
       '条件 −24% + 伤害控制训练 +20% → 加算 −4% → 96')

    # 「势不可挡」= 无视战斗条件
    sti, _ = eval_cards(cards, cond_node, ignore_conditions=True)
    ck(sti['our_eff'][0] == (290, 310), '「势不可挡」→ 无视条件，回到 290-310')

    # 搜索阶段就折算（否则选阵按无条件评估，推荐系统性偏乐观）
    from m7_assign import search_assignment
    enemy = {'powers': [280, 280, 280, 280], 'weak': [set()] * 4}
    r1 = search_assignment([dict(c) for c in cards], enemy, rate_levels=(1.0,))
    r2 = search_assignment([dict(c) for c in cards], enemy, rate_levels=(1.0,),
                           attr_pct={'存活性': -0.24})
    ck(r1.get(1.0) is not None and
       r1[1.0]['stats']['our_eff'][0] == (290, 310), '无条件搜索：290-310 全胜')
    ck(r2.get(1.0) is None,
       '有条件搜索：266-286 vs 280 → 100% 不可行（搜索正确感知条件）')
    ck(r2.get(0.0) is not None and
       r2[0.0]['stats']['our_stats'][0] == [100, 76, 100],
       '退化档的最优可行解也按 76 点存活性评估')

    # 变项分配层：「势不可挡」作为最后手段，装上后无视条件、不消耗次数上限
    p = {'cards': cards, 'nodes': cond_node, 'std': [], 'spc': [],
         'win_rates': wr, 'stats': st}
    inv, left, notes = plan_variations([p], {'势不可挡': 1}, uses_max=0,
                                       target=1.0)
    ck(p['spc'] == ['势不可挡'], '分配了「势不可挡」（最后手段）')
    ck(min_rate(p['win_rates']) >= 1.0 - 1e-9 and p['meets_target'],
       '装上后无视条件 → 胜率 100%，达标')
    ck(left == 0 and p['std'] == [],
       '特殊变项不消耗次级代币次数（uses_max=0 也能装）')


def t_default_target():
    print('\n== 3c. 默认目标胜率 = 100%（用户确认 2026-10-11） ==')
    ck(abs(TARGET_RATE - 1.0) < 1e-9,
       'TARGET_RATE = %.2f（默认 100%%）' % TARGET_RATE)
    ck(win_rate(150, 170, 150) >= TARGET_RATE - 1e-9,
       '100% 的方案被接受')
    ck(win_rate(148, 168, 150) < TARGET_RATE - 1e-9,
       '90% 的方案被拒绝（用户实拍：148-168 vs 150 = 19/21）')


def t_snapshots():
    print('\n== 3d. 每阶段快照 + 回退 ==')
    import tempfile
    fleet = load_fleet()
    tmp = Path(tempfile.mkdtemp()) / 'state.json'

    # 阶段1：满血出发，用了 id 1..12
    used1 = list(range(1, 13))
    hp1 = save_state(str(tmp), fleet, {}, used1, 1,
                     snapshots={'1': {'hp': {str(c['id']): HP_MAX
                                             for c in fleet},
                                      'used': used1, 'met': True,
                                      'nodes': ['a', 'b'], 'target': 1.0}})
    ck(sum(1 for v in hp1.values() if v < HP_MAX) == 12,
       '阶段1 结束后 12 张带伤')
    st1 = load_state(str(tmp))

    # 阶段2：接力 12 张带伤，再用 4 张
    damaged2 = {int(k): int(v) for k, v in st1['hp'].items()}
    used2 = list(range(13, 17))
    hp2 = save_state(str(tmp), fleet, damaged2, used2, 2,
                     snapshots={'2': {'hp': {str(k): v for k, v in
                                             damaged2.items()},
                                      'used': used2, 'met': True,
                                      'nodes': ['c'], 'target': 1.0}},
                     base_state=st1)
    ck(sum(1 for v in hp2.values() if v < HP_MAX) == 16,
       '阶段2 结束后 16 张带伤（跨阶段累减）')
    st2 = load_state(str(tmp))

    rows = list_snapshots(st2)
    ck([r[0] for r in rows] == [1, 2], '快照按阶段号升序：%s' % [r[0] for r in rows])
    ck(rows[0][1] == 0 and rows[1][1] == 12,
       '快照记的是**该阶段开始时**的带伤数（阶段1=0、阶段2=12）')

    # 回退到阶段2 开始前 = 阶段1 结束后的状态
    rb = rollback(st2, 2)
    ck(rb['stage'] == 1, '回退后「已完成阶段」= 1')
    ck(sum(1 for v in rb['hp'].values() if v < HP_MAX) == 12,
       '回退后 hp 回到阶段2 开始时的 12 张带伤')
    ck(sorted(int(k) for k in rb['snapshots']) == [1],
       '回退后 ≥2 的快照被丢弃')
    ck(snapshot_of(rb, 2) == {}, '被丢弃的快照取不到（回退不可逆，符合预期）')

    # 回退到阶段1 = 全部满血、无快照
    rb1 = rollback(st2, 1)
    ck(rb1['stage'] == 0 and not rb1['snapshots'],
       '回退到阶段1 → 视为「尚未开始」，快照清空')


def t_json_out():
    print('\n== 8b. 结构化输出 plan/v1（M6-1，图形面板的数据源） ==')
    import json
    import tempfile
    cards = [mkcard(i, [100, 100, 100], debuff=[{'type': '速射', 'dir': 'up'}])
             for i in range(1, 5)]
    node = mknode('n', [280, 280, 280, 280],
                  weak=[{'速射'}, set(), set(), set()],
                  progress=True, tokens=1000)
    node['cond'] = {'attr': '存活性', 'pct': -24, 'scope': '区域',
                    'ban_item': False, 'directive': None}
    st, wr = eval_cards(cards, node)
    p = {'cards': cards, 'nodes': node, 'std': ['效率提升'], 'spc': [],
         'win_rates': wr, 'stats': st, 'meets_target': True,
         'efficiency': True, 'teamwork': False}
    inp = {'current': 2, 'total': 3, 'missing': []}
    remaining = [mkcard(9, [80, 80, 40])]
    doc = plan_to_json(inp, [p], remaining, 5, 5, ['注1'])

    # 必须能无损往返 JSON
    doc2 = json.loads(json.dumps(doc, ensure_ascii=False))
    ck(doc2['schema'] == 'plan/v1', 'schema 标识 plan/v1')
    ck(doc['stage'] == {'current': 2, 'total': 3, 'target_rate': 1.0,
                        'n_nodes': 1}, '阶段头（含目标胜率）')

    nd = doc['nodes'][0]
    ck(nd['name'] == 'n' and nd['enemy_powers'] == [280] * 4, '据点头字段')
    ck(nd['condition'] == {'attr': '存活性', 'pct': -24, 'scope': '区域',
                           'directive': None, 'ban_item': False,
                           'ban_types': [], 'disabled_variations': [],
                           'disabled_all': False,
                           'quantified': True, 'pairs': []},
       '条件字段（quantified=True；含新增可选字段 pairs / disabled_all）')
    ck(nd['progress_tokens'] == 1000 and nd['is_progress'], '进度代币字段')

    a = nd['assignment'][0]
    ck(a['position'] == 1 and a['card_id'] == 1 and a['name'] == 'C1',
       '位置/卡 id/卡名')
    ck(a['stats'] == [100, 76, 100] and a['power_range'] == [266, 286],
       '有效参数与区间已含**条件折算**（与 3e 同锚点）')
    ck(a['debuffs'] == {'速射': ['up']}, '减益键为中文弱点类型（与文本一致）')
    ck(abs(a['win_rate'] - wr[0]) < 1e-3 and isinstance(a['win_rate'], float),
       'win_rate 为 4 位小数')

    s = doc['summary']
    ck(s['cards_used'] == [1, 2, 3, 4] and s['cards_used_count'] == 4,
       '参战卡清单')
    ck(s['secondary_uses'] == {'max': 5, 'left': 5, 'used': 0},
       '次级代币次数 max/left/used（该据点只装「效率提升」→ used=0）')
    # 未指定上限 → max/left = None（JSON null）；used 只计非「效率提升」的标准变项
    doc_u = plan_to_json(inp, [p], remaining, None, None, [])
    ck(doc_u['summary']['secondary_uses'] == {'max': None, 'left': None,
                                              'used': 0},
       '未指定上限 → summary.secondary_uses.max/left 为 None（不限）')
    ck(json.loads(json.dumps(doc_u))['summary']['secondary_uses']['max'] is None,
       'JSON round-trip 后 max 仍是 null（不设上限）')
    p2 = dict(p, std=['炮术训练', '效率提升'])
    doc_u2 = plan_to_json(inp, [p2], remaining, None, None, [])
    ck(doc_u2['summary']['secondary_uses']['used'] == 1,
       'used 只计非「效率提升」的标准变项（炮术训练 1 个）')
    ck(s['progress_tokens_total'] == 1000 and s['efficiency_nodes'] == 1,
       '进度代币合计 / 效率提升据点数')
    ck(s['kept_top'][0]['name'] == 'C9', '留存卡清单')
    # 新增可选字段：为达标放弃「效率提升」（最后手段）
    ck(nd.get('efficiency_dropped') is False,
       '新增可选字段 efficiency_dropped（未放弃时为 False）')
    p3 = dict(p, efficiency_dropped=True)
    doc3 = plan_to_json(inp, [p3], remaining, 5, 5, [])
    ck(doc3['nodes'][0]['efficiency_dropped'] is True,
       'efficiency_dropped=True 如实透出（界面可提示收益丢失）')
    # 新增可选字段：condition.disabled_all（「禁用变项」= 全部变项不可用）
    from m11_plan import ALL_VAR_NAMES
    node_all = mknode('allban', [280] * 4)
    node_all['disabled'] = list(ALL_VAR_NAMES)
    st_a, wr_a = eval_cards(cards, node_all)
    pa = {'cards': cards, 'nodes': node_all, 'std': [], 'spc': [],
          'win_rates': wr_a, 'stats': st_a, 'meets_target': True}
    doc_a = plan_to_json(inp, [pa], remaining, 5, 5, [])
    ck(doc_a['nodes'][0]['condition']['disabled_all'] is True
       and len(doc_a['nodes'][0]['condition']['disabled_variations']) == 11,
       'disabled_all=True（11 门全禁）时 disabled_variations 仍逐个列出')
    ck(json.loads(json.dumps(doc_a))['nodes'][0]['condition']
       ['disabled_all'] is True, 'JSON round-trip 后 disabled_all 仍是 true')


def t_html_panel():
    print('\n== 8c. 图形面板 HTML（M6-3，只展示不计算） ==')
    import json as _json
    from m12_panel import build_html
    cards = [mkcard(i, [100, 100, 100]) for i in range(1, 5)]
    node = mknode('n', [280] * 4)
    st, wr = eval_cards(cards, node)
    p = {'cards': cards, 'nodes': node, 'std': [], 'spc': [],
         'win_rates': wr, 'stats': st, 'meets_target': True}
    doc = plan_to_json({'current': 1, 'total': 1, 'missing': []}, [p],
                       [], 0, 0, [])
    html = build_html(doc)
    ck('plan/v1' in html and '编排建议' in html, '内嵌 schema 与标题')
    ck(html.lstrip().startswith('<!DOCTYPE html>'), '自包含 HTML')
    # 数据须以合法 JSON 内嵌（</转义过），且关键内容都在
    ck('<\\/' in html or '</' not in
       html.split('<script>const DATA', 1)[1].split(';</script>')[0],
       '内嵌 JSON 已转义 </（防提前闭合 script）')
    ck('C1' in html and '280' in html, '卡名与敌方战力已内嵌')


def t_input_editor():
    print('\n== 8d. 输入编辑页（M6-4：交互式生成 player_input.json） ==')
    import json as _json
    import tempfile
    import m13_input as ed

    roster = ed.load_roster()
    ck(len(roster) == 30 and roster[0]['id'] == 1, '卡表 30 张、按 id 升序')
    ck({'id', 'name', 'power', 'tier', 'type'} <= set(roster[0]), '卡表字段齐备')

    doc = {'_meta': {'desc': '编辑器生成'},
           'stage': {'current': 2, 'total': 4}, 'secondary_uses_max': None,
           'missing_cards': [30, 24], 'damaged_cards': {'1': 2},
           'banned_types': {'kp3': ['巡洋舰']},
           'disabled_variations': {'kp5': ['炮术训练', '心无旁骛']},
           'variations': None}
    clean = ed.validate_input_doc(doc)
    ck(clean['missing_cards'] == [24, 30], 'missing_cards 排序后写出')
    ck(clean['_meta'] == {'desc': '编辑器生成'} and clean['secondary_uses_max']
       is None, '_meta 原样保留 / 上限不限')
    ck(clean['disabled_variations'] == {'kp5': ['炮术训练', '心无旁骛']},
       'disabled_variations 校验通过并写出')

    d2 = ed.validate_input_doc({'stage': {'current': 1, 'total': 3}})
    ck(d2['missing_cards'] == [] and d2['damaged_cards'] == {}
       and d2['secondary_uses_max'] is None and d2['variations'] is None
       and '_meta' not in d2,
       '可选字段省略 → 默认（全拥有 / 无损耗 / 上限不限 / 无库存 / 无 _meta）')

    for bad, why in (({'stage': {'current': 5, 'total': 4}}, 'current > total'),
                     ({'stage': {'current': 1, 'total': 1},
                       'secondary_uses_max': -1}, '上限为负'),
                     ({'stage': {'current': 1, 'total': 1},
                       'damaged_cards': {'7': 3}}, '满格不该写'),
                     ({'stage': {'current': 1, 'total': 1},
                       'banned_types': {'a': ['潜艇']}}, '舰种超范围'),
                     ({'stage': {'current': 1, 'total': 1},
                       'missing_cards': [7], 'damaged_cards': {'7': 1}},
                      '未拥有又已损耗'),
                     ({'stage': {'current': 1, 'total': 1},
                       'variations': [1, 2, 3]}, 'variations 不是 11 个'),
                     ({'stage': {'current': 1, 'total': 1},
                       'disabled_variations': {'kp': ['不存在的变项']}},
                      '禁用变项里有未知名字')):
        try:
            ed.validate_input_doc(bad)
            ck(False, '应拒绝：%s' % why)
        except ValueError:
            ck(True, '拒绝：%s' % why)

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / 'player_input.json'
        ed.save_input(doc, p)
        ed.save_input(dict(doc, missing_cards=[24]), p)      # 二次保存 → 备份
        bak = Path(d) / 'player_input.json.bak'
        ck(bak.exists() and _json.loads(bak.read_text(encoding='utf-8'))
           ['missing_cards'] == [24, 30], '二次保存把旧文件备份为 .bak')
        inp = load_player_input(str(p))
        ck(inp['current'] == 2 and inp['total'] == 4
           and inp['uses_max'] is None and inp['missing'] == [24]
           and inp['damaged'] == {1: 2},
           '写出的文件被 load_player_input 正确读回（端到端契约）')
        ck(inp['banned'] == {'kp3': ['巡洋舰']} and inp['variations'] is None,
           'banned_types 读回 / variations 为空')
        ck(inp['disabled'] == {'kp5': ['炮术训练', '心无旁骛']},
           'disabled_variations 被 load_player_input 读回')

    html = ed.build_html(roster, ed.load_var_slots(), serve=True)
    ck('全部拥有' in html and 'player_input.json' in html, '页面含核心控件')
    ck(all(c['name'] in html for c in roster), '30 张卡名全部内嵌')
    ck('"serve": true' in html, '--serve 模式标记为可直接保存')
    ck('"serve": false' in ed.build_html(roster, {}, serve=False),
       '静态模式标记为不可保存（只下载）')


def t_all_stages():
    print('\n== 9. 整局模式：按阶段分组 + 跨阶段接力（据点全行动连续编号） ==')
    # 12 张同质弱卡（120 → 110-130）填 2 阶段 × 2 据点 × 4 = 16 个槽位
    # → 阶段2 **必须**复用 4 张带伤卡（2 格 = 90% 参数，对 90 仍 100%）
    fleet = [mkcard(i, [40, 40, 40]) for i in range(1, 13)]
    fleet.append(mkcard(99, [67, 67, 66]))          # 强卡，应全程留存
    nodes = []
    for st, names in ((1, ['n1', 'n2']), (2, ['n3', 'n4'])):
        for i, nm in enumerate(names):
            n = mknode(nm, [90] * 4)
            n['stage'] = st
            n['node'] = i + 1 + (0 if st == 1 else 2)   # 全行动连续编号
            nodes.append(n)
    inp = {'current': 1, 'total': 2, 'missing': []}
    results, docs = plan_all_stages(fleet, inp, {}, nodes, target=1.0,
                                    uses_max=0)

    ck(sorted(results) == [1, 2], '按面板阶段号分成 2 组')
    for st in (1, 2):
        plan = results[st]['plan']
        ids = [c['id'] for p in plan for c in p['cards']]
        ck(len(plan) == 2 and len(ids) == 8 and len(set(ids)) == 8,
           '阶段%d：2 据点 × 4 卡，阶段内无重复（C1 按阶段约束）' % st)
        ck(all(min_rate(p['win_rates']) >= 0.999 for p in plan),
           '阶段%d：全部 100%%' % st)
        ck(99 not in ids, '阶段%d：强卡 99 被留存' % st)

    # 卡池紧张 → 阶段2 必然复用阶段1 用过的卡（12 卡填 16 槽，鸽笼 ≥4）。
    # 实际复用 8 张是留存目标函数的正确行为：优先用「更弱的卡」（带伤 2 格
    # 参数 90%），只要仍 100%，总分更低 → 高战力留给后续。
    ids1 = {c['id'] for p in results[1]['plan'] for c in p['cards']}
    ids2 = {c['id'] for p in results[2]['plan'] for c in p['cards']}
    ck(len(ids1 & ids2) >= 4, '阶段2 复用带伤卡 ≥4 张（12 卡填 16 槽）')
    used1 = {c['id'] for p in results[1]['plan'] for c in p['cards']}
    hp_after_1 = {c['id']: (int(c.get('hp', HP_MAX)) - (1 if c['id'] in used1
                                                       else 0))
                  for c in fleet}
    start2 = results[2]['damaged_start']
    ck(all(start2[k] == v for k, v in hp_after_1.items()),
       '阶段2 起点血量 = 阶段1 结束后（接力 −1 格/次）')
    ck(all(start2[k] == HP_MAX - 1 for k in ids1 & ids2),
       '复用卡在阶段2 开始时为 2 格')

    # docs：每阶段一份 plan/v1，阶段头正确
    ck([d['stage']['current'] for d in docs] == [1, 2],
       'docs = 每阶段一份 plan/v1，阶段号递增')

    # 缺阶段号 → 明确报错
    bad = [dict(nodes[0])]
    bad[0]['stage'] = None
    try:
        plan_all_stages(fleet, inp, {}, bad, target=1.0)
        ck(False, '缺阶段号应报错')
    except ValueError:
        ck(True, '缺阶段号 → ValueError（CLI 会提示改用单阶段模式）')


def t_cond_types():
    print('\n== 9b. 条件五类建模：战力削弱 + 禁用舰种（用户 2026-10-11 口径） ==')
    # 口径前提：敌方侧**属性/舰种**加成已体现在截图战力数字里，程序不重复计算；
    # 但「敌方战力 ±X%」走独立通道（enemy_pct_of，见 t_enemy_power），
    # 因为它只进了**面板显示值**、战斗用基础值 —— 2026-10-13 实拍对照修正。
    ck(validate_fleet(load_fleet()) == [], '舰种数据注入后卡库仍可校验')
    types = {c['type'] for c in load_fleet()}
    ck(types == {'驱逐舰', '巡洋舰', '战列舰', '飞机'}, '30 张卡覆盖 4 舰种')

    cards = [mkcard(i, [100, 100, 100]) for i in range(1, 5)]
    n = mknode('n', [280] * 4)
    n['cond'] = {'attr': '战力', 'pct': -30, 'scope': '区域',
                 'ban_item': False, 'directive': None, 'ban_types': []}
    ck(cond_all_pct(n) == -0.30, '「战力 −30%」→ 全参数 −0.30')
    ck(cond_pct(n) == {}, '战力削弱不进单参数通道')
    st, _ = eval_cards(cards, n)
    ck(st['our_stats'][0] == [70, 70, 70]
       and st['our_eff'][0] == (200, 220),
       '战力 −30% 作用于逐项参数 → 210→147? 应为 70/70/70=210→200-220')

    # 禁用舰种：搜索阶段即剔除（敌方 170 —— 太弱的敌方会被「留存剪枝」
    # 把 200+ 的卡全排除，那是设计行为不是禁用的效果）
    n2 = mknode('n2', [170] * 4)
    n2['cond'] = {'attr': None, 'pct': None, 'scope': '区域',
                  'ban_item': False, 'directive': None,
                  'ban_types': ['驱逐舰']}
    pool = []
    for i, c in enumerate(load_fleet()):
        c2 = dict(c)
        c2['power'] = [90, 110] if c2['type'] == '驱逐舰' else [200, 220]
        pool.append(c2)
    r = search_node(pool, n2, target=1.0)
    ids = {c['id'] for p in ([r] if r else []) for c in p['cards']}
    ck(r is not None and ids,
       '驱逐舰被禁 → 仍能用其余舰种编队')
    if r is not None:
        types_in = {c['type'] for c in r['cards']}
        ck('驱逐舰' not in types_in, '编队中无被禁舰种')
    # 被禁后不足 4 张 → None（stage_plan 记 skipped）
    tiny = [dict(c) for c in pool if c['type'] == '驱逐舰'][:3]
    ck(search_node(tiny, n2, target=1.0) is None,
       '剔除后不足 4 张 → 无法编队（skipped）')
    ck(cond_ban_types(n2) == ['驱逐舰'], 'cond_ban_types 读取')

    # 局部搜索禁换守卫由 t_all_stages 的交换路径覆盖；这里验证战力削弱计入 has_cond
    ck(has_cond(n), '战力削弱计入 has_cond')

    # 「增益效果 −100%」（阶段2据点5 实测）：我方相邻增益完全失效
    cards_b = [mkcard(1, [50, 50, 50], buff=[{'type': '火力', 'dir': 'right'}]),
               mkcard(2, [50, 50, 50]), mkcard(3, [50, 50, 50]),
               mkcard(4, [50, 50, 50])]
    nb = mknode('nb', [140] * 4)
    nb['cond'] = {'attr': None, 'pct': None, 'scope': '区域',
                  'ban_item': False, 'directive': None, 'ban_types': []}
    st_b0, _ = eval_cards(cards_b, nb)
    nb['cond']['attr'] = '增益效果'
    nb['cond']['pct'] = -100
    st_b1, _ = eval_cards(cards_b, nb)
    ck(st_b0['our_stats'][1] == [65, 50, 50]
       and st_b1['our_stats'][1] == [50, 50, 50]
       and has_cond(nb),
       '「增益效果 −100%」→ 相邻增益完全失效且计入')

    # 作用域门控（用户 2026-10-11）：全胜口径下带入/指定/全场/全域条件不会
    # 积累到头上，一律忽略；只有「区域」参与计算；scope 缺失按保守处理
    for scope, want in (('带入', False), ('指定', False), ('全场', False),
                        ('全域', False), ('区域', True), (None, True)):
        ns = mknode('ns', [170] * 4)
        ns['cond'] = {'attr': '战力', 'pct': -50, 'scope': scope,
                      'ban_item': False, 'directive': None,
                      'ban_types': ['驱逐舰']}
        ck(cond_all_pct(ns) == (-0.50 if want else 0.0)
           and cond_ban_types(ns) == (['驱逐舰'] if want else []),
           'scope=%s → %s' % (scope, '生效' if want else '忽略'))


def t_pool():
    print('\n== 4. 输入层：卡池 = 全部 − 缺失；生命值 ==')
    fleet = load_fleet()
    pool, dead = build_pool(fleet, missing=[17, 28, 30], damaged={20: 2, 9: 1})
    ck(len(pool) == 27, '30 − 3 缺失 = 27 张')
    ck(all(c['id'] not in (17, 28, 30) for c in pool), '缺失卡已排除')
    ck([c for c in pool if c['id'] == 20][0]['power'] == [153, 173],
       '拉菲 2 格 → 153-173（55×0.9→50、60×0.9=54、65×0.9→59，逐项取整后求和）')
    ck([c for c in pool if c['id'] == 9][0]['power'] == [110, 130],
       '科罗拉多 1 格（150→120）→ 110-130')
    ck(all(c['hp'] <= HP_MAX for c in pool), '生命值不超过上限')

    pool0, dead0 = build_pool(fleet, missing=[], damaged={7: 0})
    ck(7 not in [c['id'] for c in pool0] and dead0 and dead0[0][0] == 7,
       '生命值 0 格 → 剔除出卡池')
    try:
        build_pool(fleet, damaged={1: 9})
        ck(False, '生命值越界应报错')
    except ValueError:
        ck(True, '生命值越界 → 报错')

    print('  — 跨阶段累减 —')
    pool2, _ = build_pool(fleet, missing=[], damaged={})
    nxt = advance_hp(pool2, used_ids={20, 21})
    ck(nxt[20] == 2 and nxt[21] == 2 and nxt[22] == HP_MAX,
       '参战过的卡 −1 格，未参战保持满格')


def t_varconfig():
    print('\n== 5. 变项身份映射（4.5 / 4.6） ==')
    names, status = load_var_config()
    ck(len(names) == 11, '槽位映射 11 项')
    ck(names[0] == '炮术训练' and names[6] == '心无旁骛', '前 6 标准 / 后 5 特殊')
    ck(status == 'CONFIRMED', '槽位顺序已由用户确认（CONFIRMED）')
    inv = inventory_by_name({0: 1, 4: 2, 7: 5, 99: 9}, names)
    ck(inv == {'炮术训练': 1, '效率提升': 2, '特化训练': 5}, '库存按名归并（未知槽丢弃）')


def t_stage_plan():
    print('\n== 6. 阶段规划：C1 单次参战 + 战力留存 ==')
    # 8 张弱卡（区间 110-130，对 100 必胜）+ 1 张超强卡（190-210，应被剪枝保留）
    pool = [mkcard(i, [40, 40, 40]) for i in range(1, 9)]      # 120 → 110-130
    pool.append(mkcard(99, [67, 67, 66]))                      # 200 → 190-210
    n1 = mknode('n1', [100] * 4)
    n2 = mknode('n2', [100] * 4)
    plan, skipped, remaining = stage_plan(pool, [n1, n2], target=0.95,
                                          use_local_search=False)
    ck(len(plan) == 2 and not skipped, '2 个据点均成功编队')
    ids = [c['id'] for p in plan for c in p['cards']]
    ck(len(ids) == 8 and len(set(ids)) == 8, 'C1：8 张卡互不重复')
    ck(99 not in ids, '战力过剩卡被剪枝保留（留存需求）')
    ck(all(min_rate(p['win_rates']) >= 0.999 for p in plan), '两据点均 100%')
    ck(len(remaining) == len(pool) - 8, '剩余卡数 = 卡池 − 已消耗')

    # 卡池不足 4 张 → 明确跳过而不是崩溃
    tiny = [mkcard(i, [40, 40, 40]) for i in (1, 2, 3)]
    plan2, skipped2, _ = stage_plan(tiny, [n1], target=0.95)
    ck(plan2 == [] and skipped2 == ['n1'], '卡池不足 → 跳过并报告')


def t_variations():
    print('\n== 7. 变项分配（规则卡 16：每单位口径） ==')
    pool = [mkcard(i, [50, 50, 50]) for i in range(1, 9)]      # 150 → 140-160
    n = mknode('weak', [145] * 4)                              # 76% 达标不了
    plan, _, _ = stage_plan(pool, [n], target=0.95, use_local_search=False)
    ck(len(plan) == 1 and min_rate(plan[0]['win_rates']) < 0.95, '初始低于目标')

    # 每单位口径：属性变项只加成装备它的那张卡 → 4 张卡各装 1 门才能全队达标
    inv, left, notes = plan_variations(plan, {'炮术训练': 4}, uses_max=4,
                                       target=0.95)
    ck(plan[0]['std'] == ['炮术训练'] * 4, '标准属性变项：4 张卡各装 1 门')
    ck(all(e['std'] == '炮术训练' for e in plan[0]['equip'].values()),
       '每个单位的标准栏位各占 1 个（规则卡 16）')
    ck(abs(min_rate(plan[0]['win_rates']) - 1.0) < 1e-9, '装满后达到 100%')
    ck(left == 0, '标准属性变项消耗 4 次次级代币次数上限（4 → 0）')
    ck(notes and '炮术训练' in notes[0], '给出分配说明')

    # 库存不足（只有 1 门）→ 只能补 1 张卡，min_rate 仍低于目标但优于初始
    plan1, _, _ = stage_plan(pool, [n], target=0.95, use_local_search=False)
    lo0 = min_rate(plan1[0]['win_rates'])
    inv1, left1, _ = plan_variations(plan1, {'炮术训练': 1}, uses_max=1,
                                     target=0.95)
    ck(plan1[0]['std'] == ['炮术训练']
       and sum(e['std'] == '炮术训练' for e in plan1[0]['equip'].values()) == 1,
       '库存 1 门 → 只装 1 个单位')
    # 每单位口径：装备的单位达标（100%），其余不动 —— min_rate 不变
    ck(plan1[0]['win_rates'][0] >= 0.999
       and min_rate(plan1[0]['win_rates']) == lo0,
       '单门只提升装备的单位（min_rate 仍 %.0f%%）' % (lo0 * 100))

    # 次数上限为 0 → 标准属性变项不装（即使胜率不达标），并给出说明
    plan3, _, _ = stage_plan(pool, [n], target=0.95, use_local_search=False)
    _, left0b, notes0b = plan_variations(plan3, {'炮术训练': 1}, uses_max=0,
                                         target=0.95)
    ck(plan3[0]['std'] == [] and left0b == 0
       and any('次数已用尽' in x for x in notes0b),
       '次数上限 0 → 标准属性变项不装并给出说明')

    # 未指定上限（缺省 None）= 不设上限（用户 2026-10-12 口径）：
    # 需要时**照装**标准变项、left 返回 None、不出现「次数已用尽」告警
    plan4, _, _ = stage_plan(pool, [n], target=0.95, use_local_search=False)
    _, left4, notes4 = plan_variations(plan4, {'炮术训练': 4}, target=0.95)
    ck(plan4[0]['std'] == ['炮术训练'] * 4 and left4 is None,
       '未指定次数上限（缺省）→ 不设上限：需要时照装 4 门标准变项、left=None')
    ck(abs(min_rate(plan4[0]['win_rates']) - 1.0) < 1e-9,
       '未指定上限 → 装满后达标 100%（旧口径按 0 一个都装不上）')
    ck(any('余 不限' in x for x in notes4), 'notes 显示「余 不限」')
    ck(not any('次数已用尽' in x for x in notes4),
       '未指定上限 → **不出现**「次数已用尽」告警')

    # format_plan 的「次级代币使用」行：未设上限 → 明确标注
    from m11_plan import format_plan
    txt = format_plan(plan4, pool, [], [n],
                      {'current': 1, 'total': 1, 'missing': []},
                      None, None, notes4, [], 'CONFIRMED', target=0.95)
    ck('次级代币使用：**未设上限**（玩家未指定）→ 已用 4 次' in txt,
       'format_plan 未设上限时打印「未设上限（玩家未指定）→ 已用 N 次」')

    # load_player_input：缺字段 / null = 不设上限；显式数字（含 0）才作为约束
    import json as _json
    import tempfile as _tempfile
    tmpd = Path(_tempfile.mkdtemp())
    for raw, want in (({}, None), ({'secondary_uses_max': None}, None),
                      ({'secondary_uses_max': 0}, 0),
                      ({'secondary_uses_max': 3}, 3),
                      ({'secondary_currency': 2}, 2)):
        f = tmpd / 'in.json'
        f.write_text(_json.dumps(raw), encoding='utf-8')
        ck(load_player_input(f)['uses_max'] == want,
           'load_player_input %s → uses_max=%s'
           % (raw or '（缺字段）', want))

    # 效率提升：掉落进度代币的据点**必须装**，且不占用次数上限
    print('  — 效率提升：进度代币据点必须装，且不占用次级代币次数上限 —')
    pa = mknode('普通', [100] * 4, progress=False)
    pb = mknode('BOSS', [100] * 4, progress=True, tokens=2400)
    pc = mknode('BOSS2', [100] * 4, progress=True, tokens=600)
    pool2 = [mkcard(i, [40, 40, 40]) for i in range(11, 23)]     # 12 张
    plan2, _, _ = stage_plan(pool2, [pa, pb, pc], target=0.95,
                             use_local_search=False)
    ck(len(plan2) == 3, '三据点均编队')

    _, left0, _ = plan_variations(plan2, {'效率提升': 5}, uses_max=0)
    by = {p['nodes']['name']: p for p in plan2}
    ck(by['普通']['std'] == [], '普通据点不装效率提升（收益为 0）')
    ck(by['BOSS']['std'] == ['效率提升']
       and by['BOSS2']['std'] == ['效率提升'],
       '两个进度代币据点都装 ——「效率提升」不占用次数上限（uses_max=0 也照装）')
    ck(left0 == 0, '次数上限完全没有被效率提升消耗')

    _, left5, _ = plan_variations(plan2, {'效率提升': 5}, uses_max=5)
    ck(left5 == 5, 'uses_max=5 → 余量仍是 5（效率提升不占上限）')

    inv4, _, notes4 = plan_variations(
        [p for p in plan2 if p['nodes']['name'] == 'BOSS'],
        {}, uses_max=5)
    by4 = {p['nodes']['name']: p for p in plan2}
    ck(by4['BOSS']['std'] == ['效率提升']
       and inv4.get('效率提升', 0) == 0
       and not any('库存不足' in x for x in notes4),
       '库存为 0 → 效率提升按无限库存照装、不扣成负数、不再告警')


def t_local_search():
    print('\n== 8. 局部搜索 ==')
    pool = [mkcard(i, [40, 40, 40]) for i in range(1, 9)]
    n1 = mknode('n1', [105, 105, 95, 95])
    n2 = mknode('n2', [95, 95, 105, 105])
    plan, _, _ = stage_plan(pool, [n1, n2], target=0.95, use_local_search=False)
    before = _objective(plan, 0.95)
    ids_before = sorted(c['id'] for p in plan for c in p['cards'])
    plan2 = local_search([dict(p) for p in plan], [n1, n2], target=0.95)
    after = _objective(plan2, 0.95)
    ck(after >= before, '局部搜索不劣化总目标（%s → %s）' % (before, after))
    ids_after = sorted(c['id'] for p in plan2 for c in p['cards'])
    ck(ids_before == ids_after, '只交换不新增/删除 → C1 与卡集合不变')
    ck(all(min_rate(p['win_rates']) >= 0.95 - 1e-9 for p in plan2),
       '交换后仍全部达标')


def t_banned_input():
    print('\n== 9c. banned_types 玩家输入覆盖图标识别（合并步骤6） ==')
    ck(apply_banned_types(['战列舰'], 'shotA', {'shotA': ['飞机']})
       == (['飞机'], True), '玩家输入覆盖图标识别结果（来源=True）')
    ck(apply_banned_types(['战列舰'], 'shotA', {'shotA': []}) == ([], True),
       '空列表 = 显式「无禁用」（同样覆盖图标识别，否决误判）')
    ck(apply_banned_types(['战列舰'], 'shotB', {'shotA': ['飞机']})
       == (['战列舰'], False), '未命中截图名 → 用图标识别结果（来源=False）')

    # load_player_input：键=截图名、单字符串归一化成列表、空列表保留
    import json as _json
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / 'input.json'
    tmp.write_text(_json.dumps({'banned_types': {'shotA': '巡洋舰',
                                                 'shotB': []}}),
                   encoding='utf-8')
    inp = load_player_input(tmp)
    ck(inp['banned'] == {'shotA': ['巡洋舰'], 'shotB': []},
       'load_player_input 解析 banned_types（单字符串归一化、空列表保留）')


def t_var_input():
    print('\n== 9d. variations 玩家直给（截图无工具栏时的库存来源，合并步骤7） ==')
    names, _ = load_var_config()
    ck(normalize_variations([None, None, None, None, 5, None, None, 3,
                             None, 7, None], names)
       == {'效率提升': 5, '特化训练': 3, '快速维修': 7},
       '11 元素列表按槽位顺序 → {变项名: 数量}（null 未指定 = 跳过）')
    ck(normalize_variations([0, 0, 0, 0, 5, 0, 0, 3, 0, 7, 0], names)
       == {'炮术训练': 0, '伤害控制训练': 0, '方向舵操作训练': 0,
           '团队合作': 0, '效率提升': 5, '士气打击': 0,
           '心无旁骛': 0, '特化训练': 3, '老水手': 0, '快速维修': 7,
           '势不可挡': 0},
       '显式 0 保留（0 = 本轮不可用；标准变项留空才是无上限）')
    ck(normalize_variations({'效率提升': 2, 6: 1}, names)
       == {'效率提升': 2, '心无旁骛': 1}, '按变项名 / 槽位号键都收')
    ck(normalize_variations(None, names) == {}, 'None → 空库存')

    print('  — 库存口径：标准变项未给值 = 无上限；特殊变项未给值 = 0 —')
    ck(var_stock_ok({}, '炮术训练') and var_stock_ok({}, '团队合作'),
       '标准变项未给值 → 可用（商店可买，视为无上限）')
    ck(not var_stock_ok({'炮术训练': 0}, '炮术训练'),
       '标准变项显式 0 → 本轮不可用')
    ck(var_stock_ok({'炮术训练': 2}, '炮术训练'), '标准变项显式 N → 可用')
    ck(not var_stock_ok({}, '老水手') and var_stock_ok({'老水手': 2}, '老水手'),
       '特殊变项未给值 → 0；显式给了才可用')

    print('  — stage_inventory：截图工具栏优先，全无工具栏才用玩家输入 —')
    var11 = [None, None, None, None, 9, None, None, None, None, None, None]
    inv, src = stage_inventory(
        [{'has_bar': True, 'inventory': {0: 2}},
         {'has_bar': False, 'inventory': {}}],
        {'variations': var11}, names)
    ck(inv == {'炮术训练': 2} and src == INV_SRC_BAR,
       '有工具栏截图 → 截图库存胜出（玩家输入被忽略）')
    inv, src = stage_inventory([{'has_bar': False, 'inventory': {}}],
                               {'variations': var11}, names)
    ck(inv == {'效率提升': 9} and src == INV_SRC_INPUT,
       '全无工具栏 → 用玩家输入 variations')
    inv, src = stage_inventory([{'has_bar': False, 'inventory': {}}],
                               {}, names)
    ck(inv == {} and src == INV_SRC_NONE, '两者皆无 → 空库存')
    inv, src = stage_inventory([{'has_bar': True}, {'has_bar': False}],
                               {}, names)
    ck(inv == {} and src == INV_SRC_BAR,
       '缺 inventory 键的合成节点不崩（.get 防御）')


def t_disabled_var():
    print('\n== 9e. 标准变项无上限 + 区域「禁用变项」（2026-10-12 口径） ==')
    cards = [mkcard(i, [100, 100, 100]) for i in range(1, 5)]
    enemy = 310        # 无变项只有 5%；装上「炮术训练」后 310-330 全压 → 达标

    def solve(inv, disabled=None, progress=False):
        node = mknode('n', [enemy] * 4)
        if disabled is not None:
            node['disabled'] = disabled
        if progress:
            node['is_progress'] = True
            node['progress_tokens'] = 500
        st, wr = eval_cards(cards, node)
        p = {'cards': cards, 'nodes': node, 'std': [], 'spc': [],
             'win_rates': wr, 'stats': st}
        _, _, notes = plan_variations([p], inv, uses_max=None, target=1.0)
        return p, notes

    p, _ = solve({})
    ck(p['meets_target'] and p['std'].count('炮术训练') == 4,
       '标准变项未给值 → 视为无上限，装上后达标（%s）' % p['std'])
    p, _ = solve({'炮术训练': 0, '伤害控制训练': 0, '方向舵操作训练': 0})
    ck(not p['std'] and not p['meets_target'],
       '标准变项显式 0 → 本轮不可用（不装、不达标）')
    p, _ = solve({}, ['炮术训练', '伤害控制训练', '方向舵操作训练'])
    ck(not p['std'] and not p['meets_target'],
       '该据点「禁用变项」列了这三门 → 一门都不装（禁用生效）')
    p, notes = solve({}, ['效率提升'], progress=True)
    ck('效率提升' not in p['std'] and p['std'].count('炮术训练') == 4,
       '禁用「效率提升」→ 不装它、改装别的（%s）' % p['std'])
    ck(any('禁用变项' in x and '效率提升' in x for x in notes),
       '禁用效率提升 → 显式告警（进度代币收益受影响）')


def t_final_stage_reserve():
    print('\n== 9f. 末阶段预留：非最终阶段的据点先试「无 3 级舰」池（A/B） ==')
    # 8 张 2 级 [50,50,50]=150 → 140-160：对 141 → 20/21 = 95.2%（≥0.95 达标）
    # 4 张 3 级 [60,60,60]=180 → 170-190：对 141 → 100%
    blues = [mkcard(i, [50, 50, 50]) for i in range(1, 9)]
    for c in blues:
        c['tier'] = 'blue'
    golds = [mkcard(i, [60, 60, 60]) for i in range(11, 15)]
    for c in golds:
        c['tier'] = 'gold'
    pool = blues + golds
    n_hard = mknode('n_hard', [141] * 4, label='困难')

    # 非最终阶段（后面还有阶段）→ 先试无 3 级池：0.952 ≥ 0.95 → 用它、3 级全留下
    plan, _, _ = stage_plan(pool, [n_hard], target=0.95,
                            use_local_search=False, final_stage=False)
    ck(len(plan) == 1 and all(c['tier'] == 'blue' for c in plan[0]['cards']),
       '非最终阶段的困难据点：不动用 3 级舰（末阶段预留）')
    ck(plan[0]['gold_saved'] and plan[0]['gold_save_reason'] == 'final_stage',
       'gold_saved=True 且 gold_save_reason=final_stage')

    # 最终阶段（老口径）：困难据点按难度治理不生效 → 100% 的 3 级编队胜出
    plan2, _, _ = stage_plan(pool, [n_hard], target=0.95,
                             use_local_search=False, final_stage=True)
    ck(any(c['tier'] == 'gold' for c in plan2[0]['cards'])
       and not plan2[0].get('gold_saved'),
       '最终阶段的困难据点：可用 3 级舰（100% 编队胜出）')

    # 简单/普通据点即使在最终阶段也保留 3 级（老口径不破）
    n_easy = mknode('n_easy', [141] * 4, label='简单')
    plan3, _, _ = stage_plan(pool, [n_easy], target=0.95,
                             use_local_search=False, final_stage=True)
    ck(plan3[0]['gold_saved']
       and plan3[0]['gold_save_reason'] == 'difficulty',
       '最终阶段的简单据点仍做等级治理（reason=difficulty）')

    # 受限池达不到目标 → 退回全池动用 3 级（3 级是最后手段，不牺牲达标）
    n_very = mknode('n_very', [165] * 4, label='简单')
    plan4, _, _ = stage_plan(pool, [n_very], target=0.95,
                             use_local_search=False, final_stage=True)
    ck(plan4 and plan4[0]['meets_target']
       and any(c['tier'] == 'gold' for c in plan4[0]['cards']),
       '受限池达不到目标 → 退回全池（动用 3 级、仍达标）')

    print('  — 整局模式：阶段总数取面板 OCR 最大阶段号（B） —')
    fleet2 = [mkcard(i, [50, 50, 50]) for i in range(1, 9)]
    for c in fleet2:
        c['tier'] = 'blue'
    fleet2 += [mkcard(i, [60, 60, 60]) for i in range(11, 15)]
    for c in fleet2[8:]:
        c['tier'] = 'gold'
    nodes2 = []
    for st in (1, 2, 3):
        n = mknode('s%d_hard' % st, [141] * 4, label='困难')
        n['stage'] = st
        n['node'] = st
        nodes2.append(n)
    inp2 = {'current': 1, 'total': 2, 'missing': []}      # 玩家输入写 2（错）
    res, docs = plan_all_stages(fleet2, inp2, {}, nodes2, target=0.95,
                                uses_max=0)
    ck([d['stage']['total'] for d in docs] == [3, 3, 3],
       '阶段总数取 OCR 最大阶段号（输入 total=2 → 报告 3）')
    ck(all(p['gold_saved'] for p in res[1]['plan'] + res[2]['plan']),
       '阶段1/2（非最终）的困难据点不动用 3 级')
    ck(all(not p.get('gold_saved') for p in res[3]['plan'])
       and all(any(c['tier'] == 'gold' for c in p['cards'])
               for p in res[3]['plan']),
       '阶段3（OCR 判定的最终阶段）可用 3 级舰')


def t_std_variation():
    print('\n== 9g. 选阵计入标准属性变项：只靠变项才达标的阵容能被选中（C） ==')
    from m7_assign import search_assignment, std_variation_win_rates
    # [70,70,10]=150 → 140-160；火力 +20% → 84+70+10 = 164 → 154-174
    cards = [mkcard(i, [70, 70, 10]) for i in range(1, 5)]
    enemy = {'powers': [154] * 4, 'weak': [set()] * 4}
    r0 = search_assignment([dict(c) for c in cards], enemy, rate_levels=(1.0,))
    ck(r0.get(1.0) is None, '无变项模型：140-160 vs 154 → 33%，100% 不可行')
    r1 = search_assignment([dict(c) for c in cards], enemy, rate_levels=(1.0,),
                           std_variation=True)
    ck(r1.get(1.0) is not None and r1[1.0]['win_rates'][0] >= 0.999,
       '计入「每单位一门标准变项」→ 154-174 vs 154 → 100% 可行')
    ck(r1[1.0]['stats']['our_eff'][0] == (140, 160),
       'stats 仍是**无变项**的真实区间（140-160），变项只是选阵估计')
    r2 = search_assignment([dict(c) for c in cards], enemy, rate_levels=(1.0,),
                           std_variation=True, std_attrs=[0])
    ck(r2.get(1.0) is not None, 'std_attrs=[0]（仅火力门）→ 仍 100% 可行')

    # +20% 的取整与 stats_power 一致（逐项四舍五入）
    st2 = {'our_eff': {0: (113, 133)}, 'enemy_eff': {0: 148},
           'our_stats': {0: [73, 30, 20]}}       # 73×1.2 = 87.6 → 88
    ck(abs(std_variation_win_rates(st2, [0])[0]
           - win_rate(128, 148, 148)) < 1e-9,
       '+20% 取整口径 = stats_power（73×1.2=87.6→88 → 128-148）')
    ck(std_variation_win_rates(st2, [])[0]
       == win_rate(113, 133, 148),
       'std_attrs=[] → 一门都不建模（原样 113-133 vs 148）')
    ck(std_variation_win_rates(st2, None)[0]
       == max(std_variation_win_rates(st2, [k])[0] for k in (0, 1, 2)),
       'std_attrs=None = 逐位置取三门中的最优')

    print('  — m11 侧：库存 / 禁用变项决定是否建模 —')
    node = mknode('n', [154] * 4)
    pool = [dict(c) for c in cards]
    s0 = search_node(pool, node, target=1.0)
    ck(s0 is not None and not s0['meets_target']
       and s0['std_var_attrs'] is None,
       '未传库存（inv=None）→ 不建模（meets_target=False，老行为）')
    s1 = search_node(pool, node, target=1.0, inv={})
    ck(s1 is not None and s1['meets_target']
       and s1['std_var_attrs'] == [0, 1, 2],
       '标准变项未给值 = 无上限 → 建模三门、meets_target=True')
    zero = {'炮术训练': 0, '伤害控制训练': 0, '方向舵操作训练': 0}
    s2 = search_node(pool, node, target=1.0, inv=zero)
    ck(s2 is not None and not s2['meets_target']
       and s2['std_var_attrs'] is None,
       '三门显式 0 → 不建模（与「本轮不可用」一致）')
    dis = mknode('dis', [154] * 4)
    dis['disabled'] = ['炮术训练', '伤害控制训练', '方向舵操作训练']
    s3 = search_node(pool, dis, target=1.0, inv={})
    ck(s3 is not None and s3['std_var_attrs'] is None,
       '该据点「禁用变项」禁了三门 → 不建模')

    print('  — 变项分配阶段从**无变项真实值**起步（不把估计值当结果） —')
    # search_node 返回的 win_rates 是带变项的估计值；plan_variations 必须先重算
    plan, _, _ = stage_plan(pool, [mknode('nv', [154] * 4)], target=1.0,
                            use_local_search=False, inv={})
    ck(len(plan) == 1 and min_rate(plan[0]['win_rates']) >= 0.999,
       '选阵阶段即按「可装变项」选出 100% 阵容')
    from m11_plan import plan_variations
    _, _, _ = plan_variations(plan, {}, uses_max=None, target=1.0)
    ck(plan[0]['std'].count('炮术训练') >= 1
       and min_rate(plan[0]['win_rates']) >= 0.999,
       '收官重算：变项确实装上、逐位置胜率仍 100%（估计值未冒充结果）')


def t_cond_display():
    print('\n== 9h. 条件显示按 our_pairs 分节逐条（与折算口径一致，D） ==')
    from m11_plan import format_plan
    pool = [mkcard(i, [50, 50, 50]) for i in range(1, 5)]     # 150 → 140-160
    node = mknode('nd', [120] * 4)
    # 实测据点13 的形状：单值 attr/scope 是 m9 错配（来自不同分节），
    # 真实条件 = 带入 3 条 + 区域 1 条
    node['cond'] = {'attr': '存活性', 'pct': -12, 'scope': '区域',
                    'ban_item': False, 'directive': None, 'ban_types': [],
                    'our_pairs': [
                        {'attr': '存活性', 'pct': -12, 'scope': '带入'},
                        {'attr': '机动性', 'pct': -12, 'scope': '带入'},
                        {'attr': '增益效果', 'pct': -100, 'scope': '带入'},
                        {'attr': '火力', 'pct': -24, 'scope': '区域'}]}
    plan, _, _ = stage_plan(pool, [node], target=0.95, use_local_search=False)
    txt = format_plan(plan, pool, [], [node],
                      {'current': 1, 'total': 1, 'missing': []},
                      0, 0, [], [], 'CONFIRMED', target=0.95)
    ck('带入：存活性 -12%、机动性 -12%、增益效果 -100% ｜ 区域：火力 -24%'
       in txt, 'pairs 按作用域分节逐条显示')
    ck('区域 · 存活性' not in txt,
       '不再显示错配单值（区域 · 存活性 −12%）')
    ck('（已计入胜率：火力 -24%）' in txt,
       '折算标注来自**实际折算结果**（仅区域火力 −24%）')

    # 全部 pairs 为「带入」→ 折算 {}，标注为忽略（据点13 的真实形状）
    node2 = mknode('nd2', [120] * 4)
    node2['cond'] = {'attr': '存活性', 'pct': -12, 'scope': '区域',
                     'ban_item': False, 'directive': None, 'ban_types': [],
                     'our_pairs': [
                         {'attr': '存活性', 'pct': -12, 'scope': '带入'},
                         {'attr': '机动性', 'pct': -12, 'scope': '带入'},
                         {'attr': '增益效果', 'pct': -100, 'scope': '带入'}]}
    ck(cond_pct(node2) == {}, '折算为 {}（单值被 pairs 覆盖 → 不并入）')
    plan2, _, _ = stage_plan(pool, [node2], target=0.95,
                             use_local_search=False)
    txt2 = format_plan(plan2, pool, [], [node2],
                       {'current': 1, 'total': 1, 'missing': []},
                       0, 0, [], [], 'CONFIRMED', target=0.95)
    ck('带入：存活性 -12%、机动性 -12%、增益效果 -100%' in txt2
       and '（带入 条件按全胜口径忽略，未计入）' in txt2,
       '折算为空时标注「按全胜口径忽略」，与计算一致')


def t_scenario_search():
    print('\n== 9i. 场景式搜索：靠老水手/特化训练才达标的阵容能被选中（诚实性） ==')
    from m7_assign import search_assignment

    # 合成用例：4 张弱卡（[45,45,45]=135 → 125-145）+ 4 张更弱的陪跑卡。
    # 弱卡的减益是「烟幕」而敌方弱点是「速射」—— 保守模型下一击不中：
    #   敌方 [120,200,200,200] → 4 张全 0%（位置1 除外，其敌方本来就弱）。
    # 全员「老水手」后每个敌方吃 2 击 → 200×0.6=120 → 逐位置 100%。
    # 逐步贪心**单个**老水手救不回任何位置（单击 160 仍打不动），
    # 必须靠场景式重试的「救场种子」把 4 个老水手一次装上。
    def mk_rescue_pool():
        weak = [mkcard(i, [45, 45, 45],
                       debuff=[{'type': '烟幕', 'dir': 'up'},
                               {'type': '烟幕', 'dir': 'right'}])
                for i in range(1, 5)]
        decoy = [mkcard(i, [55, 55, 55]) for i in range(11, 15)]
        return weak + decoy

    node = mknode('rescue', [120, 200, 200, 200], weak=[{'速射'}] * 4)
    n_weak = {1, 2, 3, 4}

    # 无库存 → 谈不上救场：如实报告不达标（诚实性）
    s0 = search_node(mk_rescue_pool(), node, target=1.0, inv={})
    ck(s0 is not None and not s0['meets_target'] and 'seed_spc' not in s0,
       '无「老水手」库存 → 不达标、且不携带任何乐观假设（诚实性）')
    p0 = {'cards': list(s0['cards']), 'nodes': node}
    _, _, notes0 = plan_variations([p0], {}, uses_max=None, target=1.0)
    ck(not p0['meets_target'] and any('⚠' in x for x in notes0),
       '报告如实标注未达标（不声称达标）')

    # 库存不足（只有 2 个，需要 4 个）→ 仍然不达标（乐观假设不得冒充结果）
    s1 = search_node(mk_rescue_pool(), node, target=1.0, inv={'老水手': 2})
    ck(s1 is not None and not s1['meets_target'],
       '「老水手」库存仅 2 个 → 救不回来，不达标（库存按 var_stock_ok）')

    # 库存充足 → 场景式重试选出「4 张弱卡」，真实评估器复核 100%
    s2 = search_node(mk_rescue_pool(), node, target=1.0, inv={'老水手': 4})
    ck(s2 is not None and s2['meets_target'],
       '有 4 个「老水手」→ 场景式重试救回、达标')
    ck({c['id'] for c in s2['cards']} == n_weak,
       '选出的是「无变项时打不动、但老水手能救回」的弱卡阵容（%s）'
       % sorted(c['id'] for c in s2['cards']))
    ck(min_rate(s2['win_rates']) >= 1.0 - 1e-9,
       '解里的胜率是**实装变项后的真实值**（100%%），不是乐观估计')
    ck(sorted((s2.get('seed_spc') or {}).values()) == ['老水手'] * 4,
       '解携带救场种子（4 个老水手），供阶段级 plan_variations 复现')
    ck(all(e['spc'] == '老水手' for e in (s2.get('equip') or {}).values()),
       'equip 已按真实分配写入（每个单位特殊栏位 = 老水手）')

    # 该据点「禁用变项」列了老水手 → 不救场，如实不达标
    dis = mknode('rescue-dis', [120, 200, 200, 200], weak=[{'速射'}] * 4)
    dis['disabled'] = ['老水手', '特化训练']
    s3 = search_node(mk_rescue_pool(), dis, target=1.0, inv={'老水手': 4})
    ck(s3 is not None and not s3['meets_target'],
       '该据点禁用「老水手」→ 不救场、如实不达标')

    # 阶段级复现：stage_plan 选出的解带种子 → plan_variations 照种子实装
    # → 真实评估器复核 100%（端到端契约）
    plan, skipped, _ = stage_plan(mk_rescue_pool(), [node], target=1.0,
                                  use_local_search=False, inv={'老水手': 4})
    ck(len(plan) == 1 and not skipped, '阶段级：该据点成功编队')
    _, _, notes2 = plan_variations(plan, {'老水手': 4}, uses_max=None,
                                   target=1.0)
    ck(plan[0]['meets_target'] and min_rate(plan[0]['win_rates']) >= 0.999,
       '阶段级 plan_variations 复现救场（100%）')
    ck(plan[0]['spc'].count('老水手') == 4,
       '阶段级实装了 4 个老水手（%s）' % plan[0]['spc'])
    ck(any('场景式重试救场' in x for x in notes2),
       '变项分配说明标注了救场种子来源（可追溯）')

    # 乐观开关本身：nomatch=True 让「打不到」的减益计入搜索评估
    cards = [mkcard(i, [45, 45, 45],
                    debuff=[{'type': '烟幕', 'dir': 'up'}])
             for i in range(1, 5)]
    enemy = {'powers': [200] * 4, 'weak': [{'速射'}] * 4}
    r0 = search_assignment([dict(c) for c in cards], enemy,
                           rate_levels=(1.0,))
    r1 = search_assignment([dict(c) for c in cards], enemy,
                           rate_levels=(1.0,), nomatch=True,
                           debuff_x2=True)
    ck(r0.get(1.0) is None and r1.get(1.0) is not None,
       'search_assignment：保守模型不可行、nomatch+debuff_x2 乐观场景可行')
    ck(r1[1.0]['stats']['enemy_eff'][0] < 200,
       '乐观场景下无匹配减益也削到了敌方（enemy_eff %s）'
       % r1[1.0]['stats']['enemy_eff'][0])


def t_disabled_all():
    print('\n== 9j. 「区域·禁用变项」= 全部变项不可用（用户 2026-10-12 裁决） ==')
    from m11_plan import (resolve_disabled, format_plan, ALL_VAR_NAMES,
                          _any_var_installable)
    reg = {'ban_item': True, 'scope': '区域'}

    # 判据（单一实现 resolve_disabled，parse_node 与测试共用）
    dis, note = resolve_disabled(reg, None)
    ck(set(dis) == set(ALL_VAR_NAMES) and len(dis) == 11,
       '区域·禁用变项 未填名单 → 全部 11 门不可用（含效率提升）')
    ck(note and '全部变项不可用' in note and 'disabled_variations' in note,
       'review 说明：已按全禁用规划 + 可填名单覆盖')
    dis, note = resolve_disabled({'ban_item': True}, None)
    ck(len(dis) == 11 and note, '作用域未读出 → 同样按全禁用（判据同 _cond_active）')
    dis, note = resolve_disabled(reg, [])
    ck(dis == [] and note is None, '玩家给空列表 → 显式「不禁用」豁免（不猜测）')
    dis, note = resolve_disabled(reg, '炮术训练')
    ck(dis == ['炮术训练'], '玩家名单（单字符串归一化）→ 以玩家为准覆盖面板结论')
    dis, note = resolve_disabled(reg, ['炮术训练', '不存在的变项'])
    ck(dis == ['炮术训练'] and note and '未知名字' in note,
       '未知变项名忽略并提示')
    for sc in ('带入', '指定', '全场'):
        dis, note = resolve_disabled({'ban_item': True, 'scope': sc}, None)
        ck(dis == [] and note and '未填写具体变项' in note,
           '作用域 %s → 按既有口径不生效（不禁用）+ 人工确认提示' % sc)
    ck(resolve_disabled({'ban_item': False, 'scope': '区域'}, None)
       == ([], None), '面板无「禁用变项」→ 空名单、无提示')

    # 端到端：合成「区域·禁用变项」据点（敌方 310，只有装变项才到 100%）
    cards = [mkcard(i, [100, 100, 100]) for i in range(1, 5)]

    def ban_node(foe, dis_in):
        """合成据点：禁用名单走 resolve_disabled（与 parse_node 同一路径）"""
        n = mknode('boss-allban', [foe] * 4, progress=True, tokens=2400)
        n['progress_cards'] = [1]
        n['cards'] = [{'power': foe, 'token': 2400, 'coin': 'progress',
                       'tier': None} for _ in range(4)]
        n['cond'] = {'attr': None, 'pct': None, 'scope': '区域',
                     'ban_item': True, 'directive': None}
        dis, note = resolve_disabled(n['cond'], dis_in)
        n['disabled'] = dis
        n['review'] = [note] if note else []       # parse_node 同样落到 review
        return n

    node = ban_node(310, None)
    st, wr = eval_cards(cards, node)
    p = {'cards': cards, 'nodes': node, 'std': [], 'spc': [],
         'win_rates': wr, 'stats': st}
    _, _, notes = plan_variations([p], {}, uses_max=None, target=1.0)
    ck(p['std'] == [] and p['spc'] == [] and not p['meets_target'],
       '全禁用据点：一门变项都不装、如实未达标（310 打不动）')
    ck(any('全部变项不可用' in x and '效率提升' in x and '+10' in x
           for x in notes),
       '效率提升装不上 → 强化告警（进度代币 +10%% 拿不到）')
    ck(not _any_var_installable(node, {}),
       '全禁用据点：排列攻坚前置闸门关闭（_any_var_installable=False）')

    # 输出：条件行显示「该据点全部变项不可用」，不再列 11 个名字
    txt = format_plan([p], cards, [], [node],
                      {'current': 1, 'total': 1, 'missing': []},
                      None, None, notes, [], 'CONFIRMED', target=1.0)
    cond_line = [ln for ln in txt.splitlines()
                 if ln.startswith('  战斗条件：')][0]
    ck('禁用变项（该据点全部变项不可用）' in cond_line,
       'format_plan 条件行：禁用变项（该据点全部变项不可用）')
    ck(not any(nm in cond_line for nm in ALL_VAR_NAMES),
       '条件行不再罗列 11 门变项名（%s）' % cond_line.strip()[:40])
    ck('已按**该据点全部变项不可用**规划' in txt,
       '该据点的 review 里说明「已按全禁用规划 + 可填名单覆盖」')

    # 豁免：玩家给空列表 → 按不禁用 → 变项照常装（310 → 炮术训练 ×4 → 100%）
    node_b = ban_node(310, [])
    st2, wr2 = eval_cards(cards, node_b)
    p2 = {'cards': cards, 'nodes': node_b, 'std': [], 'spc': [],
          'win_rates': wr2, 'stats': st2}
    _, _, notes2 = plan_variations([p2], {}, uses_max=None, target=1.0)
    ck(p2['meets_target'] and p2['std'].count('炮术训练') == 4,
       '空列表豁免 → 标准属性变项照装、达标 100%%（%s）' % p2['std'])
    ck(not any('全部变项不可用' in x for x in notes2),
       '豁免后不再出现全禁用告警')


def t_efficiency_drop():
    print('\n== 9k. 「效率提升」最后手段：为达标可以卸掉它（用户 2026-10-12 裁决） ==')
    from m11_plan import ALL_VAR_NAMES

    def mk_prog(name, foe, tokens=2400):
        n = mknode(name, [foe] * 4, progress=True, tokens=tokens)
        n['progress_cards'] = [1]          # 效率提升定向装到位置1
        return n

    cards = [mkcard(i, [50, 50, 50]) for i in range(1, 5)]   # 150 → 140-160
    ck(min_rate(eval_cards(cards, mk_prog('x', 145))[1]) < 1.0,
       '前提：无变项 140-160 vs 145 → 76%%（需每单位一门炮术训练才 100%%）')

    # ① 装了效率提升就够不到 100%%（它占掉一个标准槽）→ 卸掉后 4 门炮术训练
    node = mk_prog('boss-drop', 145)
    st, wr = eval_cards(cards, node)
    p = {'cards': [dict(c) for c in cards], 'nodes': node, 'std': [],
         'spc': [], 'win_rates': wr, 'stats': st}
    _, _, notes = plan_variations([p], {}, uses_max=None, target=1.0)
    ck(p['meets_target'] and min_rate(p['win_rates']) >= 1.0 - 1e-9,
       '卸掉效率提升后达标 100%%（旧行为卡在 76%%）')
    ck('效率提升' not in p['std'] and p['efficiency_dropped']
       and not p['efficiency'] and p['std'].count('炮术训练') == 4,
       '采用卸掉方案：std=%s、efficiency=False、efficiency_dropped=True'
       % p['std'])
    ck(all(e['std'] != '效率提升' for e in p['equip'].values()),
       '装备表里已无效率提升（释放的标准槽装了属性变项）')
    ck(any('放弃「效率提升」以达标' in x and '−10' in x for x in notes),
       '显式说明：本据点放弃效率提升以达标（进度代币收益丢失约 −10%%）')

    # ② 反向：卸掉仍不达标 → 回滚（保持强制装 + 原有告警，不改默认口径）
    node2 = mk_prog('boss-keep', 170)
    st2, wr2 = eval_cards(cards, node2)
    p2 = {'cards': [dict(c) for c in cards], 'nodes': node2, 'std': [],
          'spc': [], 'win_rates': wr2, 'stats': st2}
    inv2, left2, notes2 = plan_variations([p2], {'炮术训练': 9}, uses_max=3,
                                          target=1.0)
    ck('效率提升' in p2['std'] and p2['efficiency']
       and not p2['efficiency_dropped'],
       '卸掉仍不达标 → 回滚：效率提升仍在（默认强制不被破坏）')
    ck(p2['std'].count('炮术训练') == 3 and '效率提升' in p2['std']
       and left2 == 0 and inv2 == {'炮术训练': 6},
       '回滚复原装备/次数/库存（%s / left=%s / inv=%s）'
       % (p2['std'], left2, inv2))
    ck(any('仍只有' in x for x in notes2)
       and not any('放弃「效率提升」' in x for x in notes2),
       '原有告警照旧（如实未达标）、不写「放弃」说明')

    # ③ 默认强制：装了也达标的据点不得卸
    node3 = mk_prog('boss-ok', 120, tokens=600)
    st3, wr3 = eval_cards(cards, node3)
    p3 = {'cards': [dict(c) for c in cards], 'nodes': node3, 'std': [],
          'spc': [], 'win_rates': wr3, 'stats': st3}
    _, _, notes3 = plan_variations([p3], {}, uses_max=None, target=1.0)
    ck(p3['std'] == ['效率提升'] and p3['efficiency']
       and not p3['efficiency_dropped'] and p3['meets_target'],
       '装了也达标的据点：效率提升保留（最后手段不日常降级）')

    # ④ 与「禁用变项（全禁用）」叠加：效率提升没装上 → 本步自然无操作
    node4 = mk_prog('boss-ban', 145)
    node4['disabled'] = list(ALL_VAR_NAMES)
    st4, wr4 = eval_cards(cards, node4)
    p4 = {'cards': [dict(c) for c in cards], 'nodes': node4, 'std': [],
          'spc': [], 'win_rates': wr4, 'stats': st4}
    _, _, notes4 = plan_variations([p4], {}, uses_max=None, target=1.0)
    ck(p4['std'] == [] and not p4['efficiency_dropped']
       and not any('放弃「效率提升」' in x for x in notes4),
       '全禁用叠加：效率提升没装上 → 最后手段自然无操作')

    # ⑤ 搜索阶段的验证关掉第 5 步（allow_drop=False）：不允许「卸掉效率提升」
    # 的解在**搜索阶段**被接受 —— 否则会抢在「场景式重搜 / 排列攻坚」前面，
    # 挤掉「保留效率提升」的救场解（实测 s4 BOSS 白丢 10%% 进度代币）
    from m11_plan import _install_variations
    ck(_install_variations(cards, node, {}, target=1.0,
                           allow_drop=False) is None
       and _install_variations(cards, node, {}, target=1.0) is not None,
       'allow_drop=False → 搜索阶段不采用放弃解；阶段级收官（默认）仍能靠它达标')


def t_perm_attack():
    print('\n== 9l. 强阵排列攻坚：失败据点的第二道兜底（借鉴队友核心） ==')
    import m11_plan as M
    from m11_plan import strong_perm_attack, PERM_MAX_PERMS, PERM_BUDGET_S

    # 弱卡 45/45/45=135 → 125-145（减益 1 种命中）；强卡 60/60/60=180 → 170-190。
    # 敌方 [240,150,150,150]、4 个位置弱点都是「速射」：
    #   · 保守模型（计入 +20% 单属性变项）下两阵营 min 都是 0 → 平局 → 比
    #     减益命中数 → **弱卡阵营胜出**（裸搜把弱卡选出来的形状）；
    #   · 真实模型：弱卡连「心无旁骛」（停用效果 → 179-199）都救不动 240；
    #     强卡装 1 个「心无旁骛」（242-262）即 100% —— 只有排列攻坚能选出来。
    weak = [mkcard(i, [45, 45, 45], debuff=[{'type': '速射', 'dir': 'up'}])
            for i in range(1, 5)]
    strong = [mkcard(i, [60, 60, 60]) for i in range(11, 15)]
    pool = weak + strong
    node = mknode('boss-perm', [240, 150, 150, 150], weak=[{'速射'}] * 4)
    inv = {'心无旁骛': 4}

    # 前提：弱卡阵营用尽全部可用变项仍不达标
    pw = {'cards': [dict(c) for c in weak], 'nodes': node, 'std': [],
          'spc': [], 'win_rates': {}, 'stats': None}
    _, _, _ = plan_variations([pw], dict(inv), uses_max=None, target=1.0)
    ck(not pw['meets_target'],
       '前提：弱卡阵营「全部变项都救不动」（真实评估器 %d%%）'
       % round(min_rate(pw['win_rates']) * 100))
    ck(strong_perm_attack(pool, node, dict(inv), target=1.0,
                          budget_s=-1.0) is None
       and strong_perm_attack(pool, node, dict(inv), target=1.0,
                              max_perms=0) is None,
       '双保险：超预算（budget_s 耗尽）/ 无排列可查（max_perms=0）→ 放弃')

    # 诚实性：预算耗尽 → 调用方返回保守解并**如实**标未达标（不谎报）
    old = M.PERM_BUDGET_S
    M.PERM_BUDGET_S = -1.0
    try:
        s_base = search_node(pool, node, target=1.0, inv=dict(inv))
    finally:
        M.PERM_BUDGET_S = old
    ck(s_base is not None and not s_base['meets_target']
       and {c['id'] for c in s_base['cards']} == {1, 2, 3, 4},
       '预算内失败 → 返回保守解（弱卡）并如实标未达标（meets_target=False）')

    # 正常预算：排列攻坚选出强阵、真实评估器复核 100%
    s = search_node(pool, node, target=1.0, inv=dict(inv))
    ck(s is not None and s['meets_target']
       and min_rate(s['win_rates']) >= 1.0 - 1e-9,
       '排列攻坚：强阵被选中、真实评估器确认 100%%（乐观假设不泄漏）')
    ck({c['id'] for c in s['cards']} == {11, 12, 13, 14}
       and s.get('perm_tries') == 1,
       '总战力最强的排列第一个就命中 → 命中即停（perm_tries=%s）'
       % s.get('perm_tries'))
    ck(all(e['spc'] in (None, '心无旁骛')
           for e in (s.get('equip') or {}).values()),
       '解的装备 = 实装后的真实分配 %s'
       % sorted((i, e['spc']) for i, e in (s.get('equip') or {}).items()))

    # 顺序保证：保守搜索即达标的据点不得被排列攻坚接管（不日常降级）
    s_easy = search_node([dict(c) for c in weak], mknode('easy', [110] * 4),
                         target=1.0, inv={})
    ck(s_easy is not None and s_easy['meets_target']
       and 'perm_tries' not in s_easy,
       '保守搜索即达标的据点：不进入排列攻坚（顺序不被绕过）')
    ck(PERM_MAX_PERMS == 2000 and abs(PERM_BUDGET_S - 4.0) < 1e-9,
       '双保险默认值：max_perms=%d / budget_s=%.0f 秒/据点'
       % (PERM_MAX_PERMS, PERM_BUDGET_S))


def t_trim():
    print('\n== 9m. 变项收官修剪：卸掉冗余变项（含场景种子），退还库存与次数 ==')
    from m11_plan import trim_plan, reeval_entry, TRIM_KEEP, ALL_VAR_NAMES

    def scan_redundant(p, target=1.0):
        """逐门卸下扫描（排除 TRIM_KEEP）→ 冗余清单（与 .work 审计同口径）"""
        eq = p['equip']
        std_by = {i: e['std'] for i, e in eq.items() if e['std']}
        spc_by = {i: e['spc'] for i, e in eq.items() if e['spc']}
        out = []
        for slot, by in (('spc', spc_by), ('std', std_by)):
            for pos in sorted(by):
                if by[pos] in TRIM_KEEP:
                    continue
                s2, p2 = dict(std_by), dict(spc_by)
                (s2 if slot == 'std' else p2).pop(pos)
                _st, wr = eval_cards(p['cards'], p['nodes'],
                                     std_by=s2, spc_by=p2)
                if sum(1 for v in wr.values() if v >= target - 1e-9) \
                        >= len(p['cards']):
                    out.append((slot, pos, by[pos]))
        return out

    def wr_with(p, pos, slot, name, target=1.0):
        """在 p 的装备上临时把 (位置, 槽位) 设成 name / None → 最低胜率"""
        eq = {i: dict(e) for i, e in p['equip'].items()}
        eq[pos][slot] = name
        _st, wr = eval_cards(
            p['cards'], p['nodes'],
            std_by={i: e['std'] for i, e in eq.items() if e['std']},
            spc_by={i: e['spc'] for i, e in eq.items() if e['spc']})
        return min_rate(wr)

    # ---- ① 场景种子的「只加不拆」：冗余的特殊变项被修剪（同一变项名，
    #         需要的不剪、冗余的剪 —— 判定用的是真实评估器，不是名字）----
    # 4 张卡 45/45/45（125-145），各带减益「速射·上」（命中同位置敌方）；
    # 敌方 [180,180,180,60]：位置1~3 只有特化训练（×2 → 180×0.6=108）才 100%
    # （单次命中只到 144 → 9%），位置4 对 60 本来就 100% → 种在那里的
    # 第 4 门特化训练是冗余的。
    cards = [mkcard(i, [45, 45, 45], debuff=[{'type': '速射', 'dir': 'up'}])
             for i in range(1, 5)]
    node = mknode('种子冗余', [180, 180, 180, 60], weak=[{'速射'}] * 4)
    p = {'cards': [dict(c) for c in cards], 'nodes': node,
         'seed_spc': {0: '特化训练', 1: '特化训练', 2: '特化训练',
                      3: '特化训练'}}
    inv, left, notes = plan_variations([p], {'特化训练': 4}, uses_max=6,
                                       target=1.0)
    ck(p['meets_target'] and min_rate(p['win_rates']) >= 1.0 - 1e-9,
       '修剪后仍 100%（原解靠 4 门特化训练达标）')
    ck(p['trimmed'] == [('特化训练', 3)],
       '场景种子的第 4 门特化训练（位置4，卸掉仍 100%%）被修剪：%s'
       % (p['trimmed'],))
    ck(p['equip'][3]['spc'] is None
       and all(p['equip'][i]['spc'] == '特化训练' for i in (0, 1, 2)),
       '只剪冗余的那门：位置1~3 的特化训练保留（各位置都要它才 100%）')
    ck(p['spc'].count('特化训练') == 3 and inv == {'特化训练': 1},
       '库存退还：装上 4 门（4→0）后修剪 1 门 → 库存回到 1（%s）' % inv)
    ck(left == 6, '特殊变项不占次数上限 → uses_left 不变（6）')
    ck(any('修剪掉 1 门冗余变项（特化训练×1），库存/次数已退还' in x
           for x in notes), 'notes 有修剪说明（%s）'
       % [x for x in notes if '修剪' in x])
    ck(scan_redundant(p) == [], '修剪后逐门卸下扫描 → 再无冗余（0 门）')
    # 反证：保留的那门是**真必需**（评估器看得见它的效果）——
    # 卸掉位置3 的特化训练 → 该位置胜率掉到 0%，不再是 4/4。
    ck(wr_with(p, 2, 'spc', None) < 1.0 - 1e-9,
       '保留项确实必需：卸掉位置3 的特化训练 → 最低胜率降到 %.0f%%'
       % (wr_with(p, 2, 'spc', None) * 100))
    # 回装被修剪的那门 → 仍是 100%（它本来就是冗余的）
    ck(wr_with(p, 3, 'spc', '特化训练') >= 1.0 - 1e-9,
       '回装被修剪的那门 → 仍 100%（冗余判定用的是真实评估器）')

    # ---- ①b 标准属性变项被「后装的特化训练」覆盖而冗余 → 退还库存与次数 ----
    # 4 张卡 35/30/60（115-135，各带减益「速射·上」）；敌方 [145,145,160,120]：
    # 位置1/2 单靠方向舵操作训练达 100%，位置3 对 160（单减益后 128）方向舵
    # 只有 95% → 贪心再用特化训练（160×0.6=96）把它救到 100% → 位置3 的
    # 方向舵操作训练随之冗余（单调贪心 + 相邻/同位置耦合）。
    cards2 = [mkcard(i, [35, 30, 60], debuff=[{'type': '速射', 'dir': 'up'}])
              for i in range(11, 15)]
    node2 = mknode('贪心冗余', [145, 145, 160, 120], weak=[{'速射'}] * 4)
    p2 = {'cards': [dict(c) for c in cards2], 'nodes': node2}
    inv2, left2, notes2 = plan_variations(
        [p2], {'方向舵操作训练': 4, '特化训练': 4}, uses_max=3, target=1.0)
    ck(p2['meets_target'] and min_rate(p2['win_rates']) >= 1.0 - 1e-9,
       '修剪后仍 100%')
    ck(p2['trimmed'] == [('方向舵操作训练', 2)],
       '被特化训练覆盖的方向舵操作训练（位置3）被修剪：%s'
       % (p2['trimmed'],))
    ck(inv2.get('方向舵操作训练') == 4 and left2 == 1,
       '退还库存（方向舵操作训练：装 1 门 → 修剪退还 1 门，库存回到 4）'
       '与次数（装 3 退 1 → left=1）：inv=%s left=%s' % (inv2, left2))
    ck(p2['std'] == ['炮术训练'] * 2 and len(p2['std']) == 2,
       '标准属性变项只剩 2 门（原 3 门，%s）' % (p2['std'],))
    ck(any('修剪掉 1 门冗余变项（方向舵操作训练×1）' in x for x in notes2),
       'notes 有修剪说明')
    ck(scan_redundant(p2) == [], '修剪后逐门卸下扫描 → 再无冗余（0 门）')

    # ---- ② 不修剪「效率提升」与「快速维修」（即使卸掉仍达标）----
    cards3 = [mkcard(i, [45, 45, 45], hp=(2 if i == 1 else 3))
              for i in range(1, 5)]
    node3 = mknode('不修剪', [100] * 4, progress=True, tokens=600)
    node3['progress_cards'] = [1]
    p3 = {'cards': [dict(c) for c in cards3], 'nodes': node3,
          'equip': {0: {'std': '效率提升', 'spc': None},
                    1: {'std': None, 'spc': '快速维修'},
                    2: {'std': '炮术训练', 'spc': None},
                    3: {'std': None, 'spc': None}},
          'std': ['效率提升', '炮术训练'], 'spc': ['快速维修'],
          'repairs': {}}
    reeval_entry(p3)
    ck(min_rate(p3['win_rates']) >= 1.0 - 1e-9, '前提：装了也 100%')
    ck(wr_with(p3, 0, 'std', None) >= 1.0 - 1e-9
       and wr_with(p3, 1, 'spc', None) >= 1.0 - 1e-9,
       '前提：「效率提升」「快速维修」卸掉仍 100%（本用例才有意义）')
    inv3, left3 = trim_plan([p3], {'效率提升': 0, '炮术训练': 5}, 2)
    ck(p3['trimmed'] == [('炮术训练', 2)],
       '同一据点里只有冗余的炮术训练被剪：%s' % (p3['trimmed'],))
    ck(p3['std'] == ['效率提升'] and p3['spc'] == ['快速维修']
       and p3['equip'][0]['std'] == '效率提升'
       and p3['equip'][1]['spc'] == '快速维修',
       '「效率提升」（进度代币收益）与「快速维修」（hp 跨阶段接力）不被修剪')
    ck(inv3.get('炮术训练') == 6 and inv3.get('效率提升') == 0 and left3 == 3,
       '效率提升的库存不动、炮术训练/次数已退还：inv=%s left=%s'
       % (inv3, left3))

    # ---- ③ 全禁用 / 无变项据点不受影响（修剪自然无操作）----
    node4 = mknode('全禁用', [150, 150, 150, 60], weak=[{'速射'}] * 4)
    node4['disabled'] = list(ALL_VAR_NAMES)
    p4 = {'cards': [dict(c) for c in cards], 'nodes': node4}
    _, _, notes4 = plan_variations([p4], {'特化训练': 4}, uses_max=6,
                                   target=1.0)
    ck(p4['trimmed'] == [] and p4['std'] == [] and p4['spc'] == [],
       '全禁用据点：一门都没装 → 修剪无操作（trimmed=[]）')
    ck(not any('修剪' in x for x in notes4), '不写修剪说明（无冗余可谈）')
    node5 = mknode('无变项', [100] * 4)
    p5 = {'cards': [dict(c) for c in cards], 'nodes': node5}
    inv5, left5, notes5 = plan_variations([p5], {}, uses_max=None,
                                          target=1.0)
    ck(p5['trimmed'] == [] and p5['meets_target'] and inv5 == {}
       and left5 is None, '无变项据点：不受影响（仍达标、库存/次数不动）')


def t_enemy_power():
    print('\n== 9j. 「敌方战力 ±X%」面板显示 vs 战斗值（2026-10-13 实拍对照） ==')
    # 据点8 形状：带入段「敌方战力 +8%」→ 面板绿字 173/178/151/170 是**含加成**的，
    # 部署界面（真实战斗）为 160/165/140/157 = 面板 ÷ 1.08
    n8 = {'cond': {'our_pairs': [{'attr': '敌方战力', 'pct': 8, 'scope': '带入'},
                                 {'attr': '机动性', 'pct': -36, 'scope': '带入'}]}}
    ck(enemy_pct_of(n8['cond']) == (0.08, 0.0),
       '带入段「敌方战力 +8%」→ 显示 +8%、战斗 +0%（面板虚高，战斗用基础值）')
    ck(cond_pct(n8) == {} and cond_all_pct(n8) == 0.0,
       '「敌方战力」不进我方单参数通道、也不进全参数通道（此前会误算成我方 +8%）')

    # 据点5 形状：同一条件挂在**全域**段 → 只宣告、不改本据点显示也不参与战斗
    n5 = {'cond': {'our_pairs': [{'attr': '敌方战力', 'pct': 8, 'scope': '全域'}]}}
    ck(enemy_pct_of(n5['cond']) == (0.0, 0.0),
       '全域段「敌方战力 +8%」不改本据点显示值（实测面板与部署界面一致）')

    # 区域段（暂无实拍样例）：按「区域 = 永久生效」推为战斗加成
    nz = {'cond': {'our_pairs': [{'attr': '敌方战力', 'pct': 10, 'scope': '区域'}]}}
    ck(enemy_pct_of(nz['cond']) == (0.0, 0.10), '区域段 → 只作战斗加成（不反推显示）')
    ck(cond_enemy_pct(nz) == (0.0, 0.10), 'cond_enemy_pct 取 node[cond]')

    # 单值兜底路径（无 pairs）：靠 scope 分流
    nb = {'cond': {'attr': '敌方战力', 'pct': 8, 'scope': '带入'}}
    ck(enemy_pct_of(nb['cond']) == (0.08, 0.0), 'pairs 缺失时走单值兜底')
    # 我方侧的「战力 −X%」不受影响
    no = {'cond': {'attr': '战力', 'pct': -24, 'scope': '区域'}}
    ck(cond_all_pct(no) == -0.24 and enemy_pct_of(no['cond']) == (0.0, 0.0),
       '我方「战力 −24%」仍是全参数削弱，不被当成敌方加成')


def main():
    for t in (t_map, t_crossdomain, t_power, t_buff, t_cond, t_default_target,
              t_snapshots, t_pool, t_varconfig,
              t_stage_plan, t_variations, t_local_search, t_json_out,
              t_html_panel, t_input_editor, t_all_stages, t_cond_types,
              t_banned_input, t_var_input, t_disabled_var,
              t_final_stage_reserve, t_std_variation, t_cond_display,
              t_scenario_search, t_disabled_all, t_efficiency_drop,
              t_perm_attack, t_trim, t_enemy_power):
        t()
    print()
    if FAIL:
        print('失败 %d 项：' % len(FAIL))
        for x in FAIL:
            print('  - ' + x)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
