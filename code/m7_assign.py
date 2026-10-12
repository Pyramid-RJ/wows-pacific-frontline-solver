"""战斗编队求解器 —— 站位几何 + 方向匹配 + 增益叠加

补齐设计方案中此前缺失的部分：原设计定义了 pos[i] 变量与C3 位置唯一约束，
但求解算法只按战力贪心填位，完全没有计算「减益方向是否命中目标弱点」
与「增益方向是否有友军」。本模块是那部分缺失的算法实现。

====================  版式与规则  ====================

部署区布局（实测1920×806）：
    [宽槽0] [窄] [宽槽1] [窄] [宽槽2] [窄] [宽槽3]
     ↑对应敌方0↑敌方1       ↑敌方2      ↑敌方3
  宽槽 k = 与第 k 个敌方单位正面对阵的位置
  窄槽   = 该位置与相邻我方卡之间的连接

方向语义（与手册「摆放要点」一致）：
  我方卡放在位置 k 时：
    「上」→正对敌方 k（唯一的正前方）
    「左」→ 敌方 k-1（同排相邻）
    「右」→ 敌方 k+1（同排相邻）
  即：三个方向都指向**敌方单位**，区别只在于打哪一张。

  我方卡的减益图标若标某方向，则该方向的敌方必须具备对应弱点，才产生效果。
  我方卡的增益图标若标左/右，则该位置必须有**友军**并排，才产生效果。

====================  数值规则  ====================

减益叠加（规则手册「减益/增益对照表」+ 游戏内浮窗实拍）：
  单次命中 −20%；同一弱点最多被命中 3 次 → 该弱点达 −60%
  不同弱点被命中时，减益加算
  → 单卡 2 减益槽，若两个减益命中**不同**弱点则 −40%
  → 同一弱点被本卡两个槽同时命中也只有 −40%（2 次 × 20%，未到 3 次上限）

  实拍佐证：敌方弱点浮窗显示 −60%（该弱点打满后的最终值），
  我方减益浮窗显示 −20%（单次命中值）。3 × 20% = 60%，两者吻合。

  注意：这里的 −20% / −60% 指**该弱点对应属性**的削弱，
  实际战力影响需在胜率层按「区间档数法」换算。

增益叠加（**只提升一项参数，不是提升总战力**）：
  绿色增益提升相邻友军的**某一条参数**（火力 / 存活性 / 机动性）+30%，
  装备「团队合作」（标准变项）后 ×2 至 +60%。
  增益作用于**友军**，因此只与站位布局有关，与敌方弱点无关。

  ⚠ 2026-10-10 修正：早期实现把增益乘在**总战力区间**上（our_power × 1.3），
  这是错的 —— 总战力必须按「逐项参数加成后重新求和」得到。
  用户实拍对照：USS圣地亚哥（火力35/生存35/机动70，hp2=90%）
  受相邻友军「火力」增益后，游戏内显示 火力41 / 生存32 / 机动63，
  合计 136 → 总战力区间 126–146。
    · 35×0.9×1.3 = 40.95 → 41   （增益只作用于火力这一项）
    · 35×0.9     = 31.5  → 32
    · 70×0.9     = 63    → 63
  旧算法会得出 [116,136]×1.3 = [151,177]，与实拍不符。

  另有「特化训练」（特殊变项）使减益效果 ×2，
  「心无旁骛」（特殊变项）则停用所有增益减益但参数 +40%。
"""


import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from weaktypes import to_en    # 弱点类型中英统一（修 4.7 静默错误）

# ============ 数值常量（与规则手册一致） ============
DEBUFF_STEP = 0.20      # 单次减益命中 −20%
DEBUFF_CAP = 0.60       # 同一弱点减益上限 −60%（3 次）
DEBUFF_MAX_HITS = 3     # 同一弱点最多被命中 3 次
BUFF_BASE = 0.30        # 增益基准 +30%
BUFF_TEAMWORK = 0.60    # 装备「团队合作」后 +60%

# ---- 参数 / 生命值 / 总战力（手册 §七；全项目唯一实现，勿在他处另写一份）----
ATTR_NAMES = ['火力', '存活性', '机动性']
HP_MAX = 3
HP_FACTOR = {3: 1.00, 2: 0.90, 1: 0.80}   # 手册第六节：参数按剩余格数折减
POWER_HALF = 10                           # 总战力区间半宽（恒 ±10）
STD_ATTR_PCT = 0.20                       # 标准属性变项（炮术/伤害控制/方向舵）：单项 +20%


def stats_power(stats, hp=HP_MAX, attr_pct=None, all_pct=0.0):
    """三项参数 → (逐项取整后的参数, 总战力区间)

    手册 §七：**总战力 = 火力 + 存活性 + 机动性 ± 10**。
    生命值折减与各项加成一律作用于**参数**，逐项四舍五入后再求和
    （故区间宽度恒为 21 档；拉菲 55/60/65 → 180 → 170–190 即此口径）。

    attr_pct: {属性名: 比例}，例如
        {'火力': 0.30} —— 相邻友军的绿色「火力」增益
        {'机动性': 0.20} —— 「方向舵操作训练」属性变项
    同一属性被多个来源加成时**加算**（30% + 30% = 60%）。

    all_pct: 全参数加成（「心无旁骛」+40%）。
    """
    f = HP_FACTOR.get(int(hp), 1.0)
    s = [float(x) * f for x in stats]
    if all_pct:
        s = [x * (1.0 + all_pct) for x in s]
    for i, nm in enumerate(ATTR_NAMES):
        p = (attr_pct or {}).get(nm)
        if p:
            s[i] *= (1.0 + p)
    rr = [int(math.floor(x + 0.5)) for x in s]      # 逐项四舍五入（半值进位）
    tot = sum(rr)
    return rr, [tot - POWER_HALF, tot + POWER_HALF]


# ============ 站位几何 ============
N_SLOT = 4


def neighbors(k):
    """位置 k 的三方向对应的敌方索引（上/左/右）"""
    return {'up': k,
            'left': k - 1 if k > 0 else None,
            'right': k + 1 if k < N_SLOT - 1 else None}


def has_friend_at(k, pos_of):
    """位置 k 的左/右是否存在友军（用于增益判定）"""
    nb = neighbors(k)
    out = {}
    for d in ('left', 'right'):
        j = nb[d]
        out[d] = (j is not None) and (pos_of.get(j) is not None)
    return out


# ============ 减益计算 ============
def debuff_hits(card, slot, enemy_weak, nomatch=False):
    """单张我方卡在某位置产生的减益命中

    card: our_fleet.json 中的一张卡
    slot: 0..3
    enemy_weak: {敌方索引: set(弱点类型)} 或按索引排列的 [set, set, ...]
                （类型可中可英，内部统一为英文 key）
    nomatch: 「老水手」—— 减益无需对应弱点即生效（方向上须有敌方单位）

    返回 {弱点类型(英文 key): 命中方向列表}

    **类型统一（修路线文档 4.7）**：our_fleet.json 用中文、m8_weak 用英文，
    不统一会让减益命中数静默归零，故两侧都先过 `to_en()`。
    """
    if isinstance(enemy_weak, dict):
        ew = {j: {e for e in (to_en(w) for w in ws) if e}
              for j, ws in enemy_weak.items()}
    else:
        ew = {i: {e for e in (to_en(w) for w in ws) if e}
              for i, ws in enumerate(enemy_weak)}
    nb = neighbors(slot)
    hits = {}
    for d in card.get('debuff', []):
        j = nb.get(d['dir'])
        if j is None:
            continue
        t = to_en(d['type'])
        if t is None:
            continue
        if nomatch or t in ew.get(j, ()):
            hits.setdefault(t, []).append(d['dir'])
    return hits


def debuff_multiplier(card, slot, enemy_weak, verbose=False,
                      debuff_x2=False, debuff_scale=1.0):
    """单卡减益后的战力系数

    规则（手册 + 浮窗实拍）：
        单次命中 −20%；同一弱点最多命中 3 次 → 该弱点封顶 −60%；
        不同弱点的减益**加算**。

    规则修正说明：
        早期版本误取「每命中一次 −30%」，与手册
        「单次 −20%、3 次 ×20% = 60%」矛盾，已改为 −20%。
        单卡只有 2 个减益槽，最多命中 2 次，故单卡实际封顶 −40%
        （除非同卡两槽打同一弱点，仍是 2 次 → −40%）。

    特化训练（特殊变项）使减益效果 ×2：此时单次 −40%，2 次 −80%。

    返回 (系数, {弱点: 命中次数})
    """
    hits = debuff_hits(card, slot, enemy_weak)
    if not hits:
        return 1.0, hits
    step = DEBUFF_STEP * (2 if debuff_x2 else 1) * debuff_scale
    penalty = 0.0
    for wt, dirs in hits.items():
        n = min(len(dirs), DEBUFF_MAX_HITS)
        penalty += min(step * n, DEBUFF_CAP * (2 if debuff_x2 else 1))
    mult = max(0.0, 1.0 - penalty)
    if verbose and hits:
        print('    减益命中 %s → ×%.2f' % (hits, mult))
    return mult, hits


# ============ 增益计算 ============
def buff_gain(card, slot, pos_of):
    """单张我方卡的增益是否生效

    增益图标仅左/右，需该方向有友军并排。
    返回生效的增益列表。
    """
    nb = neighbors(slot)
    active = []
    for b in card.get('buff', []):
        j = nb.get(b['dir'])
        if j is not None and pos_of.get(j) is not None:
            active.append((b['type'], b['dir'], j))
    return active


def team_stats(cards, slots, enemy_weak, enemy_powers=None,
               debuff_x2=False, teamwork=False,
               attr_pct=None, all_pct=0.0, disable_effects=False,
               pos_attr_pct=None, all_pct_pos=None, teamwork_pos=None,
               debuff_x2_pos=None, nomatch_pos=None, buff_factor=1.0,
               debuff_scale=1.0):
    """整队战力评估（双侧模型）

    cards:        [{'id','name','power':[lo,hi],'stats':[f,s,m],'hp',
                    'debuff','buff'}, ...]，长度 ≤4
    slots:        与 cards 对应的位置列表
    enemy_weak:   {敌方索引: set(弱点类型)}
    enemy_powers: {敌方索引: 固定战力值}（敌方不带浮动区间）
    debuff_x2:    「特化训练」（减益 ×2）—— 全队口径（旧参数）
    teamwork:     「团队合作」（增益 +30% → +60%）—— 全队口径（旧参数）
    attr_pct:     {属性名: 比例} —— 全队属性变项（旧参数）
    all_pct:      全参数加成 —— 全队口径
    disable_effects: 停用全部增益与减益（「心无旁骛」的副作用）
    pos_attr_pct: {位置: {属性: 比例}} —— **每单位**属性变项
    all_pct_pos:  {位置: 比例} —— 每单位全参数（心无旁骛 +0.40）
    teamwork_pos: {位置} —— 该单位**发出**的增益 ×2（团队合作，规则卡 16：
                  变项只影响应用它的单位）
    debuff_x2_pos: {位置} —— 该单位发出的减益 ×2（特化训练）
    nomatch_pos:  {位置} —— 该单位减益无需匹配弱点（老水手）
    buff_factor:  我方增益折算系数（「增益效果 −100%」区域条件 → 0）
    debuff_scale: 我方减益强度系数（「减益效果 −50%」区域条件 → 0.5：
                  单次 −20% 变 −10%）

    减益上限（手册 §10 + 特化训练口径）：同一弱点每次命中 −20%、
    最多累计 3 次 → −60%；「特化训练」使该单位发出的减益 **×2**
    （单次 −40%），该弱点的上限也随之翻倍（−120%）。全局上限 −60%，
    有特化训练的命中参与时 −100%。

    返回 {
      'our_eff':     {位置: 我方有效战力区间 (lo, hi)},
      'our_stats':   {位置: [火力, 存活性, 机动性]（已含生命值/增益/变项）},
      'enemy_eff':   {位置: 敌方有效战力},
      'debuff_total': {位置: 该位置我方卡产生的减益命中},
      'buff_active': [(来源卡名, 属性, 方向, 被增益位置), ...],
      'buff_by_pos': {位置: {属性: 次数}},
      'buff_count':  {位置: 获得的增益次数},
    }
    """
    pos_attr_pct = pos_attr_pct or {}
    all_pct_pos = all_pct_pos or {}
    teamwork_pos = teamwork_pos or set()
    debuff_x2_pos = debuff_x2_pos or set()
    nomatch_pos = nomatch_pos or set()
    pos_of = {s: c for c, s in zip(cards, slots)}
    out = {'our_eff': {}, 'our_stats': {}, 'enemy_eff': {},
           'debuff_total': {}, 'buff_active': [], 'buff_by_pos': {},
           'buff_count': {s: 0 for s in slots}}
    if enemy_powers is None:
        ep = {}
    elif isinstance(enemy_powers, dict):
        ep = enemy_powers
    else:
        ep = {i: v for i, v in enumerate(enemy_powers)}

    # 1) 减益：削弱敌方。每个位置的我方卡打向 neighbors(slot) 的敌方
    x2_hit = [False]         # 是否有特化训练的命中参与（全局上限 −100%）
    debuff_map = {}          # 敌方索引: {弱点: [普通命中数, ×2 命中数]}
    for c, s in zip(cards, slots):
        nomatch = (not disable_effects) and s in nomatch_pos
        hits = debuff_hits(c, s, enemy_weak, nomatch=nomatch) \
            if not disable_effects else {}
        out['debuff_total'][s] = hits
        cx2 = debuff_x2 or s in debuff_x2_pos
        for wt, dirs in hits.items():
            for d in dirs:
                j = neighbors(s)[d]
                if j is None:
                    continue
                cnt = debuff_map.setdefault(j, {}).setdefault(wt, [0, 0])
                cnt[1 if cx2 else 0] += 1
                if cx2:
                    x2_hit[0] = True

    for j, pw in ep.items():
        lim = 0.6 + (0.4 if x2_hit[0] else 0.0)   # 全局：−60% / 有特化 −100%
        penalty = 0.0
        for wt, (n0, n2) in debuff_map.get(j, {}).items():
            # 「特化训练」使该单位发出的减益 ×2：单次 −40%
            # （与 debuff_multiplier 同口径；旧实现只抬高全局上限、
            #   单次仍是 −20%，与文档/实拍都不符）
            step = DEBUFF_STEP * (n0 + 2 * n2) * debuff_scale
            cap = DEBUFF_CAP * (2 if n2 else 1)
            penalty += min(step, cap)
        out['enemy_eff'][j] = round(pw * max(0.0, 1.0 - min(penalty, lim)))

    # 2) 增益：提升相邻友军的**一项参数**（禁用效果时整块跳过）
    if not disable_effects:
        for c, s in zip(cards, slots):
            for (btype, bdir, j) in buff_gain(c, s, pos_of):
                out['buff_active'].append((c['name'], btype, bdir, j))
                out['buff_count'][j] = out['buff_count'].get(j, 0) + 1
                d = out['buff_by_pos'].setdefault(j, {})
                d[btype] = d.get(btype, 0) + 1
    # 「增益效果 −X%」区域条件：所有我方增益按系数折算（−100% → 全部失效）
    bstep = (BUFF_TEAMWORK if teamwork else BUFF_BASE) * buff_factor

    for c, s in zip(cards, slots):
        pct = dict(attr_pct or {})            # 属性变项（全队口径，旧参数）
        for nm, pv in (pos_attr_pct.get(s) or {}).items():
            pct[nm] = pct.get(nm, 0.0) + pv   # 每单位属性变项（规则卡 16）
        for nm, n in out['buff_by_pos'].get(s, {}).items():
            # 「团队合作」使**发出增益的单位**的增益 ×2
            b = (BUFF_TEAMWORK if (teamwork or s in teamwork_pos)
                 else BUFF_BASE) * buff_factor
            pct[nm] = pct.get(nm, 0.0) + b * n
        ap = all_pct + all_pct_pos.get(s, 0.0)
        if c.get('stats'):
            # 正确口径：逐项参数加成后重新求和 → 总战力
            rr, rng = stats_power(c['stats'], c.get('hp', HP_MAX), pct, ap)
            out['our_stats'][s] = rr
            out['our_eff'][s] = (rng[0], rng[1])
        else:
            # 兼容：卡上没有 stats 时，退回「按区间整体乘」（老行为）
            lo, hi = c['power']
            m = 1.0 + bstep * out['buff_count'].get(s, 0)
            out['our_eff'][s] = (round(lo * m), round(hi * m))
    return out


# ============ 胜率计算（区间档数法） ============
def win_rate(our_lo, our_hi, enemy_power):
    """我方浮动区间 vs 敌方固定战力 → 胜率

    规则（手册「胜率计算方式」，三个案例精确吻合）：
        胜率 = 我方区间中「大于等于敌方战力」的取值数 ÷ 区间总档数

    手册验证案例：
        区间 83–103  vs 91  → 13/21 = 61.9%，界面显示 62%
        区间 88–108  vs 91  → 18/21 = 85.7%，界面显示 86%
        区间 105–125 vs 105 → 21/21 = 100%
    """
    if our_hi < enemy_power:
        return 0.0
    if our_lo >= enemy_power:
        return 1.0
    total = our_hi - our_lo + 1
    win = our_hi - enemy_power + 1
    return win / float(total)


def slot_win_rates(stats):
    """由 team_stats 结果算出每个位置的胜率

    stats: {'our_eff': {位置: (lo,hi)}, 'enemy_eff': {位置: 战力值}}
    返回 {位置: 胜率}
    """
    out = {}
    for s, (lo, hi) in stats['our_eff'].items():
        ep = stats['enemy_eff'].get(s)
        if ep is None:
            continue
        out[s] = win_rate(lo, hi, ep)
    return out


def std_variation_win_rates(stats, std_attrs=None):
    """逐位置胜率 = 「原样」与「任一单项参数 +20%」中的较高者

    用途：**选阵阶段就把「每单位可装 1 门标准属性变项」算进去**。
    规则卡 16 下每个单位有标准/特殊各 1 个变项栏位，标准属性变项（炮术训练 /
    伤害控制训练 / 方向舵操作训练）各给装备它的单位一项参数 +20%。若搜索
    按「无变项」评估，则**只有配上变项才达标的阵容会被系统性低估、选不出来**
    （实测：困难 BOSS 靠「阿拉斯加 + 存活性」才够到 100%，无变项模型下它
    只有 ~50%，于是搜索去选了别的弱阵容）。

    +20% 的取整口径与 `stats_power` 一致：取 `our_stats`（已含生命值折减 /
    增益 / 战斗条件的**整数**参数），把该属性 ×1.20 后过
    `stats_power(boosted, HP_MAX)`（HP_MAX → 不再二次折减）得区间，
    再按区间档数法算胜率。

    `std_attrs`：允许建模的属性下标（0 火力 / 1 存活性 / 2 机动性）；
    None = 三项都允许。**库存与「禁用变项」由调用方判定**（m11.search_node）。
    """
    out = {}
    for s, (lo, hi) in stats['our_eff'].items():
        ep = stats['enemy_eff'].get(s)
        if ep is None:
            continue
        out[s] = win_rate(lo, hi, ep)
        if out[s] >= 1.0 - 1e-9:
            continue        # 已全胜：+20% 不可能更高（省 3 次区间计算）
        base = (stats.get('our_stats') or {}).get(s)
        if not base:
            continue
        for k in range(len(base)):
            if std_attrs is not None and k not in std_attrs:
                continue
            b = list(base)
            b[k] = b[k] * (1.0 + STD_ATTR_PCT)
            rng = stats_power(b, HP_MAX)[1]
            out[s] = max(out[s], win_rate(rng[0], rng[1], ep))
    return out


# ============ 编队搜索 ============
def search_assignment(pool, enemy, min_rate=0.0, debuff_x2=False,
                      teamwork=False, rate_levels=(0.0, 0.85, 1.0),
                      attr_pct=None, all_pct=0.0, buff_factor=1.0,
                      std_variation=False, std_attrs=None, nomatch=False):
    """为一个据点搜索最佳编队

    pool:   可用我方卡列表 [{'id','name','power':[lo,hi],'debuff','buff'}, ...]
    enemy:  {'powers': [p0,p1,p2,p3], 'weak': [{弱点集合}, ...]}
            powers 为敌方**固定**战力值（不带浮动区间）
    min_rate: 兼容参数；实际按 rate_levels 一次遍历返回各阈值下的最优解
    debuff_x2 / teamwork: 是否按装备对应特殊/标准变项计算
    attr_pct: {属性名: 比例} —— **整队恒定**的参数加成/削弱（战斗条件，
              如 {'存活性': -0.24}）；评估每个候选编队时都带上
    all_pct: 全参数加成/削弱（「战力 −X%」类条件为负值）
    std_variation: True 时逐位置胜率额外考虑「该单位装 1 门标准属性变项
              （单项参数 +20%）」的可能，取它与「原样」中的较高者
              （见 `std_variation_win_rates`）—— 让「只有配上变项才达标」
              的阵容也能被选中。**注意**：`win_rates` 因此是**带上变项**的
              估计值，`stats` 仍是无变项的真实值；调用方（m11）必须以
              变项分配后的重算结果为准（plan_variations 会重新判定达标）。
    std_attrs: 允许建模的属性下标（None = 三项都允许）；
              由 m11.search_node 按该阶段变项库存与「禁用变项」决定。
    nomatch:  **场景式重试用**（m11.search_node 的乐观假设）：True =
              假设 4 个位置的单位**都带「老水手」**（减益无需匹配弱点，
              见 m7_assign.debuff_hits(nomatch=...)）。等价于给
              `team_stats` 传 `nomatch_pos = 全部位置`；与 debuff_x2
              同传即「都带老水手 + 特化训练」的最乐观减益场景。
              这只是**选阵阶段的假设** —— 是否真装得上由调用方用
              plan_variations + 真实评估器验证（库存按 var_stock_ok）。

    评分优先级（全歼口径，必须每场都赢）：
        1. 4 个位置全部 win_rate ≥ 阈值
        2. 最低胜率越高越好（对抗我方 ±10 浮动）
        3. 减益命中总数越多越好（削敌方更稳）
        4. 我方总有效战力越低越好（为后续阶段留存高战力）

    返回 {阈值: 解}，解为 {'score','perm','stats','win_rates'} 或 None。

    性能：全组合 C(30,4)×4! ≈ 65.7 万，逐个算 team_stats 在纯 Python 下
    约 10 秒。改为**单次遍历同时更新所有阈值档**，避免按阈值重复搜索
    （曾按3 个阈值各搜一次 → 30 秒）。
    """
    import itertools

    enemy_p = enemy['powers']
    enemy_w = enemy['weak']
    if isinstance(enemy_w, (set, frozenset)):
        enemy_w = [set(enemy_w)]
    enemy_w = [set(w) for w in enemy_w]
    floor_p = min(enemy_p)
    ceil_p = max(enemy_p)
    levels = sorted(set(list(rate_levels) + [min_rate]))

    # 剪枝 1：单卡至少要能打动某个敌方位置；过剩的高战力卡留给后续阶段。
    #   注意乘法方向：阈值是「敌方的 55%/125%」。
    #   若误写成「我方 hi×0.55 < floor_p」，等价于要求 hi ≥ floor_p/0.55 ≈ 240，
    #   会把全部 30 张卡剪掉、搜索恒返回 None。此坑已踩两次。
    viable = [c for c in pool
              if c['power'][1] >= floor_p * 0.55
              and c['power'][0] <= ceil_p * 1.25]
    if len(viable) < 4:
        return {lv: None for lv in levels}

    combos = [cb for cb in itertools.combinations(viable, 4)
              if sum(c['power'][1] for c in cb) >= sum(enemy_p) * 0.55]
    if not combos:
        return {lv: None for lv in levels}

    best = {lv: None for lv in levels}
    for combo in combos:
        for perm in itertools.permutations(combo):
            slots = list(range(4))
            st = team_stats(list(perm), slots, enemy_w, enemy_p,
                            debuff_x2=debuff_x2, teamwork=teamwork,
                            attr_pct=attr_pct, all_pct=all_pct,
                            buff_factor=buff_factor,
                            nomatch_pos=set(slots) if nomatch else None)
            wr = slot_win_rates(st)
            if len(wr) < 4:
                continue
            if std_variation:
                wr = std_variation_win_rates(st, std_attrs)
            lo_rate = min(wr[s] for s in slots)
            n_hit = sum(len(v) for v in st['debuff_total'].values())
            tot_our = sum(hi for lo, hi in st['our_eff'].values())
            score = (round(lo_rate, 4), n_hit, -tot_our)
            cand = {'score': score, 'perm': perm, 'stats': st, 'win_rates': wr}
            for lv in levels:
                if lo_rate < lv:
                    continue
                cur = best[lv]
                if cur is None or score > cur['score']:
                    best[lv] = cand
    return best


if __name__ == '__main__':
    import json
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent.parent

    # ---- 1) 胜率公式对账（手册三个案例） ----
    print('=== 胜率公式对账（手册案例） ===')
    for lo, hi, ep, want in [(83, 103, 91, '62%'), (88, 108, 91, '86%'),
                             (105, 125, 105, '100%')]:
        got = win_rate(lo, hi, ep)
        pct = '%d%%' % round(got * 100)
        print('  %d–%d vs %d → %.1f%% (界面 %s) %s'
              % (lo, hi, ep, got * 100, want,
                 'OK' if pct == want else '不符'))

    # ---- 2) 减益数值对账（浮窗实拍：单次 −20%，3 次 = −60%） ----
    print('\n=== 减益数值对账 ===')
    probe = {'id': 0, 'name': 'probe', 'power': [100, 100],
             'debuff': [{'type': '速射', 'dir': 'up'}], 'buff': []}
    ew = [{'速射'}, set(), set(), set()]
    m1, h1 = debuff_multiplier(probe, 0, ew)
    print('单次命中        → ×%.2f（应为 0.80）%s' % (m1, 'OK' if abs(m1-0.8) < 1e-6 else '不符'))
    m2, h2 = debuff_multiplier(probe, 0, ew, debuff_x2=True)
    print('特化训练 ×2     → ×%.2f（应为 0.60）%s' % (m2, 'OK' if abs(m2-0.6) < 1e-6 else '不符'))
    db = {'id': 0, 'name': 'd', 'power': [100, 100],
          'debuff': [{'type': '速射', 'dir': 'up'},
                     {'type': '速射', 'dir': 'up'}], 'buff': []}
    m3, _ = debuff_multiplier(db, 0, ew)
    print('同卡2槽同弱点   → ×%.2f（应为 0.60，2×20%%）%s'
          % (m3, 'OK' if abs(m3-0.6) < 1e-6 else '不符'))

    # ---- 3) 编队搜索（真实我方卡库） ----
    fleet = json.loads((ROOT / 'data' / 'our_fleet.json').read_text(
        encoding='utf-8'))['cards']
    enemy = {
        'powers': [149, 145, 132, 141],          # kp1 实测敌方战力
        'weak': [{'速射'}, {'速射', '烟幕'}, set(), {'大口径'}],
    }
    print('\n=== 编队搜索（敌方战力 %s） ===' % enemy['powers'])
    res = search_assignment(fleet, enemy)
    for lv in sorted(res):
        r = res[lv]
        if not r:
            print('  最低胜率 ≥%.0f%% → 无可行编队' % (lv * 100))
            continue
        print('  最低胜率 ≥%.0f%% → 评分 %s' % (lv * 100, r['score']))
        for s, c in enumerate(r['perm']):
            print('    位置%d %-22s 我方 %-9s 敌方 %d  胜率 %.0f%%'
                  % (s, c['name'], str(r['stats']['our_eff'][s]),
                     r['stats']['enemy_eff'][s], r['win_rates'][s] * 100))
        print('    减益命中: %s' % r['stats']['debuff_total'])
        print('    生效增益: %s' % (r['stats']['buff_active'] or '无'))