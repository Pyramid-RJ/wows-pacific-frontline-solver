"""M5 编排规划器 —— 变项分配 + 战力留存 + 次级货币约束 + 局部搜索 + 输出

职责（对应开发文档 §六 优化模型 / §八 输入契约 / §九 输出格式）
─────────────────────────────────────────────────────────────────────
把一个阶段的全部据点识别结果 + 玩家手填的卡池状态，变成一套可执行的编排建议：

    1. 输入层     卡池 = 全部 30 张 − 用户标注「没有的卡牌」；生命值折算
    2. 敌方编成   由 m4 / m8 / m9 / m10 的识别结果拼装（弱点类型统一为英文）
    3. 变项分配   效率提升优先（仅进度代币据点）→ 标准属性按需 → 特殊最后手段
    4. 战力留存   据点按难度倒序处理（难题先挑卡）＋ 正序分配剩余
    5. 选阵重试   场景式重试：保守模型搜出的解经 plan_variations + 真实
                  评估器验证；不达标时按更乐观的减益假设重搜（见 search_node）
    6. 局部搜索   跨据点 pairwise 交换，只要提升总目标就接受
    7. 输出       按开发文档 §九 的格式打印

核心口径（用户确认的优化优先级）
─────────────────────────────────────────────────────────────────────
    P0 阶段目标达成（全歼口径，每场都要赢）
    P1 进度代币收益最大化（**奖励为进度代币的据点**都要装「效率提升」，
       其 +10% 对该据点的进度代币收益生效 —— 用户 2026-10-13 明确：
       不限于关底 BOSS，任何掉进度代币的据点都吃这一加成；
       程序按据点级 `is_progress`（圆牌为金色）判定，逐据点装）
    P2 为后续阶段尽可能预留高战力 → 用「已消耗我方战力总量最小」实现
    P3 低战力敌人不占用高战力卡 → 难易倒序 + 剪枝自然实现
    P4 尽可能少用道具（含特殊变项），但「效率提升」必须用（仅进度代币据点）
    P5 特殊变项比卡牌更金贵（用户 2026-10-13）：特殊变项只能靠战斗奖励获得、
       一次性；卡牌每次行动都会恢复 → 能用卡解决就别花特殊变项
       （见 `stage_solve._maybe_no_spc` 与 `stage_gold_sufficient`）

关键实现约定
─────────────────────────────────────────────────────────────────────
* **战力由三项参数推出**：总战力 = 火力 + 存活性 + 机动性 ± 10。
  因此变项/生命值的影响可以直接作用在参数上再重算区间，无需手工缩放：
      区间 = [round(Σ参数) − 10, round(Σ参数) + 10]
  手册核对：USS拉菲 55+60+65 = 180 → 区间 170–190 ✓
* **生命值折算**（手册第六节）：3/2/1/0 格 → 参数 100%/90%/80%/不可部署。
  用户只给起始损耗，程序跨阶段累减（见 `advance_hp`）。
* **C1 阶段内单次参战**：一张卡在同一阶段只能投入一场战斗，
  但它的**增益**仍可继续生效（C6）—— 本模块按「先难题后易题」分配以满足 C1。
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from weaktypes import to_zh, to_en                      # noqa: E402
from m7_assign import (team_stats, slot_win_rates,      # noqa: E402
                       stats_power, ATTR_NAMES, neighbors,
                       HP_MAX, HP_FACTOR, POWER_HALF,
                       BUFF_BASE, BUFF_TEAMWORK,
                       std_variation_win_rates)
from m7_assign import search_assignment                 # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'


# ============ 规则常量 ============
# 生命值折算 / 总战力口径的唯一实现在 m7_assign（本模块只是转出，便于既有调用方）
COST_EFFICIENCY = 18      # 「效率提升」价格（次级代币）；现改为「次数上限」口径，此常量仅存档
EFFICIENCY_BONUS = 0.10                      # 进度代币 +10%
PROGRESS_MIN = 100                           # 进度代币数量下限（与 m6_coin 一致）

# 默认目标胜率下限 = 100%（用户确认 2026-10-11）。
# 理由：一局要打 4 个位置，任一位置不是 100% 就有翻车概率；
# 例如 4 个位置各 95% → 整场约 81%。低于 100% 的方案必须显式调低才会被接受。
# 用户可用 --target 自行下调（如 0.95 / 0.9）；调低时输出会显式标注。
TARGET_RATE = 1.0
STAT_NAMES = list(ATTR_NAMES)                # ['火力', '存活性', '机动性']

# 卡牌等级（用户确认的术语）：白色 = 1级 · 蓝色 = 2级 · 金色/红色 = 3级
LEVEL_ZH = {'white': '1级', 'blue': '2级', 'gold': '3级', 'red': '3级'}
SHOT_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')

# 标准变项（白，商店可买，6 种）
STD_VAR = {
    '炮术训练':       {'cost': 12, 'act': 'attr', 'stat': 0},
    '伤害控制训练':   {'cost': 12, 'act': 'attr', 'stat': 1},
    '方向舵操作训练': {'cost': 12, 'act': 'attr', 'stat': 2},
    '团队合作':       {'cost': 12, 'act': 'teamwork'},
    '效率提升':       {'cost': 18, 'act': 'efficiency'},
    '士气打击':       {'cost': 12, 'act': 'morale'},
}
ATTR_PCT = 0.20                              # 三项属性变项：+20%
TEAMWORK_BUFF = 0.60                         # 团队合作：增益 +30% → +60%

# 特殊变项（绿，关卡掉落，5 种）
SPC_VAR = {
    '心无旁骛': {'act': 'all_param'},        # 停用所有增益减益，全参数 +40%
    '特化训练': {'act': 'debuff_x2'},
    '老水手':   {'act': 'debuff_nomatch'},
    '快速维修': {'act': 'repair_hp'},
    '势不可挡': {'act': 'ignore_conditions'},
}
ALL_PARAM_PCT = 0.40
ALL_VAR_NAMES = set(STD_VAR) | set(SPC_VAR)   # 11 种变项名（校验玩家输入用）
STD_VAR_NAMES = set(STD_VAR)                  # 6 种标准变项（商店可买）

# 场景式重试的「更乐观减益假设」顺序（m11.search_node；第一次搜索不达标时
# 才逐个试）：(全队特化训练 ×2, 全位置老水手)。名义上等价于「4 个单位都带
# 老水手」「都带特化训练」—— 是否真装得上由 plan_variations + 库存验证。
SCENARIOS_OPTIMISTIC = ((True, False), (False, True), (True, True))

# 强阵排列攻坚（`strong_perm_attack`，失败据点的第二道兜底；借鉴队友核心
# `search_node_variations` 的 top-K 排列法）：
#   取可用池战力 top-K → 4-排列按总战力降序枚举、命中即停；
#   `max_perms` 与 `budget_s` 双保险，超预算放弃（不做隐式降级）。
PERM_TOPK = 12          # top-K 候选卡（弱卡陪跑不进排列）
PERM_MAX_PERMS = 2000   # 每据点最多评估的排列数
PERM_BUDGET_S = 4.0     # 每据点最多花多少秒（超时即放弃）


def var_stock_ok(inv, name):
    """该变项现在还有没有得用（库存口径）

    **标准变项未给值 = 无上限**（商店可买，用户 2026-10-12 口径：
    「标准变项留空设为无上限」）；给了值就按值 —— 显式 `0` = 本轮不可用、
    `N` = 最多 N 个。**特殊变项**（关卡掉落）未给值 = `0`，按实际库存。
    `inv` 是 {变项名: 数量}（`normalize_variations` / 截图工具栏的产物）。
    """
    if name in (inv or {}):
        return inv[name] > 0
    return name in STD_VAR_NAMES


# 三门标准属性变项（`STD_VAR` 里 act='attr' 的三项，stat = 属性下标）
ATTR_STD_VARS = [(STD_VAR[nm]['stat'], nm) for nm in
                 ('炮术训练', '伤害控制训练', '方向舵操作训练')]


def std_var_attrs_for(node, inv):
    """该据点**在选阵阶段**可以建模的标准属性变项 → 属性下标列表或 None

    判据（缺一不可）：
      · 库存可用 —— `var_stock_ok(inv, 变项名)`（玩家显式置 0 → 不可用；
        标准变项未给值 = 无上限 → 可用）
      · 该据点没禁用这门变项（区域条件「禁用变项」，`node['disabled']`）

    返回 None = 一门都不可用 → 搜索退回「无变项」模型（老行为）。
    """
    if inv is None:
        return None
    dis = node.get('disabled') or []
    out = [idx for idx, nm in ATTR_STD_VARS
           if var_stock_ok(inv, nm) and nm not in dis]
    return sorted(out) or None

# ---- 等级治理（合并步骤4，默认开启）：简单/普通不动用 3 级（gold）舰 ----
# tier 取值见 data/our_fleet.json：gold = 3 级（金色/红色）、blue、white
GOLD_TIERS = ('gold',)
SAVE_GOLD_LABELS = ('简单', '普通')


def pool_without_tiers(pool, tiers=GOLD_TIERS):
    """从卡池剔除指定等级（默认剔除 gold = 3 级）

    卡本身还在船坞里，只是**这个据点**不候它 —— 用于把高等级卡留给硬仗。
    """
    bad = {str(t).lower() for t in tiers}
    return [c for c in pool if str(c.get('tier') or '').lower() not in bad]


def node_saves_gold(node, labels=SAVE_GOLD_LABELS):
    """该据点是否受等级治理（简单 / 普通 —— 不动用 3 级舰船）"""
    return (node.get('difficulty_label') or '') in labels


def stage_gold_sufficient(pool, nodes):
    """本阶段的 3 级卡是否「够用」（用户 2026-10-13 口径）

    判据：池里可用的 3 级卡 ≥ 该阶段据点各 4 张不同卡的需求（`4 × 据点数`）。

    用途：**据点少、3 级卡够用时，优先花卡牌、少花特殊变项** ——
    因为特殊变项只能靠战斗奖励获得（一次性），而卡牌每次行动都会恢复状态。
    这类阶段的据点会先试「一门特殊变项都不花」的解。
    （据点一多，3 级卡就不够每人 4 张，硬塞会挤掉后续阶段的硬仗 → 不启用。）
    """
    n_gold = sum(1 for c in (pool or [])
                 if str(c.get('tier') or '').lower() in GOLD_TIERS)
    return n_gold >= 4 * len(nodes or [])


def node_prefers_no_spc(node, labels=SAVE_GOLD_LABELS):
    """该据点是否受「特殊变项预算」约束（4.30，用户口径）

    **低星关卡尽量少使用特殊变项，困难 / 关底不设限**。
    低星 = 难度 简单 / 普通；关底 = **掉进度代币**的据点（规则手册：只有关底
    BOSS 会掉落此类代币）→ 故判据 = 简单/普通 **且** 非关底。

    ⚠ 2026-10-13：用户新口径把它**推广到了两类据点** ——
    「特殊变项只能靠战斗奖励获得（一次性），卡牌每次行动都会恢复状态」，
    所以（a）低星据点、以及（b）3 级卡够用的少据点阶段（`stage_gold_sufficient`）
    里的据点，都先试「一门特殊变项都不花」的解，只有卡牌撑不住时才动特殊变项
    （见 `stage_plan` 的 `inv_tries`）。
    """
    if (node.get('difficulty_label') or '') not in labels:
        return False
    return not node.get('is_progress')


def inv_without_specials(inv):
    """库存副本：把**特殊变项**整类清零（缺键 = 不可用，见 `var_stock_ok`）

    用于低星据点的特殊变项预算 —— 先按「一门特殊变项都不用」搜一轮。
    """
    if inv is None:
        return None
    return {k: v for k, v in inv.items() if k not in SPC_VAR}


# ============ 1. 输入层 ============
def load_fleet(path=None):
    p = Path(path) if path else DATA / 'our_fleet.json'
    cards = json.loads(Path(p).read_text(encoding='utf-8'))['cards']
    # 舰种（2026-10-11 用户确认口径）：禁用舰种条件依赖此字段
    for c in cards:
        assert c.get('type') in ('驱逐舰', '巡洋舰', '战列舰', '飞机'), \
            '卡 %s 缺少合法的 type 字段（驱逐舰/巡洋舰/战列舰/飞机）' % c.get('name')
    return cards


def load_player_input(path):
    """读取玩家输入

    字段（开发文档 §八）：
      stage                {current, total}
      missing_cards        [自己没有的卡 id]（默认全有）
      damaged_cards        {卡 id: 当前剩余格数}（只写已损耗的）
      secondary_uses_max   本轮最多能用几次「次级代币购买的变项」
                           （**「效率提升」不计入此上限**，见 §七 规则 8）；
                           **留空/null = 不设上限（默认）**；
                           显式给了数字（含 0）才作为约束 ——
                           0 = 本轮不用次级代币变项
      banned_types         {截图名: [舰种]} —— 战斗条件「禁用的单位类型」
                           在面板上**只有图标没有文字**，玩家可照面板
                           补录或纠正图标识别（键 = 截图 stem，即
                           parse_node 的 name / --shots 里的名字）；
                           值允许单个字符串，空列表 = 显式「无禁用」
      variations           变项库存 —— **本阶段截图都没有变项工具栏时**用它替代
                           截图库存；有工具栏时忽略（以截图为准）。写法见
                           `normalize_variations`。语义：**标准变项未给值 =
                           无上限**（商店可买），显式 0 = 本轮不可用、N = 最多 N 个；
                           特殊变项（关卡掉落）未给值 = 0
      disabled_variations  {截图名: [变项名]} —— 区域条件「禁用变项」禁用的
                           具体变项（面板只写四个字、截图上又看不出，只能
                           人工填）。语义（用户 2026-10-12 裁决）：
                           未填 + 面板有「禁用变项」且作用域为区域/未读出 →
                           该据点**全部变项不可用**（含「效率提升」）；
                           填了（含空列表）= 以玩家为准 —— 空列表 =
                           显式「不禁用」，用于豁免全禁用
    """
    d = json.loads(Path(path).read_text(encoding='utf-8'))
    stage = d.get('stage') or {}
    # 兼容旧字段名 secondary_currency（曾表示次级货币存量）
    uses = d.get('secondary_uses_max')
    if uses is None:
        uses = d.get('secondary_currency')
    # 未指定（缺字段 / null）= 不设上限；显式给了数字（含 0）才作为约束
    banned = {}
    for k, v in (d.get('banned_types') or {}).items():
        vals = v if isinstance(v, (list, tuple)) else ([] if v is None else [v])
        banned[str(k)] = [str(x) for x in vals if x]
    # 禁用变项（区域条件「禁用变项」）：{截图名: [变项名]}，只收 11 个合法变项名
    disabled = {}
    for k, v in (d.get('disabled_variations') or {}).items():
        vals = v if isinstance(v, (list, tuple)) else ([] if v is None else [v])
        disabled[str(k)] = [str(x) for x in vals if str(x) in ALL_VAR_NAMES]
    return {
        'current': int(stage.get('current') or 1),
        'total': int(stage.get('total') or 1),
        'uses_max': None if uses is None else int(uses),
        'missing': [int(x) for x in (d.get('missing_cards') or [])],
        'damaged': {int(k): int(v) for k, v in (d.get('damaged_cards') or {}).items()},
        'banned': banned,
        'disabled': disabled,
        'variations': d.get('variations'),
    }


def power_from_stats(stats, hp=HP_MAX, attr_idx=None, attr_pct=0.0, all_pct=0.0):
    """三项参数 → 总战力区间（含生命值折算与变项加成）

    实际口径实现在 `m7_assign.stats_power`（本函数是它的索引式包装，
    供既有调用方与测试沿用）。总战力 = 火力 + 存活性 + 机动性 ± 10（手册 §七）。
    """
    pct = {}
    if attr_idx is not None and attr_pct:
        pct[ATTR_NAMES[attr_idx]] = attr_pct
    return stats_power(stats, hp, pct, all_pct)[1]


def build_pool(fleet, missing=None, damaged=None):
    """卡池 = 全部卡 − 缺失；并按当前生命值给出有效战力

    missing: 用户标注「自己没有的卡牌」id 列表（路线文档 §三 方案二）
    damaged: {卡 id: 当前剩余格数}；未列出者默认满格（3）
    返回的卡带 hp / base_power / power(有效区间) / stats
    """
    missing = set(missing or ())
    damaged = {int(k): int(v) for k, v in (damaged or {}).items()}
    pool, skipped = [], []
    for c in fleet:
        if c['id'] in missing:
            continue
        hp = damaged.get(c['id'], int(c.get('hp', HP_MAX)))
        if hp <= 0:
            skipped.append((c['id'], c['name']))       # 生命值归零 → 不可部署
            continue
        if hp > HP_MAX:
            raise ValueError('卡 %s 生命值 %s 超出上限 %d' % (c['id'], hp, HP_MAX))
        c2 = dict(c)
        c2['hp'] = hp
        c2['base_power'] = list(c['power'])
        c2['power'] = power_from_stats(c['stats'], hp)
        pool.append(c2)
    return pool, skipped


def advance_hp(pool_cards, used_ids):
    """跨阶段生命值累减：参战过的卡 −1 格（手册第六节：每参战一次损一格）

    返回新的 {卡 id: 剩余格数}，供下一阶段作为 damaged 输入。
    """
    out = {}
    for c in pool_cards:
        v = int(c.get('hp', HP_MAX)) - (1 if c['id'] in set(used_ids) else 0)
        out[c['id']] = max(0, v)
    return out


# ============ 2. 敌方编成（拼装识别结果） ============
def apply_banned_types(ban_icon, name, banned):
    """玩家输入 banned_types 覆盖图标识别结果（键=截图名；空列表=显式无禁用）

    返回 (舰种列表, 是否来自玩家输入)；未命中（name 不在 banned 里）→
    (list(ban_icon or []), False)。空列表 `[]` 是**显式「无禁用」** ——
    同样覆盖图标识别，用于否决误判。
    """
    banned = banned or {}
    if name in banned:
        vals = banned[name]
        if vals is None:
            vals = []
        elif isinstance(vals, str):
            vals = [vals]
        return [str(x) for x in vals if x], True
    return list(ban_icon or []), False


def resolve_disabled(cond, dis_in):
    """「区域 · 禁用变项」→ (该据点被禁用的变项名单, 人工确认说明 or None)

    用户 2026-10-12 裁决（面板只写「禁用变项」四个字，禁哪几门在截图上与
    可用槽**视觉一致**、看不出来）：
      · 玩家给了名单（`disabled_variations`）→ **以玩家为准**，覆盖面板结论；
        空列表 = 显式「不禁用」，用于豁免全禁用；
      · 未给 + 面板有「禁用变项」+ 作用域为区域/未读出（判据同
        `_scope_region`）→ **该据点全部变项不可用**（11 门全禁，含
        「效率提升」）—— 宁保守不乐观（旧口径按不禁用处理，可能推荐
        装不上的方案）；
      · 未给 + 作用域为带入/指定/全场 → 按既有口径不生效（不禁用），
        只留人工确认提示；
      · 未给 + 面板没有「禁用变项」→ 空名单、无提示。
    名单落到 `node['disabled']`，选阵（`std_var_attrs_for`）与变项分配
    （`plan_variations`）共用。
    """
    if dis_in is not None:
        vals = [dis_in] if isinstance(dis_in, str) else list(dis_in)
        bad = [str(x) for x in vals if str(x) not in ALL_VAR_NAMES]
        note = ('禁用变项里有未知名字被忽略：%s' % '、'.join(bad)) if bad else None
        return [str(x) for x in vals if str(x) in ALL_VAR_NAMES], note
    if not cond.get('ban_item'):
        return [], None
    if _scope_region(cond.get('scope')):
        return list(ALL_VAR_NAMES), (
            '面板有「禁用变项」（区域）—— 已按**该据点全部变项不可用**规划；'
            '若实际只禁部分，请在 player_input.json 的 disabled_variations '
            '里填名单覆盖')
    return [], ('面板有「禁用变项」条件，但未填写具体变项'
                '（`disabled_variations`）—— 规划时按不禁用处理，'
                '如需精确请在「编写输入」页补上')


def parse_node(name, path, ocr=True, need_weak=True, banned=None,
               disabled=None):
    """把四种识别结果拼成一个『据点』对象

    m4_pipeline → 每张敌方卡的战力 / 代币数值 / 代币类型 / 底色档位
    m8b_weak    → 每张卡的弱点类型（英文 key，含待确认标记）
    m9_condition→ 阶段号 / 据点号 / 难度 / 战斗条件 / 禁用变项
    m10_bar     → 底部变项库存 {槽位: 数量}

    banned:   {截图名: [舰种]} —— 玩家输入**覆盖**图标识别结果
              （`banned_types` 字段；见 `apply_banned_types`）。
    disabled: {截图名: [变项名]} —— 区域条件「禁用变项」禁用的具体变项。
              面板只写「禁用变项」四个字、不说禁用哪一个，且禁用状态在
              截图上与可用槽**视觉一致**，故只能由玩家按截图填写
              （`disabled_variations` 字段）。**未填写时**（用户 2026-10-12
              裁决）：作用域为区域 / 未读出 → 按**该据点全部变项不可用**
              处理（11 门全禁，含「效率提升」，宁保守不乐观）；
              作用域为带入/指定/全场 → 按既有口径不生效（不禁用 +
              人工确认提示）。玩家给了名单则一律**以玩家为准**覆盖面板
              结论（空列表 = 显式「不禁用」，用于豁免）。

    注：弱点用 `m8b_weak.read_screen`（标注集 39/39 的那套：裁出敌方卡牌行
    + bbox 归一化 + 只取最大连通域），**不用**旧 `m8_weak` ——
    后者在低分辨率整屏图上会把「大口径」系统性误判成「烟幕」。
    """
    import m4_pipeline
    import m9_condition
    import m10_bar

    recs = m4_pipeline.parse(path)
    cards = [{'power': r['power'], 'token': r['token'],
              'coin': r['token_type'], 'tier': r['tier']} for r in recs]
    review = []
    for i, r in enumerate(recs):
        if r['power'] is None:
            review.append('敌方%d 战力未读出 —— 多为弹窗遮挡卡头'
                          '（如「保存成功」提示）或界面不支持，请重截' % (i + 1))
        if r['token'] is not None and r['token_low']:
            review.append('敌方%d 代币读数低置信：%s' % (i + 1, r['token']))

    weak, weak_ordered = [], []
    if need_weak:
        import m8b_weak
        m8res = m8b_weak.read_screen(path)
        for i in range(len(cards)):
            ws, order = set(), []
            if i < len(m8res):
                for it in m8res[i]['icons']:
                    if it.get('type'):
                        ws.add(it['type'])
                        order.append(it['type'])
                    if it.get('needs_review'):
                        review.append('弱点 敌方%d 待人工确认' % (i + 1))
            weak.append(ws)
            weak_ordered.append(order)
    else:
        weak = [set() for _ in cards]
        weak_ordered = [[] for _ in cards]

    cond = m9_condition.parse(path, ocr=ocr) if ocr else m9_condition.parse(
        path, ocr=False)
    bar = m10_bar.parse(path)

    # 禁用舰种：玩家输入覆盖图标识别结果（就地改 cond['ban_types']，
    # 后续 node['cond']、cond_ban_types()、plan_to_json 全部自动生效）。
    ban_icon = list(cond.get('ban_types') or [])
    ban_final, ban_from_input = apply_banned_types(ban_icon, name, banned)
    if ban_from_input and ban_final != ban_icon:
        review.append('禁用舰种按玩家输入覆盖图标识别：图标=%s → 输入=%s'
                      % ('、'.join(ban_icon) or '无', '、'.join(ban_final) or '无'))
        # 图标结果已被否决，剔除 m9 的「建议抽检一次」条目（留着会误导）
        cond['needs_review'] = [x for x in (cond.get('needs_review') or [])
                                if not str(x).startswith('禁用舰种来自图标匹配')]
    cond['ban_types'] = ban_final
    if ban_from_input:
        # 玩家输入是**整份名单**（不是增补）：显示也要跟着走 —— 否则
        # `ban_text` 仍照 ban_rows 念面板认出的那几行，与生效列表自相矛盾
        cond['ban_rows'] = ([{'scope': cond.get('ban_scope'),
                              'types': list(ban_final)}]
                            if ban_final else [])

    # 「禁用变项」（区域条件，用户 2026-10-12 裁决）→ `resolve_disabled`
    # （单一实现，可单测；详见该函数的口径说明）。
    dis_in = (disabled or {}).get(name)
    dis_final, dis_note = resolve_disabled(cond, dis_in)
    if dis_note:
        review.append(dis_note)

    powers = [c['power'] for c in cards]
    # 只认「进度代币」，判据是**圆牌是否为金色**（m6_coin 的饱和度判据）。
    # ⚠ 不能用数量级：经验值/硬币等掉落物同样在 1k~2k（用户确认），
    #   例如 11.png 的「硬币柱 1600/2000/2400」就不是进度代币。
    prog_idx = [i for i, c in enumerate(cards) if c['coin'] == 'progress']
    prog = sum(cards[i]['token'] or 0 for i in prog_idx)

    n = {
        'name': name,
        'cards': cards,
        'powers': powers,
        'weak': weak,
        'weak_list': weak_ordered,
        'progress_tokens': prog,
        'progress_cards': [i + 1 for i in prog_idx],
        'is_progress': bool(prog_idx),
        'difficulty': sum(p or 0 for p in powers),
        'stage': cond.get('stage'),
        'node': cond.get('node'),
        'difficulty_label': cond.get('difficulty'),
        'star': cond.get('star_max'),
        # 战斗条件字段：**黑名单**而非白名单 —— 白名单会把识别层新增的条件
        # 字段静默丢掉（4.28：`ban_scope` 就是这么被丢的，导致分区归属的修复
        # 在计划链路里完全不生效）。只剔掉「面板级 / 非条件」的字段；
        # plan/v1 的输出字段另由 `plan_to_json` 固定枚举，不受此处影响。
        'cond': {k: v for k, v in cond.items()
                 if k not in ('file', 'has_seal', 'seal_fill', 'seal_evidence',
                              'panel_h', 'cond_text', 'items', 'needs_review',
                              'stage', 'node', 'difficulty', 'star_max',
                              'stars_all', 'goal', 'locked')},
        'disabled': dis_final,          # 该据点被禁用的变项名（玩家输入）
        'inventory': bar.get('inventory', {}),
        'has_bar': bar.get('has_bar', False),
        'mtime': os.path.getmtime(path),
        'review': review + list(cond.get('needs_review') or []),
    }
    # 「敌方战力 ±X%」：面板给的是**含「带入」加成**的值，战斗用基础值
    # （2026-10-13 实拍对照，见 enemy_pct_of 的口径说明）。
    shown, battle = cond_enemy_pct(n)
    if shown or battle:
        n['powers_displayed'] = list(powers)          # 面板原值（绿字）
        n['powers'] = [None if p is None else
                       int(round(p / (1.0 + shown) * (1.0 + battle)))
                       for p in powers]
        n['enemy_pct'] = {'displayed': round(shown, 4), 'battle': round(battle, 4)}
        n['difficulty'] = sum(p or 0 for p in n['powers'])
    return n


# ============ 3. 变项身份映射（用户配置） ============
def load_var_config(path=None):
    p = Path(path) if path else DATA / 'var_config.json'
    if not Path(p).exists():
        return {}, 'UNCONFIGURED'
    d = json.loads(Path(p).read_text(encoding='utf-8'))
    names = {}
    for k, v in (d.get('slot_names') or {}).items():
        if v:
            names[int(k)] = v
    return names, ((d.get('_meta') or {}).get('status') or 'UNKNOWN')


def inventory_by_name(inventory_by_slot, slot_names):
    """{槽位: 数量} + {槽位: 变项名} → {变项名: 数量}"""
    out = {}
    for slot, cnt in (inventory_by_slot or {}).items():
        nm = slot_names.get(int(slot))
        if nm and cnt is not None:
            out[nm] = out.get(nm, 0) + int(cnt)
    return out


def normalize_variations(var, slot_names):
    """玩家输入的变项库存 → {变项名: 数量}（**只收显式给过的项**）

    写法（`null` / 缺项 = 未指定，跳过）：
      · 11 个元素的列表 —— 按 `data/var_config.json` 的**槽位顺序**
        （与截图工具栏自左至右一致），元素为 `null` 或 ≥0 的整数
      · `{变项名: 数量}` 或 `{槽位号: 数量}`，值为 `null` 或 ≥0 的整数

    **语义**（用户 2026-10-12）：未指定的**标准变项 = 无上限**（商店可买，
    见 `var_stock_ok`）；显式 `0` = 本轮不可用、`N` = 最多 N 个；
    特殊变项（关卡掉落）未指定 = 0。故本函数**保留 0**（与旧版"忽略 0"不同）。
    """
    out = {}
    if var is None:
        return out
    if isinstance(var, (list, tuple)):
        for i, v in enumerate(var):
            nm = slot_names.get(i)
            if nm and v is not None:
                out[nm] = int(v)
        return out
    if isinstance(var, dict):
        for k, v in var.items():
            if v is None:
                continue
            nm = None
            if isinstance(k, int) or (isinstance(k, str) and k.strip().isdigit()):
                nm = slot_names.get(int(k))
            elif k in ALL_VAR_NAMES:
                nm = k
            if nm:
                out[nm] = int(v)
    return out


# 变项库存来源（`stage_inventory` 的第二个返回值）
INV_SRC_BAR = '截图工具栏'
INV_SRC_INPUT = '玩家输入variations'
INV_SRC_NONE = '无'


def stage_inventory(nodes, inp, slot_names):
    """阶段变项库存 → ( {变项名: 数量}, 来源说明 )

    截图工具栏优先；本阶段**所有**截图都无工具栏时，用玩家输入
    `variations`（`normalize_variations`）替代；两者都无 → 空库存。
    来源说明：INV_SRC_BAR / INV_SRC_INPUT / INV_SRC_NONE。
    nodes 可能缺 'has_bar'/'inventory' 键（合成/旧节点）→ 一律 .get 防御。
    """
    nodes = nodes or []
    if any(n.get('has_bar') for n in nodes):
        inv_slot = {}
        for n in nodes:
            for k, v in (n.get('inventory') or {}).items():
                inv_slot[int(k)] = v
        return inventory_by_name(inv_slot, slot_names), INV_SRC_BAR
    var = normalize_variations((inp or {}).get('variations'), slot_names)
    if var:
        return var, INV_SRC_INPUT
    return {}, INV_SRC_NONE


def inv_source_note(src, nodes):
    """变项库存来源 → 提示文本（插在变项分配 notes 前）；无噪音时 None

    来源=截图工具栏 → 静默；来源=玩家 variations → 说明；
    两者皆无且存在无工具栏截图 → 告警（效率提升以外的变项都装不上）。
    """
    if src == INV_SRC_INPUT:
        return '变项库存来源：玩家输入 variations（本阶段截图无工具栏）'
    if src == INV_SRC_NONE and any(not n.get('has_bar') for n in (nodes or [])):
        return ('⚠ 无变项库存（截图无工具栏且未填 variations）'
                '——「效率提升」以外的变项将按无库存处理，达标率可能被低估')
    return None


# ============ 4. 单据点求解 ============
def _enemy(node):
    return {'powers': list(node['powers']), 'weak': [set(w) for w in node['weak']]}


def _scope_region(scope):
    """作用域闸门：区域 / 未读出 → 折算进本据点

    用户口径（2026-10-11）：目标是全胜 —— 全胜后条件被移除，
    **带入 / 指定 / 全场条件不会积累到我方头上**，一律忽略；
    只有「区域」（永久、仅本据点）参与计算。
    scope 没读出来时按保守处理（参与计算）。
    唯一实现 —— `_cond_active` 与 `parse_node` 的「禁用变项」判据共用。
    """
    return scope in (None, '区域')


def _cond_active(node):
    """该据点的量化条件（属性/战力/禁用舰种/增益效果）是否需要建模"""
    return _scope_region((node.get('cond') or {}).get('scope'))


def cond_pct(node):
    """战斗条件的量化口径 → {属性名: 比例}（负值 = 削弱我方该参数）

    来源 `node['cond']` = m9_condition 读出的 {attr, pct, scope, ...}
    （如「区域 · 存活性 −24%」→ {'存活性': -0.24}）。
    「战力 −X%」不在其中 —— 它作用于**总战力**，见 `cond_all_pct()`。

    多行削弱：优先读 `our_pairs`（一个分节可挂多条「属性 −X%」，逐条
    按 pair 自己的 scope 过闸 —— 只算 区域 / 未读出）；pairs 缺失时
    回退单值路径（attr/pct + `_cond_active` 节点级闸门）。

    合并规则：`our_pairs` 只由 1x OCR 行配对生成，而单值 attr/pct 取自
    1x + 2x 重读的合并文本（ctext）。某条区域属性行 **1x 漏读、2x 重读
    救回** 时 pairs 里没有它，则把单值并入（按 `_cond_active` 过节点级闸门）。

    ⚠ **只在该属性「根本不在这批 pairs 里」时才并入**（`covered` 判据）。
    不能只看「不在过闸结果里」：单值本身可能是一次**错配** —— m9 的
    `attr` 取 ctext 里第一个属性名、`scope` 取第一个作用域名，两者可能来自
    **不同分节**（实测据点13：「带入：存活性 −12%…｜区域：火力 −24%」被错配成
    「区域 · 存活性 −12%」）。此时 pairs 里已有 存活性(带入)，若还按
    「不在过闸结果里」并入，就会把**带入条件当区域折算**、模型凭空变严。
    """
    c = node.get('cond') or {}
    pairs = c.get('our_pairs') or []
    if pairs:
        covered = {pr.get('attr') for pr in pairs}
        out = {}
        for pr in pairs:
            if pr.get('scope') not in (None, '区域'):
                continue
            a, p = pr.get('attr'), pr.get('pct')
            if a in ATTR_NAMES and p:
                out[a] = out.get(a, 0.0) + p / 100.0
        # 2x 重读救回的单值：pairs 里完全没有这个属性时才并入（防漏折算）
        if _cond_active(node):
            a, p = c.get('attr'), c.get('pct')
            if a in ATTR_NAMES and p and a not in covered:
                out[a] = p / 100.0
        return out
    if not _cond_active(node):
        return {}
    a, p = c.get('attr'), c.get('pct')
    if not a or not p or a not in ATTR_NAMES:
        return {}
    return {a: p / 100.0}


def cond_all_pct(node):
    """「战力 −X%」类条件 → 全参数削弱比例（负值）

    作用在逐项参数再求和，与总战力乘 (1+X%) 同效（stats_power 口径）。
    仅「区域」条件生效。

    多行配对优先（4.28）：扫 `our_pairs` 里所有「战力 −X%」按各自 scope
    过闸求和 —— 与属性/增益/减益同一条通道。光看节点级 `_cond_active`
    会把**带入段的战力削弱**当区域折进去（一个据点里可同时有带入/区域两段）。
    """
    c = node.get('cond') or {}
    pairs = c.get('our_pairs') or []
    if pairs:
        tot, seen = 0.0, False
        for pr in pairs:
            if pr.get('attr') != '战力' or not pr.get('pct'):
                continue
            seen = True
            if pr.get('scope') in (None, '区域'):
                tot += pr['pct'] / 100.0
        if seen:
            return tot
    if not _cond_active(node):
        return 0.0
    if c.get('attr') == '战力' and c.get('pct'):
        return c['pct'] / 100.0
    return 0.0


# 敌方侧属性名（与 m9_condition.ENEMY_ATTR 同名；此处写字面量，
# 让 m11 不硬依赖识别层模块）
ENEMY_ATTR = '敌方战力'


def enemy_pct_of(cond):
    """「敌方战力 ±X%」条件 → (面板显示比例, 战斗生效比例)

    与 `cond_all_pct` 严格区分：`cond_all_pct` 处理的是**我方**的
    「战力 −X%」（我方总战力被削）；「敌方战力 ±X%」是**敌方侧**加成，
    面板上是另一行（`m9_condition.ENEMY_ATTR`）。旧实现只按子串匹配，
    把后者读成「战力」并走我方通道 —— 一旦它落在区域段，我方参数会
    凭空虚增 8%。2026-10-13 拆开。

    **面板值 ≠ 战斗值**（1945 行动 13 张截图实拍 + 部署界面对照得出）：

      · 据点5 把「敌方战力 +8%」挂在**全域**段 —— 其自身面板战力求为
        黑色（= 基础值），与该据点部署界面逐格吻合 → 全域段不改显示；
      · 据点8~13 该条件挂在**带入**段，面板战力为**绿色**（= 已加成），
        而部署界面上的数字正是 面板值 ÷ 1.08（据点8/9 共 8 个数字逐一吻合，
        且减益步长仍是干净的 −20%：165×0.6=99、140×0.8=112…）→
        「带入」的敌方加成只进了**地图/面板展示**，**战斗用未加成的基础值**。

    口径（用户 2026-10-13 指定「读带入战斗条件里的敌方战力加成反推初始值」）：

        base      = 面板值 ÷ (1 + Σ带入)      # 反推出的初始（战斗）战力
        battle    = base × (1 + Σ区域)         # 区域=永久，参战生效

    返回 (displayed, battle) 两个比例（如 0.08 / 0.0）。
    ⚠ 未知项：若「敌方战力」挂在**区域**段（目前三套数据与 1945 行动
    都没有这种样例），这里按「区域 = 永久生效」推算为战斗加成；
    若日后实测表明区域加成也进了面板显示，改这一处即可。
    """
    cond = cond or {}
    pairs = cond.get('our_pairs') or []
    shown = battle = 0.0
    seen = False
    for pr in pairs:
        if pr.get('attr') != ENEMY_ATTR or not pr.get('pct'):
            continue
        seen = True
        if pr.get('scope') == '带入':
            shown += pr['pct'] / 100.0
        if pr.get('scope') in (None, '区域'):
            battle += pr['pct'] / 100.0
    if not seen and cond.get('attr') == ENEMY_ATTR and cond.get('pct'):
        sc = cond.get('scope')
        if sc == '带入':
            shown += cond['pct'] / 100.0
        if sc in (None, '区域'):
            battle += cond['pct'] / 100.0
    return shown, battle


def cond_enemy_pct(node):
    """节点级入口：见 `enemy_pct_of`（取 node['cond']）"""
    return enemy_pct_of(node.get('cond') or {})


def cond_ban_types(node):
    """「禁用舰种」类条件 → 不可 deploy 的舰种列表（如 ['巡洋舰']）

    仅「区域」条件生效。禁用条目的**图标没有文字**，一个据点可有**多行**
    （带入/区域/指定各一行），归属由 m9 的 `ban_rows` 逐行给出（每行 = 它
    **上方最近的分区标题**）。

    ⚠ 4.28：原先只看节点级 `_cond_active` —— 一个据点里同时有「带入」与
    「区域」两段时，节点级 scope 会算成区域，把**带入段的禁用**错误生效
    （实测 mygame_s4 关底 `00-18-13` 的禁用挂在带入段却被生效）。
    ⚠ 2026-10-13：原先只读**第一行**（m9 取首个「单位类型」锚点）—— 那份
    实拍的第一行恰好是带入段，于是**区域段的禁用根本没被看过**（玩家反馈
    「区域禁了战列舰和驱逐舰，程序照样上驱逐舰」）。现在逐行判归属。
    """
    c = node.get('cond') or {}
    rows = c.get('ban_rows')
    if rows:
        out = set()
        for r in rows:
            sc = r.get('scope')
            ok = _scope_region(sc) if sc is not None else _cond_active(node)
            if ok:
                out.update(r.get('types') or [])
        return sorted(out)
    # 旧数据（plan.json 里没有 ban_rows）：退回单值 + 节点级闸门
    if not c.get('ban_types'):
        return []
    sc = c.get('ban_scope')
    ok = _scope_region(sc) if sc is not None else _cond_active(node)
    return list(c['ban_types']) if ok else []


def ban_text(c):
    """「禁用舰种」的显示文本：多行条目**逐行列出**并标注是否计入

    只显示生效的那部分，玩家会以为另一行读漏了；只显示全部又不说明哪行拦人
    —— 两者都不行，所以逐行写清「（X 段）+ 是否计入」。
    """
    rows = c.get('ban_rows')
    if not rows:
        bs = c.get('ban_scope')
        return ('禁用' + '、'.join(c.get('ban_types') or [])
                + ('（%s 段）' % bs if bs else ''))
    segs = []
    for r in rows:
        tps = r.get('types') or []
        lbl = '（%s 段）' % r['scope'] if r.get('scope') else ''
        if not tps:
            segs.append('禁用舰种未识别出' + lbl)
        else:
            segs.append('禁用' + '、'.join(tps) + lbl
                        + ('' if r.get('scope') in (None, '区域')
                           else '，不生效'))
    return '｜'.join(segs)


def cond_buff_suppress(node):
    """「增益效果 −X%」类条件 → 我方增益折算系数的增量（负值）

    「增益效果 −100%」= 该据点上我方所有增益（绿色相邻 +30%）完全失效：
    有效增益 = 基准 × (1 + X/100)。仅「区域」条件生效（`_cond_active`）。

    多行配对优先（4.21 #2）：扫描 `our_pairs` 里所有「增益效果 −X%」按各自
    scope 过闸（只算 区域 / 未读出）求和；配对里出现过该属性就不再退回单值
    —— 单值可能是一次跨节错配（如「区域·增益效果 −100%」与「全域·火力 −12%」
    错配成「火力 −100%」，既漏折增益、又折出幽灵削弱）。
    """
    c = node.get('cond') or {}
    pairs = c.get('our_pairs') or []
    if pairs:
        tot, seen = 0.0, False
        for pr in pairs:
            if pr.get('attr') != '增益效果' or not pr.get('pct'):
                continue
            seen = True
            if pr.get('scope') in (None, '区域'):
                tot += pr['pct'] / 100.0
        if seen:
            return tot
    if not _cond_active(node):
        return 0.0
    if c.get('attr') == '增益效果' and c.get('pct'):
        return c['pct'] / 100.0
    return 0.0


def cond_debuff_scale(node):
    """「减益效果 −X%」类条件 → 我方减益强度系数（如 −50% → 0.5）

    规则手册 §十二把「减益」定义为**我方针对敌方弱点**的手段（单次 −20%、
    同一弱点最多 3 次 → −60%）。面板「区域 · 减益效果 −50%」出现在战斗条件
    列表里，与「火力 −24%」「增益效果 −100%」并列（均为对我方不利），故口径
    为：**我方发出的减益强度按 (1 + X/100) 折算**（−50% → 单次 −20% 变 −10%）。

    多行配对优先（同 cond_buff_suppress）：扫 `our_pairs` 里所有「减益效果 −X%」
    按各自 scope 过闸求和；仅「区域」/未读出生效。
    """
    c = node.get('cond') or {}
    pairs = c.get('our_pairs') or []
    if pairs:
        tot, seen = 0.0, False
        for pr in pairs:
            if pr.get('attr') != '减益效果' or not pr.get('pct'):
                continue
            seen = True
            if pr.get('scope') in (None, '区域'):
                tot += pr['pct'] / 100.0
        if seen:
            return tot
    if not _cond_active(node):
        return 0.0
    if c.get('attr') == '减益效果' and c.get('pct'):
        return c['pct'] / 100.0
    return 0.0


def has_cond(node):
    """该据点是否有「量化影响」的战斗条件（指令类不算）"""
    return bool(cond_pct(node)) or bool(cond_all_pct(node))         or bool(cond_buff_suppress(node)) or bool(cond_debuff_scale(node))


def eval_cards(cards, node, debuff_x2=False, teamwork=False,
               attr_idx=None, attr_pct=0.0, all_pct=0.0,
               disable_effects=False, ignore_conditions=False,
               std_by=None, spc_by=None):
    """给定我方卡序列（按位置 0..3）→ (stats, {位置: 胜率})

    加成一律交给 `team_stats` 按「逐项参数」计算（见 m7_assign.stats_power）。

    战斗条件（`node['cond']`，如存活性 −24%）折算进我方参数后再算胜率
    （修路线文档 4.12 第一条）。同一属性上，条件与属性变项**加算**
    （−24% + 20% = −4%），与 stats_power 的加成约定一致。

    `std_by` / `spc_by`：{位置: 变项名} —— **每单位**变项装备（规则卡 16：
    变项只影响应用它的单位）。位置从 0 计。
    `ignore_conditions`：装备「势不可挡」时无视战斗条件。
    """
    spc_by = spc_by or {}
    std_by = std_by or {}
    if any(v == '势不可挡' for v in spc_by.values()):
        ignore_conditions = True
    pct = {} if ignore_conditions else cond_pct(node)
    if attr_idx is not None and attr_pct:
        nm = ATTR_NAMES[attr_idx]
        pct[nm] = pct.get(nm, 0.0) + attr_pct
    # 「战力 −X%」条件 + 「心无旁骛」全参数加成：同一通道加算
    all_total = all_pct if ignore_conditions else \
        all_pct + cond_all_pct(node)
    # 「增益效果 −X%」条件：我方增益按系数折算（势不可挡无视之）
    buff_factor = 1.0 if ignore_conditions else \
        1.0 + cond_buff_suppress(node)
    # 「减益效果 −X%」条件：我方减益强度按系数折算（势不可挡无视之）
    debuff_scale = 1.0 if ignore_conditions else \
        1.0 + cond_debuff_scale(node)

    # 每单位变项展开（规则卡 16：变项只影响应用它的单位）
    pos_attr, teamwork_pos, x2_pos = {}, set(), set()
    nomatch_pos, all_pct_pos, repairs = set(), {}, {}
    for pos, nm in std_by.items():
        act = (STD_VAR.get(nm) or {}).get('act')
        if act == 'attr':
            key = ATTR_NAMES[STD_VAR[nm]['stat']]
            pos_attr.setdefault(pos, {})
            pos_attr[pos][key] = pos_attr[pos].get(key, 0.0) + ATTR_PCT
        elif act == 'teamwork':
            teamwork_pos.add(pos)
    for pos, nm in spc_by.items():
        act = (SPC_VAR.get(nm) or {}).get('act')
        if act == 'all_param':
            all_pct_pos[pos] = all_pct_pos.get(pos, 0.0) + ALL_PARAM_PCT
            disable_effects = True          # 心无旁骛：停用所有增益与减益
        elif act == 'debuff_x2':
            x2_pos.add(pos)
        elif act == 'debuff_nomatch':
            nomatch_pos.add(pos)
        elif act == 'repair_hp':
            c = cards[pos]
            repairs[pos] = min(HP_MAX, int(c.get('hp', HP_MAX)) + 2)

    if repairs:
        cards = [dict(c, hp=repairs.get(i, c.get('hp', HP_MAX)))
                 for i, c in enumerate(cards)]

    st = team_stats(list(cards), list(range(len(cards))),
                    _enemy(node)['weak'], _enemy(node)['powers'],
                    debuff_x2=debuff_x2, teamwork=teamwork,
                    attr_pct=pct, all_pct=all_total,
                    disable_effects=disable_effects,
                    pos_attr_pct=pos_attr or None,
                    all_pct_pos=all_pct_pos or None,
                    teamwork_pos=teamwork_pos or None,
                    debuff_x2_pos=x2_pos or None,
                    nomatch_pos=nomatch_pos or None,
                    buff_factor=buff_factor,
                    debuff_scale=debuff_scale)
    return st, slot_win_rates(st)


def min_rate(wr):
    return min(wr.values()) if wr else 0.0


def _debuff_scenario_kind(card, pos, weak):
    """该单位（位置 pos）的减益逐个看：有**匹配命中** / 有**打不到的**

    返回 (live, dead)：live = 至少一个减益类型命中目标敌方弱点；
    dead = 至少一个方向上有敌方、但类型不匹配（老水手能救回）。
    """
    live = dead = False
    nb = neighbors(pos)
    for d in card.get('debuff') or []:
        j = nb.get(d['dir'])
        if j is None:
            continue
        t = to_en(d['type'])
        if t is None:
            continue
        ws = weak[j] if j < len(weak) else set()
        if t in ws:
            live = True
        else:
            dead = True
    return live, dead


def _scenario_seed(cards, node, x2_all, nomatch_all, inv, dis):
    """乐观减益场景的假设 → 每单位特殊变项「救场种子」{位置: 变项名}

    每个单位只有一个特殊栏位，故按场景**二选一**（老水手 / 特化训练）：
      · 该单位有**打不到**的减益（dead）→ 老水手（把死减益救活）；
      · 其余有匹配命中的单位 → 特化训练（把命中 ×2）。
    这是对场景假设（全位置老水手 / 全位置特化）**可实装**的还原 ——
    写进解（`seed_spc`）后由 `plan_variations` 先实装再走常规贪心，
    最后仍以真实评估器为准（库存按 `var_stock_ok` 截断，禁用不种）。
    """
    weak = [set(w) for w in (node.get('weak') or [])]
    seed = {}
    for pos, c in enumerate(cards):
        live, dead = _debuff_scenario_kind(c, pos, weak)
        if nomatch_all and dead and var_stock_ok(inv, '老水手') \
                and '老水手' not in dis:
            seed[pos] = '老水手'
        elif x2_all and live and var_stock_ok(inv, '特化训练') \
                and '特化训练' not in dis:
            seed[pos] = '特化训练'
    return seed


def _install_variations(cards, node, inv, target, seed_spc=None,
                        allow_drop=True, trim=False):
    """用**同一个** `plan_variations` 给一套阵容做按船变项装配（单一实现）

    返回装配完的计划条目 `p`（含 stats/win_rates/equip/std/spc/repairs）；
    **未达标 → None** —— 诚实性：只有真实评估器确认达标的解才被采用。
    `search_node.validate` 与 `strong_perm_attack` 共用它（严禁第二份装配
    实现，用户 2026-10-12 强调「单一实现，别复制粘贴」）。
    `uses_max=None`（不限次数）与 validate 的老口径一致 —— 阶段级
    `plan_variations` 才是最终仲裁（同阶段库存可能被其他据点分走）。

    `allow_drop=False` → 关掉 plan_variations 第 5 步（「效率提升」最后手段）。
    **搜索阶段的验证用 False**：卸掉「效率提升」会丢进度代币收益（P1），
    必须等「场景式重搜 / 排列攻坚」把「保留效率提升的解」找完才轮到它
    （实测：s4 BOSS 用 True 时会抢先接受一个放弃效率提升的强阵解，
    把「4×特化训练 + 效率提升」的救场解挤掉，白丢 10% 进度代币）。

    `trim=False`（默认）→ 关掉第 6 步（收官修剪）。**搜索阶段一律不修剪**
    —— 修剪是**阶段级收官**步骤（最终以阶段级 `plan_variations` 为准），
    在搜索里修剪会把「已剪掉冗余的标准变项」的装备表喂给
    `local_search` 的口径（它按 `p['equip']` 评估），改变跨据点交换的
    目标函数 → **实际换卡选择跟着变**（实测 data/stage：boss/n2/n5 的
    阵容与等级治理结果全被带偏）。搜索阶段的职责只是「选阵 + 验证达标」，
    与第 5 步同理：收官步骤不在搜索里抢先生效。
    """
    p = {'cards': list(cards), 'nodes': node}
    if seed_spc:
        p['seed_spc'] = dict(seed_spc)
    plan_variations([p], dict(inv or {}), None, target=target,
                    drop_efficiency=allow_drop, trim=trim)
    return p if p.get('meets_target') else None


def _any_var_installable(node, inv):
    """该据点现在还有可装的变项吗（强阵排列攻坚的前置闸门）

    全禁用的据点 / 库存全空且标准变项显式 0 —— 排列攻坚必然白跑
    （纯无变项的更优阵容轮不到它：裸搜的 0.0 档已经取了该模型下的最优）。
    """
    dis = node.get('disabled') or []
    if std_var_attrs_for(node, inv):
        return True
    if var_stock_ok(inv, '团队合作') and '团队合作' not in dis:
        return True
    return any(var_stock_ok(inv, nm) and nm not in dis for nm in SPC_VAR)


def search_node(pool, node, target=TARGET_RATE, debuff_x2=False, teamwork=False,
                inv=None):
    """为一个据点找编队。先试目标胜率，退化到「最优可行」并标注

    战斗条件（属性类）在**搜索阶段**就折算进每个候选编队的评估
    （否则搜索按无条件选阵，变项阶段才发现不达标，推荐会系统性偏乐观）。
    「禁用舰种」条件在搜索前就把被禁的卡剔除（剔除后不足 4 张 → None）。

    `inv`：本阶段变项库存（`stage_inventory` 的产物）。给了就在搜索里按
    `std_var_attrs_for` 把「每单位一门标准属性变项（+20%）」算进去
    （见 m7_assign.std_variation_win_rates）—— 只有配上变项才达标的阵容
    因此能被选中；显式置 0 / 被该据点禁用时自动不建模。
    返回的解带 `std_var_attrs`（None = 未建模），供局部搜索沿用同一口径。

    **场景式重试**（`SCENARIOS_OPTIMISTIC`）：搜索选阵看不到**特殊变项**
    的救场能力（特化训练 = 减益 ×2、老水手 = 减益无需匹配弱点），会选出
    「无变项时 min 更高、但用变项也救不回来」的阵容（实测：困难 BOSS 的
    100% 解 `USS拉菲/…` 靠 3 个老水手把无匹配的减益打到敌方头上，而
    「无变项」模型下它逐位置更低、永远选不上）。故分两步：
      1. 先用当前（保守）模型搜一次，把结果交给 `plan_variations` 实装
         变项，用**真实评估器**验证是否达标 → 达标即原样返回（老行为）；
      2. 不达标时，依次用更乐观的减益假设重搜 —— 「全队特化训练 ×2」
         「全位置老水手」「两者同时」（复用 `search_assignment` 的
         `debuff_x2` / `nomatch`）——每个场景的结果先按
         `_scenario_seed` 把该场景的假设**实装成每单位特殊变项种子**
         （贪心可能因局部最优错过救场组合：实测 BOSS 的乐观最优阵容需
         「4 个老水手同时装上」才起效，逐步贪心永远走不到），再交给
         `plan_variations` + 真实评估器验证；取第一个真正达标的解，
         并把解里的 stats/win_rates/equip 换成**实装后的真实值**
         （乐观假设只用于选阵，不泄进最终结论）；
      3. 仍不达标 → **强阵排列攻坚**（`strong_perm_attack`，第二道兜底，
         用户 2026-10-12 同意借鉴队友核心）：取可用池战力 top-K（默认 12）
         的 4-排列，按总战力降序逐个用**同一个** `plan_variations` 装配，
         第一个真实达标（100%）的排列即采用（双保险：`max_perms` 2000 /
         `budget_s` 4 秒，超预算放弃、不做隐式降级）；
      4. 全都不达标 → 返回保守解并置 `meets_target=False`，报告按真实
         结果说话（诚实性）。

    成本：场景式重试与排列攻坚只在第一次不达标时发生；`inv=None`
    （无库存信息）不验证也不重试；库存里没有对应特殊变项（或被该据点
    禁用）、池里没有减益卡时直接跳过对应场景。
    """
    ban = cond_ban_types(node)
    usable = [c for c in pool if c.get('type') not in ban] if ban else pool
    if len(usable) < 4:
        return None
    std_attrs = std_var_attrs_for(node, inv)

    def run_scenario(x2_all, nomatch_all):
        """一次选阵搜索 → (解 or None, 该场景模型下是否达标)"""
        res = search_assignment(
            usable, _enemy(node), debuff_x2=debuff_x2 or x2_all,
            teamwork=teamwork, rate_levels=(0.0, target),
            attr_pct=cond_pct(node) or None,
            all_pct=cond_all_pct(node),
            buff_factor=1.0 + cond_buff_suppress(node),
            std_variation=bool(std_attrs), std_attrs=std_attrs,
            nomatch=nomatch_all)
        sol = res.get(target)
        met = sol is not None
        if sol is None:
            sol = res.get(0.0)
        if sol is None:
            return None, False
        out = dict(sol)
        out['meets_target'] = met
        out['cards'] = list(sol['perm'])
        out['nodes'] = node
        out['std_var_attrs'] = std_attrs
        return out, met

    def validate(out):
        """`plan_variations` 实装变项 + 真实评估器：达标 → 真实值并回解

        每个场景的解都要过这一关 —— 「装上变项后真的达标」才算数
        （库存按 `var_stock_ok`：特殊变项要真在库存里）。达标后把
        stats/win_rates/equip 换成实装后的**真实**值，供局部搜索沿用。
        装配走 `_install_variations`（单一实现，与排列攻坚共用）。
        """
        p = _install_variations(out['cards'], node, inv, target,
                                out.get('seed_spc'), allow_drop=False)
        if p is None:
            return False
        for k in ('stats', 'win_rates', 'equip', 'std', 'spc', 'repairs',
                  'trimmed'):
            if k in p:
                out[k] = p[k]
        out['meets_target'] = True
        return True

    base, _ = run_scenario(False, False)
    if base is None:
        return None
    if inv is None:
        return base                     # 无库存信息 → 不做场景式重试（老行为）
    if validate(base):
        return base
    base['meets_target'] = False        # 诚实：保守模型达标不了就不标达标

    # --- 1) 强阵排列攻坚（**提前**为第一道兜底；用户 2026-10-12 指示：按队友
    # 思路「强化阵容优先」——强阵容需要的变项更少（尤其特殊变项，见 4.30），
    # 因此先试强阵，再考虑「弱阵容靠特殊变项救场」。
    # 取可用池战力 top-K 的 4-排列（按总战力降序）逐个用同一个
    # `plan_variations` 装配，第一个真实达标（100%）的排列即采用；
    # 双保险超预算则放弃（如实标未达标，不谎报）。
    # 前置闸门：一门变项都装不上时必然白跑（见 `_any_var_installable`）。
    if _any_var_installable(node, inv):
        hit = strong_perm_attack(usable, node, inv, target=target,
                                 topk=PERM_TOPK, max_perms=PERM_MAX_PERMS,
                                 budget_s=PERM_BUDGET_S)
        if hit is not None:
            return hit

    # --- 2) 场景式乐观重搜（第二道兜底：弱阵容 + 特殊变项救场）---
    dis = node.get('disabled') or []
    if any(c.get('debuff') for c in usable):
        for x2_all, nomatch_all in SCENARIOS_OPTIMISTIC:
            # 场景前提：对应特殊变项真在库存里、且该据点没禁用（否则白搜）
            if x2_all and (not var_stock_ok(inv, '特化训练')
                           or '特化训练' in dis):
                continue
            if nomatch_all and (not var_stock_ok(inv, '老水手')
                                or '老水手' in dis):
                continue
            cand, _ = run_scenario(x2_all, nomatch_all)
            if cand is None:
                continue
            seed = _scenario_seed(cand['cards'], node, x2_all, nomatch_all,
                                  inv, dis)
            if seed:
                cand['seed_spc'] = seed
            if validate(cand):
                return cand
    return base


# ============ 4b. 强阵排列攻坚（失败据点的第二道兜底） ============
def strong_perm_attack(pool, node, inv, target=TARGET_RATE, topk=PERM_TOPK,
                       max_perms=PERM_MAX_PERMS, budget_s=PERM_BUDGET_S):
    """强阵排列攻坚 —— 失败据点的第二道兜底（用户 2026-10-12 同意借鉴）

    只在「保守搜索 → 计入标准变项的搜索 → 场景式乐观重搜」全部不达标后
    启用（调用点见 `search_node`）。队友核心 `search_node_variations` 的
    关键洞察：裸搜失败时 `search_assignment` 会把「战力消耗最小」的**弱卡**
    阵容选出来（它是按 0.0 档的最优可行解挑的），此后无论怎么装变项都在
    错误的底子上打补丁；正确顺序是**先假设可以装变项、再重选阵容**。

    做法（与队友一致的口径）：
      1. 取该据点可用池（已剔除禁用舰种）按**当前战力** top-K
         （默认 `PERM_TOPK`=12）—— 弱卡陪跑不进排列，枚举量从
         C(30,4)×4! ≈ 65.7 万降到 C(12,4)×4! = 11880；
      2. 4-排列按**总战力降序**枚举 —— 强阵更可能达标、**命中即停**；
      3. 每个排列用**同一个** `plan_variations` 做按船变项装配
         （`_install_variations`，单一实现，不复制粘贴）；
      4. `max_perms`（默认 2000）与 `budget_s`（默认 4 秒/据点）双保险；
         超预算 → 返回 None（放弃），调用方按不达标**如实**报告 ——
         不做隐式降级。

    **诚实性**：只有 `plan_variations` 的真实评估器确认达标
    （`meets_target=True`）的排列才被采用；解的 stats/win_rates/equip
    全是实装后的真实值，乐观假设不泄漏。`inv` 给空字典也可用
    （标准变项未给值 = 无上限）。

    返回解 dict（与 `search_node` 的解同构，带 `perm_tries` 供排查）或 None。
    """
    import itertools
    cand = sorted(pool, key=lambda c: -c['power'][1])[:int(topk)]
    if len(cand) < 4:
        return None
    perms = list(itertools.permutations(range(len(cand)), 4))
    perms.sort(key=lambda cb: -sum(cand[i]['power'][1] for i in cb))
    t0 = time.time()
    for n, combo in enumerate(perms[:int(max_perms)], 1):
        if time.time() - t0 > budget_s:
            return None                 # 超预算放弃（不谎报、不降级）
        p = _install_variations([cand[i] for i in combo], node, inv, target)
        if p is None:
            continue
        out = {'cards': list(p['cards']), 'nodes': node,
               'perm': list(p['cards']), 'meets_target': True,
               'std_var_attrs': std_var_attrs_for(node, inv),
               'perm_tries': n}
        for k in ('stats', 'win_rates', 'equip', 'std', 'spc', 'repairs',
                  'trimmed'):
            if k in p:
                out[k] = p[k]
        return out
    return None


# ============ 5. 局部搜索 ============
def _objective(plan, target):
    """总目标（按优先级）：达标据点数 → 最差胜率 → 减益命中数 → 已消耗战力最小"""
    if not plan:
        return (0, 0.0, 0, 0)
    rates = [min_rate(p['win_rates']) for p in plan]
    met = sum(1 for r in rates if r >= target - 1e-9)
    hits = sum(sum(len(v) for v in p['stats']['debuff_total'].values())
               for p in plan)
    return (met, round(min(rates), 4), hits, -_total_our(plan))


def _total_our(plan):
    return sum(sum(hi for lo, hi in p['stats']['our_eff'].values()) for p in plan)


def local_search(plan, nodes, target=TARGET_RATE, max_rounds=4,
                 debuff_x2=False, teamwork=False):
    """跨据点 pairwise 交换：任一交换若提升总目标则接受

    只交换、不新增/删除卡 —— C1（阶段内单次参战）与 C3（位置唯一）自动保持。
    评估口径与 `search_node` 一致：据点若在选阵时建模了标准属性变项
    （`p['std_var_attrs']`，见 `search_node`），这里的胜率同样按
    「每单位可装 1 门 +20%」取较高者 —— 否则「靠变项才达标」的据点会被
    误判为不达标，局部搜索会去换它的卡、把选好的阵容拆掉。
    **场景式重试的救场据点**（`search_node` 验证通过后带着已验证的
    `p['equip']`，如「老水手」这类搜索模型看不到的减益变项）更敏感：
    直接用**真实模型**（eval_cards + 实装变项）评估，绝不用保守口径
    把它当成「不达标」去拆。
    """
    if len(plan) < 2:
        return plan

    def eval_of(p, cards, st):
        """该据点在**与选阵阶段一致的口径**下的 (stats, 逐位置胜率)"""
        equip = p.get('equip') or {}
        std_by = {i: e['std'] for i, e in equip.items() if e.get('std')}
        spc_by = {i: e['spc'] for i, e in equip.items() if e.get('spc')}
        if std_by or spc_by:
            return eval_cards(cards, p['nodes'], std_by=std_by,
                              spc_by=spc_by)
        wr = slot_win_rates(st)
        attrs = p.get('std_var_attrs')
        if attrs:
            wr = std_variation_win_rates(st, attrs)
        return st, wr

    cur = _objective(plan, target)
    for _ in range(max_rounds):
        improved = False
        for a in range(len(plan)):
            for b in range(a + 1, len(plan)):
                for i in range(len(plan[a]['cards'])):
                    for j in range(len(plan[b]['cards'])):
                        ca, cb = plan[a]['cards'], plan[b]['cards']
                        if ca[i]['id'] == cb[j]['id']:
                            continue
                        # 禁用舰种守卫：换入的卡不得是目标据点被禁的类型
                        ban_a = cond_ban_types(plan[a]['nodes'])
                        ban_b = cond_ban_types(plan[b]['nodes'])
                        if cb[j].get('type') in ban_a \
                                or ca[i].get('type') in ban_b:
                            continue
                        # 等级治理守卫：受治理据点不得换入 3 级（gold）卡
                        if plan[a].get('gold_saved') and \
                                str(cb[j].get('tier') or '').lower() in GOLD_TIERS:
                            continue
                        if plan[b].get('gold_saved') and \
                                str(ca[i].get('tier') or '').lower() in GOLD_TIERS:
                            continue
                        na = list(ca)
                        na[i] = cb[j]
                        nb = list(cb)
                        nb[j] = ca[i]
                        sa, _wa0 = eval_cards(na, plan[a]['nodes'], debuff_x2,
                                              teamwork)
                        sb, _wb0 = eval_cards(nb, plan[b]['nodes'], debuff_x2,
                                              teamwork)
                        sa, wa = eval_of(plan[a], na, sa)
                        sb, wb = eval_of(plan[b], nb, sb)
                        trial = list(plan)
                        trial[a] = {**plan[a], 'cards': na, 'stats': sa,
                                    'win_rates': wa}
                        trial[b] = {**plan[b], 'cards': nb, 'stats': sb,
                                    'win_rates': wb}
                        obj = _objective(trial, target)
                        if obj > cur:
                            plan, cur, improved = trial, obj, True
        if not improved:
            break
    return plan


# ============ 6. 变项分配 ============
def plan_variations(plan, inventory, uses_max=None, target=TARGET_RATE,
                    disabled=None, drop_efficiency=True, trim=True):
    """按「必需性驱动」分配变项（开发文档 §6.2 + 规则卡 16 每单位口径）

    **规则卡 16**：每个单位位置有标准/特殊各 1 个变项栏位，变项只影响
    应用它的单位 —— 属性变项只加成装备它的那张卡、团队合作使该卡发出
    的增益 ×2、特化训练使该卡发出的减益 ×2。故分配 = 选（变项, 单位）
    的组合，贪心取收益最大者。

    优先级：
      0. 救场种子（可选）—— 计划条目带 `seed_spc`（{位置: 变项名}，由
         `search_node` 的场景式重试写入）时**先按库存实装**再走下面的
         贪心；否则「只有同时装上多个特殊变项才达标」的阵容会被逐步
         贪心错过（见 search_node 的“场景式重试”说明）
      1. 效率提升 —— 掉落进度代币的据点**必须装**（不占次级代币次数上限、
         库存视为无限）；优先装到面对掉进度代币敌方的我方位置
         （progress_cards 序号 −1），目标槽占满则退到第一个空闲标准槽
      2. 标准属性变项 —— 仅当「不装则达不到目标胜率」；每次消耗 1 次上限
      3. 团队合作 —— 同上
      4. 特殊变项 —— 最后手段；关卡掉落、不消耗次级代币次数
         （特化训练 / 老水手 / 心无旁骛 / 势不可挡 / 快速维修）
      5. **「效率提升」最后手段**（用户 2026-10-12 裁决）—— 走完上面全部
         步骤仍不达标的据点，若装着「效率提升」就**卸掉它**（释放该标准槽）
         再对该据点做一轮同样的装配（2/3/4 步）；卸掉后达标才采用并显式
         说明，仍不达标则回滚（保持强制装 + 原有告警）。默认强制不被破坏：
         装了也达标的据点一律不动它。
      6. **收官修剪（trim）**（用户 2026-10-12 裁决）—— 逐据点、逐门尝试
         卸下已装变项：卸掉后该据点**仍 4/4 达标**（真实评估器 `eval_cards`，
         与本函数末尾判定同一口径）→ 保留卸除并**退还**库存（`inv`）与
         次数（`left`，仅标准属性 / 团队合作）；否则还原。修剪的冗余来自
         两个机制：场景式救场的种子「只加不拆」（按库存全装）、单调贪心 +
         相邻耦合（先装的项被后装的项覆盖）。**不修剪**「效率提升」（进度
         代币收益）与「快速维修」（修复的 hp 跨阶段接力，收益在后续阶段）
         —— 这两类的价值不在本场胜率。顺序：先特殊变项（关卡掉落、库存
         有限，最该省）再标准属性 / 团队合作；同类别内一轮扫描即可（卸除
         不会抬高其他人的必需性 —— 「心无旁骛」的**卸下**只会把停用的
         增益/减益还回来，故它归在特殊变项里先判）。修剪结果写进 notes，
         并在 `p['trimmed'] = [(变项名, 位置0起), ...]` 留痕（测试断言用）。

    `drop_efficiency=False` 关掉第 5 步（搜索阶段的验证用，见
    `_install_variations`）—— 它会让「放弃效率提升换达标」的解在**搜索阶段
    就被接受**，抢在「场景式重搜 / 排列攻坚」前面，反而挤掉「保留效率提升」
    的救场解（实测 s4 BOSS 白丢 10% 进度代币）。默认 True = 阶段级收官口径。
    `trim=False` 关掉第 6 步（同样给搜索阶段的验证用，见
    `_install_variations`）—— 修剪是**阶段级收官**步骤；在搜索里修剪会把
    修剪后的装备表喂给 `local_search`，连带改变跨据点换卡选择（实测
    data/stage）。默认 True = 阶段级收官口径。

    `uses_max` 是**每阶段**可用次级代币购买变项的次数上限；
    **None = 不设上限（未指定，默认）**，显式数字（含 0）才作为约束；
    「效率提升」不计入。inventory / uses_left 会被就地扣减。
    返回 (inv, uses_left, notes)（uses_left = None 表示不设上限）。
    修复生命值记录在 p['repairs']
    （{卡id: 修复后起始格数}），由调用方并入跨阶段接力。
    """
    notes = []
    inv = dict(inventory or {})
    left = None if uses_max is None else int(uses_max)

    for p in plan:
        p['equip'] = {i: {'std': None, 'spc': None}
                      for i in range(len(p['cards']))}
        p['std'], p['spc'] = [], []
        p['repairs'] = {}
        p['efficiency'] = False             # 第 1 步装成功后置 True
        p['efficiency_dropped'] = False     # 第 5 步（最后手段）放弃后置 True
        p['trimmed'] = []                   # 第 6 步（收官修剪）留痕

    def node_obj(wr):
        """节点目标（字典序）：达标位置数 → 最差胜率 → 总和。
        每单位口径下单门变项只提升一个位置，min_rate 不动 ——
        必须以「达标位置数」优先才能驱动逐单位装配。"""
        return (sum(1 for v in wr.values() if v >= target - 1e-9),
                round(min_rate(wr), 4), round(sum(wr.values()), 4))

    def reeval(p):
        reeval_entry(p)
        return node_obj(p['win_rates'])

    def free_pos(p, slot):
        return [i for i, e in p['equip'].items() if e[slot] is None]

    # 起点必须先重算成「**无变项**的真实值」再逐个装 —— search_node 在
    # 开了 std_variation 时返回的 win_rates 是「带上标准属性变项」的
    # **估计值**（为了把「只有配变项才达标」的阵容选出来）。若沿用它作起点，
    # 贪心会以为已经达标、一门变项都不装，最后把估计值当成真实值输出。
    # 重算后（equip 全空）与搜索的无变项评估逐位一致，故不影响老行为。
    for p in plan:
        reeval(p)

    def do_equip(p, pos, slot, name):
        p['equip'][pos][slot] = name
        p['std' if slot == 'std' else 'spc'].append(name)
        # 只有**显式给过数量**的变项才扣减 —— 标准变项未给值 = 无上限（var_stock_ok）
        if name != '效率提升' and name in inv:
            inv[name] = inv[name] - 1
        if slot == 'std' and name != '效率提升':   # 效率提升不占次数上限
            nonlocal left
            if left is not None:    # None = 不设上限（未指定）
                left -= 1
        act = (SPC_VAR.get(name) or {}).get('act')
        if act == 'repair_hp':
            cid = p['cards'][pos]['id']
            cur = p['repairs'].get(cid, int(p['cards'][pos].get('hp', HP_MAX)))
            p['repairs'][cid] = min(HP_MAX, cur + 2)

    def banned_here(p, nm):
        """该据点是否禁用了这个变项

        来源 = `p['nodes']['disabled']`：玩家按截图填的
        `disabled_variations`，或 parse_node 在「区域 · 禁用变项」未填名单时
        默认注满的全部 11 门（2026-10-12 裁决）。
        """
        return nm in (p['nodes'].get('disabled') or [])

    # --- 0) 场景式重试的「救场种子」（可选，search_node 写入 p['seed_spc']）---
    # 场景式重试选出的阵容可能「只有把假设的特殊变项**同时**装上才达标」，
    # 逐步贪心会因局部最优错过（实测 BOSS：4 个老水手都装上才有 100%）。
    # 故先把种子（{位置: 老水手/特化训练}）按**实际库存**（var_stock_ok）
    # 与该据点禁用情况实装，再走下面的常规贪心 —— 种子只加不拆，
    # 最终达标与否仍由本函数末尾的真实评估器判定。
    for p in plan:
        seed = p.get('seed_spc') or {}
        if not seed:
            continue
        seeded = []
        for pos, nm in sorted(seed.items()):
            if not isinstance(pos, int) or pos >= len(p['cards']):
                continue
            if (SPC_VAR.get(nm) or {}).get('act') not in ('debuff_x2',
                                                          'debuff_nomatch'):
                continue        # 只接受救场类特殊变项（防止误用其他名字）
            if not var_stock_ok(inv, nm) or banned_here(p, nm):
                continue        # 库存不足（特殊变项按实际库存）/ 被禁用
            if p['equip'][pos]['spc'] is not None:
                continue        # 该单位特殊栏位已被占（每单位只有一个）
            do_equip(p, pos, 'spc', nm)
            seeded.append('位置%d「%s」' % (pos + 1, nm))
        if seeded:
            reeval(p)
            notes.append('%s 场景式重试救场（按乐观减益假设选阵，'
                         '已用真实评估器复核达标）：%s'
                         % (p['nodes']['name'], '、'.join(seeded)))

    # --- 1) 效率提升（进度代币据点必须装；不占次数上限、库存视为无限） ---
    # 定向装船：优先装到「面对掉落进度代币敌方」的我方位置 ——
    # progress_cards 是掉进度代币的敌方序号（1 起），对应我方同序号槽位
    # （0 起 = 序号 −1）；目标槽都已占用则退到第一个空闲标准槽。
    # 多个目标位置都空闲时挑**当前胜率最高**的：该位置最不需要把标准栏位
    # 留给属性变项（实测据点13：效率提升占住 57% 位置的栏位 → 炮术训练
    # 装不上去、该位置停在 57%；挪到已 100% 的位置后即达 100%）。
    prog = [p for p in plan if p['nodes']['is_progress']]
    prog.sort(key=lambda p: -p['nodes']['progress_tokens'])
    for p in prog:
        name = p['nodes']['name']
        if banned_here(p, '效率提升'):
            if set(p['nodes'].get('disabled') or []) >= ALL_VAR_NAMES:
                notes.append('⚠ %s 的「禁用变项」= **该据点全部变项不可用**'
                             '（含「效率提升」）—— 本据点装不上，'
                             '进度代币 +10%% 收益拿不到' % name)
            else:
                notes.append('⚠ %s 的「禁用变项」含**效率提升**（玩家输入）'
                             '—— 本据点装不上，进度代币 +10%% 收益拿不到'
                             % name)
            continue
        free = free_pos(p, 'std')
        targets = [int(i) - 1 for i in
                   (p['nodes'].get('progress_cards') or [])]
        cand = [i for i in targets if i in free]
        pos = max(cand, key=lambda i: (p['win_rates'].get(i, 0.0), -i)) \
            if cand else (free[0] if free else None)
        if pos is None:
            notes.append('⚠ %s 需装「效率提升」但 4 个单位的标准栏位已满'
                         % name)
            continue
        do_equip(p, pos, 'std', '效率提升')
        p['efficiency'] = True

    # --- 2~4) 逐据点装配：标准属性变项 → 团队合作 → 特殊变项 ---
    # 三个步骤各自独立成函数（**单一实现**）：第 5 步「效率提升最后手段」的
    # 重新装配也调它们，禁止第二份复制粘贴的装配逻辑。
    def greedy_attrs(p):
        """标准属性变项（按需）：每次消耗 1 个次级代币次数；
        left=None = 不设上限（未指定，默认）"""
        cur_obj = node_obj(p['win_rates'])
        full = (len(p['cards']), 1.0, round(target * len(p['cards']), 4))
        while cur_obj < full and (left is None or left > 0):
            best = None        # (obj, pos, name)
            for nm, meta in STD_VAR.items():
                if meta['act'] != 'attr' or not var_stock_ok(inv, nm) \
                        or banned_here(p, nm):
                    continue
                for pos in free_pos(p, 'std'):
                    st_c, wr = eval_cards(
                        p['cards'], p['nodes'],
                        std_by={**{i: e['std'] for i, e in
                                   p['equip'].items() if e['std']},
                                pos: nm},
                        spc_by={i: e['spc'] for i, e in
                                p['equip'].items() if e['spc']})
                    o = node_obj(wr)
                    if o > cur_obj and (best is None or o > best[0]):
                        best = (o, pos, nm, st_c, wr)
            if best is None:
                break
            o, pos, nm, st_c, wr = best
            do_equip(p, pos, 'std', nm)
            p['stats'], p['win_rates'] = st_c, wr
            cur_obj = o
            notes.append('%s 位置%d 装「%s」（次级代币次数 −1，余 %s）'
                         % (p['nodes']['name'], pos + 1, nm,
                            '不限' if left is None else left))
        # 告警仅在**显式给 0 且用尽**（left == 0）时出现；left=None = 不设上限
        if min_rate(p['win_rates']) < target - 1e-9 and left == 0:
            notes.append('%s 胜率 %.0f%% 未达标，但次级代币次数已用尽（上限 %d）'
                         % (p['nodes']['name'],
                            min_rate(p['win_rates']) * 100, uses_max))

    def greedy_teamwork(p):
        """团队合作（该单位发出的增益 ×2；消耗 1 次）"""
        cur_obj = node_obj(p['win_rates'])
        full = (len(p['cards']), 1.0, round(target * len(p['cards']), 4))
        while cur_obj < full and (left is None or left > 0) \
                and var_stock_ok(inv, '团队合作') \
                and not banned_here(p, '团队合作') \
                and p['stats']['buff_active']:
            best = None
            for pos in free_pos(p, 'std'):
                st_c, wr = eval_cards(
                    p['cards'], p['nodes'],
                    std_by={**{i: e['std'] for i, e in
                               p['equip'].items() if e['std']}, pos: '团队合作'},
                    spc_by={i: e['spc'] for i, e in
                            p['equip'].items() if e['spc']})
                o = node_obj(wr)
                if o > cur_obj and (best is None or o > best[0]):
                    best = (o, pos, st_c, wr)
            if best is None:
                break
            o, pos, st_c, wr = best
            do_equip(p, pos, 'std', '团队合作')
            p['stats'], p['win_rates'] = st_c, wr
            cur_obj = o
            notes.append('%s 位置%d 装「团队合作」（发出的增益 ×2）'
                         % (p['nodes']['name'], pos + 1))

    SPC_PRIO = ['势不可挡', '特化训练', '老水手', '快速维修', '心无旁骛']

    def greedy_special(p):
        """特殊变项（最后手段；不消耗次级代币次数）"""
        cur_obj = node_obj(p['win_rates'])
        full = (len(p['cards']), 1.0, round(target * len(p['cards']), 4))
        while cur_obj < full:
            has_debuff = any(p['stats']['debuff_total'].values())
            best = None         # (obj, prio, pos, name)
            for nm in SPC_PRIO:
                act = SPC_VAR[nm]['act']
                if not var_stock_ok(inv, nm) or banned_here(p, nm):
                    continue
                if act == 'debuff_x2' and not has_debuff:
                    continue
                if act == 'debuff_nomatch' and not \
                        any(c.get('debuff') for c in p['cards']):
                    continue
                if act == 'repair_hp' and \
                        all(int(c.get('hp', HP_MAX)) >= HP_MAX
                            for c in p['cards']):
                    continue
                prio = SPC_PRIO.index(nm)
                for pos in free_pos(p, 'spc'):
                    st_c, wr = eval_cards(
                        p['cards'], p['nodes'],
                        std_by={i: e['std'] for i, e in
                                p['equip'].items() if e['std']},
                        spc_by={**{i: e['spc'] for i, e in
                                   p['equip'].items() if e['spc']}, pos: nm})
                    o = node_obj(wr)
                    if o > cur_obj and (best is None or o > best[0]):
                        best = (o, prio, pos, nm, st_c, wr)
            if best is None:
                break
            o, _prio, pos, nm, st_c, wr = best
            do_equip(p, pos, 'spc', nm)
            p['stats'], p['win_rates'] = st_c, wr
            cur_obj = o
            notes.append('%s 位置%d 装「%s」（最后手段）'
                         % (p['nodes']['name'], pos + 1, nm))

    def assemble_node(p):
        """一轮完整装配（第 2/3/4 步的顺序与老行为一致）"""
        greedy_attrs(p)
        greedy_teamwork(p)
        greedy_special(p)

    for p in plan:
        greedy_attrs(p)
    for p in plan:
        greedy_teamwork(p)
    for p in plan:
        greedy_special(p)

    # --- 5) 「效率提升」最后手段：为达标可以卸掉它（用户 2026-10-12 裁决）---
    # 走完全部既有步骤后仍不达标、且装着「效率提升」的据点：它可能正占着
    # 达标所必需的那个标准槽 → 卸掉它（释放该槽）再对该据点做一轮同样的
    # 装配。卸掉后达标才采用（显式说明进度代币收益丢失）；仍不达标则回滚
    # ——「默认强制」不被破坏：装了也达标的据点一律不动它。
    # 与「禁用变项（全禁用）」叠加时效率提升根本没装上 → 本步自然无操作。
    # `drop_efficiency=False`（搜索阶段的验证）→ 本步整体跳过。
    for p in (plan if drop_efficiency else ()):
        if not p.get('efficiency') \
                or min_rate(p['win_rates']) >= target - 1e-9:
            continue
        name = p['nodes']['name']
        pos = next((i for i, e in sorted(p['equip'].items())
                    if e.get('std') == '效率提升'), None)
        if pos is None:
            continue
        # 回滚快照：装备表（深副本）/ 库存 / 次数余量 / 评估结果 / 说明条数
        snap = {k: p.get(k) for k in ('equip', 'std', 'spc', 'stats',
                                      'win_rates', 'repairs', 'efficiency')}
        snap['equip'] = {i: dict(e) for i, e in (snap['equip'] or {}).items()}
        snap['std'] = list(snap['std'] or [])
        snap_inv, snap_left, snap_n = dict(inv), left, len(notes)
        p['equip'][pos]['std'] = None
        if '效率提升' in p['std']:
            p['std'].remove('效率提升')
        p['efficiency'] = False
        reeval(p)
        assemble_node(p)
        if min_rate(p['win_rates']) >= target - 1e-9:
            p['efficiency_dropped'] = True
            notes.append('%s 本据点放弃「效率提升」以达标'
                         '（该据点进度代币收益丢失，约 −10%%）' % name)
        else:
            p.update(snap)              # 回滚：保持原状 + 原有告警
            inv, left = snap_inv, snap_left
            del notes[snap_n:]

    # --- 6) 收官修剪（trim）：卸掉冗余变项，退还库存 / 次数 --------------
    # 过程逻辑是「达标即停 + 弱卡优先 + 特殊变项最后手段」，目标函数里没有
    # 道具数量项 → 会出现**冗余变项**（装上但卸掉仍达标）：
    #   · 场景式救场的种子「只加不拆」（如 4×特化训练按库存全装，有的据点
    #     2~3 个就够了）；
    #   · 单调贪心 + 相邻耦合（实测 22-14-05 的两门「方向舵操作训练」都可
    #     卸 —— 该据点是靠相邻单位的特化训练把敌方削下来达标的）。
    # 实现见 `trim_plan`（单一实现，可直接单测）：卸掉后用真实评估器重算，
    # 仍 4/4 达标才保留卸除并退还；不修剪「效率提升」「快速维修」。
    # `trim=False`（搜索阶段的验证，见 `_install_variations`）→ 整体跳过。
    if trim:
        inv, left = trim_plan(plan, inv, left, target=target, notes=notes)

    # 达标标记必须在**变项装完之后**重新判定。
    for p in plan:
        lo = min_rate(p['win_rates'])
        p['meets_target'] = lo >= target - 1e-9
        if not p['meets_target']:
            notes.append('⚠ %s 装上可用变项后仍只有 %.0f%%（目标 %.0f%%）'
                         '—— 建议换阵容或先补库存' % (p['nodes']['name'],
                                                   lo * 100, target * 100))

    return inv, left, notes


# ---- 6b. 收官修剪（trim）的单一实现 ----
# 不修剪：它们的价值**不在本场胜率** —— 「效率提升」是进度代币收益（用户
# 口径强制装），「快速维修」修复的 hp 要跨阶段接力（收益在后续阶段）。
TRIM_KEEP = ('效率提升', '快速维修')


def reeval_entry(p):
    """按 `p['equip']` 实装重算 `p['stats']` / `p['win_rates']`（真实口径）

    plan_variations 的 `reeval` 与 `trim_plan` 共用（严禁第二份
    「装备表 → 胜率」的映射实现）。
    """
    st, wr = eval_cards(
        p['cards'], p['nodes'],
        std_by={i: e['std'] for i, e in p['equip'].items() if e['std']},
        spc_by={i: e['spc'] for i, e in p['equip'].items() if e['spc']})
    p['stats'], p['win_rates'] = st, wr
    return st, wr


def trim_plan(plan, inv, left, target=TARGET_RATE, notes=None):
    """收官修剪 —— 卸掉**冗余变项**，退还库存 / 次数（plan_variations 第 6 步）

    对每个据点的每门已装变项：卸下 → 用真实评估器 `eval_cards` 重算，
    仍 **4/4 达标**（口径与 plan_variations 末尾的达标判定一致）→ 保留卸除，
    并退还该门的库存（`inv`）与次级代币次数（`left`，仅标准属性 / 团队合作，
    「效率提升」不占上限）；否则还原原状。**不修剪** `TRIM_KEEP`
    （「效率提升」「快速维修」—— 价值不在本场胜率）。

    顺序：先特殊变项（关卡掉落、库存有限，最该省）再标准属性 / 团队合作；
    同类别内一轮扫描即可 —— 卸除不会抬高其他项的必需性（「心无旁骛」的
    卸下只会把停用的增益 / 减益还回来，它归在特殊变项里先判）。

    就地修改 plan 条目（equip / std / spc / stats / win_rates /
    p['trimmed'] = [(变项名, 位置0起), ...]）；`notes` 给了就追加说明。
    返回 (inv, left)。**单一实现** —— 搜索阶段的验证（`_install_variations`）
    也走 plan_variations 调到它，不复制粘贴。
    """
    def try_unequip(p, pos, slot):
        """试卸 (位置, 槽位) 的变项；仍 4/4 达标 → 保留并退还，返回是否卸掉

        退还与 `do_equip` 完全镜像：只有**显式给过数量**的变项才退库存
        （未给值 = 无上限，本就没扣），只有标准槽（且非「效率提升」）才退次数。
        """
        nonlocal left
        nm = p['equip'][pos][slot]
        if not nm or nm in TRIM_KEEP:
            return False
        p['equip'][pos][slot] = None
        reeval_entry(p)
        n_ok = sum(1 for v in p['win_rates'].values() if v >= target - 1e-9)
        if n_ok < len(p['cards']):          # 卸掉后不达标 → 还原
            p['equip'][pos][slot] = nm
            reeval_entry(p)
            return False
        lst = p['std' if slot == 'std' else 'spc']
        if nm in lst:
            lst.remove(nm)
        if nm in inv:
            inv[nm] = inv[nm] + 1
        if slot == 'std' and left is not None:
            left += 1
        p['trimmed'].append((nm, pos))
        return True

    for p in plan:
        p.setdefault('trimmed', [])
        for slot in ('spc', 'std'):     # 先特殊（掉落、最该省）再标准/团队合作
            for pos in sorted(p['equip']):
                try_unequip(p, pos, slot)
        if p['trimmed'] and notes is not None:
            cnt = {}
            for nm, _pos in p['trimmed']:
                cnt[nm] = cnt.get(nm, 0) + 1
            notes.append('%s 修剪掉 %d 门冗余变项（%s），库存/次数已退还'
                         % (p['nodes']['name'], len(p['trimmed']),
                            '、'.join('%s×%d' % (k, v)
                                      for k, v in sorted(cnt.items()))))
    return inv, left


# ============ 7. 阶段规划主流程 ============
def stage_plan(pool, nodes, target=TARGET_RATE, use_local_search=True,
               save_gold=True, final_stage=True, inv=None, frugal_all=False):
    """规划一个阶段

    倒序贪心预留 + 正序分配：据点按难度**降序**处理，
    最难者先挑卡 → 高战力卡自然流向硬仗，易题拿剩余（需求 2 与 3）。

    `save_gold`：等级治理（默认开启，`--no-save-gold` 关闭）。
    **两段式**：先试「无 3 级舰（gold）」的受限池，达标就用它（3 级留硬仗）；
    受限池搜不出解或达不到目标 → 退回全池（3 级是最后手段，不牺牲达标）。

    `final_stage`：当前阶段是否**整局最后一个阶段**。False（后面还有阶段）
    时**所有据点**都先试受限池 —— 末阶段（往往是 BOSS）通常需要满编 3 级舰，
    前面的阶段把 3 级舰烧掉会让末阶段无解（实测：阿拉斯加被前面阶段连用
    3 次 → hp0 → 阶段5 池子里没有它 → BOSS 只有 43% 上限）。True 时只有
    简单/普通据点做等级治理（老行为）。

    `inv`：本阶段变项库存（`stage_inventory` 的产物），透传给 `search_node`
    用于「选阵时就计入每单位一门标准属性变项」（见 search_node）。
    """
    order = sorted(range(len(nodes)), key=lambda i: -nodes[i]['difficulty'])
    # 「3 级卡够用」是**阶段级**属性（按本阶段起始池算一次，不随用卡浮动）
    gold_ok = stage_gold_sufficient(pool, nodes)
    remaining = list(pool)
    plan, skipped = [], []
    for idx in order:
        node = nodes[idx]
        if len(remaining) < 4:
            skipped.append(node['name'])
            continue
        pools = []
        # `frugal_all`（4.30 逐阶段倒退用）：把该阶段**所有**据点都按
        # 「省卡」处理 —— 禁 3 级 + 优先不用特殊变项，把资源让给后面的 BOSS。
        governed = save_gold and (frugal_all or node_saves_gold(node)
                                 or not final_stage)
        if governed:
            low = pool_without_tiers(remaining)
            # 受限池必须**真的更小**才值得试（池里没 3 级时两者等价，
            # 白跑一遍搜索还会让 gold_saved 标记失真）
            if len(low) >= 4 and len(low) < len(remaining):
                pools.append(low)
        pools.append(remaining)
        # 低星据点的**特殊变项预算**（4.30，用户口径：低星关卡尽量少用特殊
        # 变项，困难/关底不设限）：简单/普通且非关底的据点，先用「特殊变项
        # 一律不用」（库存里把它们清零）搜一轮 —— 达标就用它，把有限的**关卡
        # 掉落**变项留给硬仗；真的达标不了才放开（不牺牲达标）。
        # 「先不花特殊变项」的适用范围（用户 2026-10-13 口径）：低星据点
        # （简单/普通、非关底，4.30③）以及**3 级卡够用的少据点阶段**
        # （`stage_gold_sufficient`）—— 后者是「据点少、金卡足够时，
        # 宁可多花卡牌也不花特殊变项」，因为特殊变项只能靠战斗奖励获得
        # （一次性），而卡牌每次行动都会恢复状态。
        # 尝试顺序仍是「池优先于库存」：
        #   (受限池, 无特殊) → (受限池, 有特殊) → (全池, 无特殊) → (全池, 有特殊)
        # 「达标即停」保证不会为省变项而牺牲达标（先命中的达标解胜出）。
        inv_tries = []
        if inv is not None and (frugal_all or node_prefers_no_spc(node)
                                or gold_ok):
            inv0 = inv_without_specials(inv)
            if inv0 != inv:
                inv_tries.append((inv0, True))
        inv_tries.append((inv, False))
        chosen = None
        # 尝试顺序：**池优先于库存** —— 等级治理（3 级只留给困难/关底，硬规则）
        # 优先于特殊变项预算（「尽量少用」是偏好）。即
        # (受限池, 无特殊) → (受限池, 有特殊) → (全池, 无特殊) → (全池, 有特殊)。
        for cand in pools:
            for inv_try, spc_saved in inv_tries:
                sol = search_node(cand, node, target=target, inv=inv_try)
                if sol is None:
                    continue
                chosen = {**sol, 'index': idx,
                          'gold_saved': (cand is not remaining
                                         and len(cand) < len(remaining))}
                if chosen['gold_saved']:
                    chosen['gold_save_reason'] = ('difficulty'
                                                 if node_saves_gold(node)
                                                 else 'final_stage')
                if spc_saved and chosen['meets_target']:
                    chosen['spc_saved'] = True   # 该据点没用特殊变项也达标
                if chosen['meets_target']:
                    break
            if chosen is not None and chosen['meets_target']:
                break                            # 达标即停（含「受限池+无特殊」）
        if chosen is None:
            skipped.append(node['name'])
            continue
        plan.append(chosen)
        used = {c['id'] for c in chosen['cards']}
        remaining = [c for c in remaining if c['id'] not in used]

    if use_local_search and len(plan) >= 2:
        plan = local_search(plan, nodes, target=target)

    plan.sort(key=lambda p: p['index'])
    return plan, skipped, remaining


def stage_solve(pool, nodes, inv, target=TARGET_RATE, uses_max=None,
                save_gold=True, final_stage=True, use_local_search=True,
                frugal_all=False):
    """阶段求解全流程 = `stage_plan` + `plan_variations`（+ 达标兜底）

    「等级治理（不动用 3 级舰）」的前提是受限池能达标；但 `search_node`
    的达标验证是按**单据点独立库存**做的，同阶段其他据点把变项库存分掉后
    （实测据点13 所在阶段：两个据点共用「方向舵操作训练」×2），受限池的
    结果可能在阶段级 `plan_variations` 下掉到 90%。此时**退回全池重排一次**
    —— 与「3 级是最后手段、不牺牲达标」同口径；重排结果同样经
    `plan_variations` 真实评估，达标据点数不增加则保留原方案。

    返回 (plan, skipped, remaining, inv_left, uses_left, notes)。
    """
    plan, skipped, remaining = stage_plan(
        pool, nodes, target=target, use_local_search=use_local_search,
        save_gold=save_gold, final_stage=final_stage, inv=inv,
        frugal_all=frugal_all)
    inv_left, uses_left, notes = plan_variations(plan, inv, uses_max,
                                                 target=target)
    if not save_gold or not any(not p['meets_target'] for p in plan) \
            or not any(p.get('gold_saved') for p in plan):
        return _maybe_no_spc(pool, nodes, plan, skipped, remaining,
                             inv_left, uses_left, notes, inv, target,
                             save_gold, final_stage, use_local_search,
                             uses_max, frugal_all)
    plan2, skipped2, rem2 = stage_plan(
        pool, nodes, target=target, use_local_search=use_local_search,
        save_gold=False, final_stage=final_stage, inv=inv,
        frugal_all=frugal_all)
    inv2, left2, notes2 = plan_variations(plan2, inv, uses_max, target=target)
    if sum(1 for p in plan2 if p['meets_target']) \
            > sum(1 for p in plan if p['meets_target']):
        notes2.insert(0, '⚠ 等级治理的结果在实装变项后不达标'
                      '（同阶段变项库存被其他据点分走）→ 已退回全池重排')
        return _maybe_no_spc(pool, nodes, plan2, skipped2, rem2,
                             inv2, left2, notes2, inv, target,
                             save_gold, final_stage, use_local_search,
                             uses_max, frugal_all)
    return _maybe_no_spc(pool, nodes, plan, skipped, remaining,
                         inv_left, uses_left, notes, inv, target,
                         save_gold, final_stage, use_local_search,
                         uses_max, frugal_all)


def _spc_used(plan):
    """该方案用掉的特殊变项门数"""
    return sum(len(p.get('spc') or []) for p in plan)


def _maybe_no_spc(pool, nodes, plan, skipped, remaining, inv_left, uses_left,
                  notes, inv, target, save_gold, final_stage,
                  use_local_search, uses_max, frugal_all):
    """「少花特殊变项」整段复算（用户 2026-10-13 口径）

    特殊变项**只能靠战斗奖励获得（一次性）**，而卡牌每次行动都会恢复状态
    —— 所以只要「整段一门特殊变项都不花」也能达到**同样多**的达标据点，
    就应该用那一版（多花几张卡换来省下特殊变项）。

    判据自带保险：只有 `达标数 不减少` 且 `特殊变项数 真的更少` 才替换 ——
    金卡不够 / 卡池撑不住时，无特殊版会掉达标数，自然不会被采用。
    因此不需要额外按「据点多少」设闸；`inv` 里没有特殊变项时整段跳过。
    """
    if inv is None or not _spc_used(plan):
        return plan, skipped, remaining, inv_left, uses_left, notes
    inv0 = inv_without_specials(inv)
    if inv0 == inv:
        return plan, skipped, remaining, inv_left, uses_left, notes
    p0, sk0, rem0 = stage_plan(pool, nodes, target=target,
                               use_local_search=use_local_search,
                               save_gold=save_gold, final_stage=final_stage,
                               inv=inv0, frugal_all=frugal_all)
    i0, u0, n0 = plan_variations(p0, inv, uses_max, target=target)
    met = lambda pl: sum(1 for p in pl if p['meets_target'])   # noqa: E731
    if met(p0) >= met(plan) and 0 <= _spc_used(p0) < _spc_used(plan):
        n0.insert(0, '· 少花特殊变项：整段改用「一门特殊变项都不花」的编队后'
                     '达标据点不减少（%d→%d），特殊变项用量 %d→%d —— '
                     '卡牌可恢复、特殊变项一次性，故优先省特殊变项'
                     % (met(plan), met(p0), _spc_used(plan), _spc_used(p0)))
        return p0, sk0, rem0, i0, u0, n0
    return plan, skipped, remaining, inv_left, uses_left, notes


# ============ 8. 输出 ============
def format_plan(plan, pool, remaining, nodes, inp, uses_max, uses_left,
                var_notes, skipped, var_status, target=TARGET_RATE):
    L = []
    L.append('阶段 %d（共 %d 阶段） · %d 个据点 · 目标胜率 %.0f%%%s'
             % (inp['current'], inp['total'], len(nodes), target * 100,
                '' if target >= 1.0 - 1e-9
                else '（已调低：非 100% 意味着仍有翻车概率）'))
    L.append('卡池 %d 张（缺失 %d 张%s）'
             % (len(pool), len(inp['missing']),
                '：' + '、'.join(str(i) for i in inp['missing']) if inp['missing'] else ''))
    if skipped:
        L.append('⚠ 未能编队：%s' % '、'.join(skipped))
    L.append('')

    for p in plan:
        n = p['nodes']
        flags = []
        if n.get('difficulty_label'):
            flags.append(n['difficulty_label'])
        if p.get('gold_saved'):
            flags.append('等级治理·未动用3级舰'
                         if p.get('gold_save_reason') != 'final_stage'
                         else '末阶段预留·未动用3级舰')
        if n['is_progress']:
            flags.append('进度代币据点')
        c = n.get('cond') or {}
        prs = c.get('our_pairs') or []
        parts = []
        ignored_scopes = []
        if prs:
            # 逐条按 pairs 显示（作用域分组）。m9 的单值 attr/pct 可能是**错配**
            # —— `attr` 取合并文本里第一个属性名、`scope` 取第一个作用域名，
            # 两者来自**不同分节**（实测据点13：「带入：存活性 −12%…」被错配成
            # 「区域 · 存活性 −12%」）。折算口径（cond_pct）以 pairs 为准，
            # 显示必须跟上，否则展示与计算自相矛盾。
            order, grp = [], {}
            for pr in prs:
                a, pc, sc = pr.get('attr'), pr.get('pct'), pr.get('scope')
                if not a or pc is None:
                    continue
                k = sc or '作用域未读出'
                if k not in grp:
                    grp[k] = []
                    order.append(k)
                grp[k].append('%s %+d%%' % (a, pc))
                if sc not in (None, '区域') and sc not in ignored_scopes:
                    ignored_scopes.append(sc)
            if order:
                parts.append(' ｜ '.join('%s：%s' % (k, '、'.join(grp[k]))
                                        for k in order))
        else:
            if c.get('scope'):
                parts.append(c['scope'])
            if c.get('attr'):
                parts.append(c['attr'])
            if c.get('pct') is not None:
                parts.append('%+d%%' % c['pct'])
        if c.get('directive'):
            parts.append('「%s」' % c['directive'])
        if c.get('ban_item'):
            dis = n.get('disabled') or []
            if set(dis) >= ALL_VAR_NAMES:
                # 全 11 门不可用（区域「禁用变项」默认口径）→ 不列 11 个名字
                parts.append('禁用变项（该据点全部变项不可用）')
            elif dis:
                parts.append('禁用变项（禁用：%s）' % '、'.join(dis))
            else:
                parts.append('禁用变项（未填具体变项，按不禁用处理）')
        if c.get('ban_types') or c.get('ban_rows'):
            parts.append(ban_text(c))
        if parts:
            cond = '战斗条件：' + ' · '.join(parts)
            # 标注必须来自**实际折算结果**（cond_pct / cond_all_pct /
            # cond_buff_suppress），不能照显示的单值说 —— 否则又会出现
            # 「显示 区域·存活性−12% 但实际折算 {}」这类自相矛盾。
            eff = ['%s %+d%%' % (a, round(v * 100))
                   for a, v in sorted(cond_pct(n).items())]
            ap, bv = cond_all_pct(n), cond_buff_suppress(n)
            if ap:
                eff.append('战力 %+d%%' % round(ap * 100))
            if bv:
                eff.append('增益效果 %+d%%' % round(bv * 100))
            dv = cond_debuff_scale(n)
            if dv:
                eff.append('减益效果 %+d%%（我方减益强度 ×%.2f）'
                           % (round(dv * 100), 1.0 + dv))
            if eff:
                cond += '（已计入胜率：%s）' % '、'.join(eff)
                if ignored_scopes:
                    cond += '（%s 条件按全胜口径忽略）' % '/'.join(ignored_scopes)
            elif ignored_scopes:
                # 全胜口径：带入/指定/全场条件不会积累到头上，忽略不计
                cond += ('（%s 条件按全胜口径忽略，未计入）'
                         % '/'.join(ignored_scopes))
            elif not prs and c.get('scope') not in (None, '区域') \
                    and (c.get('attr') or c.get('pct') is not None
                         or c.get('ban_types')):
                cond += ('（%s 条件按全胜口径忽略，未计入）' % c.get('scope'))
            elif not prs and c.get('attr') and c.get('pct') is None:
                cond += '（未读到百分比，未计入 —— 需人工确认）'
        else:
            cond = '战斗条件：无'
        L.append('%s（%s）' % (n['name'], ' · '.join(flags) or '—'))
        L.append('  敌方战力 %s · 难度 %d' % (n['powers'], n['difficulty']))
        if n.get('powers_displayed'):
            _ep = n.get('enemy_pct') or {}
            L.append('    （面板绿字显示 %s，已按「带入」敌方战力加成 %+d%% '
                     '反推出基础值；战斗用基础值）'
                     % (n['powers_displayed'],
                        round((_ep.get('displayed') or 0) * 100)))
        L.append('  %s' % cond)
        wl = n.get('weak_list') or []
        if wl:
            L.append('  识别弱点：%s' % '  '.join(
                '敌方%d=%s' % (i + 1, '/'.join(to_zh(t) for t in lst) or '未识别')
                for i, lst in enumerate(wl)))
        if n['is_progress']:
            L.append('  掉落：进度代币 %s = %d'
                     % ('+'.join(str(n['cards'][i - 1]['token'])
                                 for i in n['progress_cards']),
                        n['progress_tokens']))
        else:
            L.append('  掉落：无进度代币')

        # 每单位变项（规则卡 16：变项只影响应用它的单位）
        equip = p.get('equip') or {}
        varlist = ['位置%d[%s（标准）]' % (i + 1, e['std'])
                   for i, e in sorted(equip.items()) if e.get('std')] \
            + ['位置%d[%s（特殊）]' % (i + 1, e['spc'])
               for i, e in sorted(equip.items()) if e.get('spc')]
        L.append('  变项：%s' % ('、'.join(varlist) if varlist else '无'))

        def src_bstep(src_name):
            """发出增益的单位的团队合作 → 该增益 30% 或 60%"""
            for i, c in enumerate(p['cards']):
                if c['name'] == src_name and \
                        (p.get('equip') or {}).get(i, {}).get('std') == '团队合作':
                    return BUFF_TEAMWORK
            return BUFF_BASE

        bstats = p['stats'].get('our_stats') or {}
        bbypos = p['stats'].get('buff_by_pos') or {}
        for s, card in enumerate(p['cards']):
            lo, hi = p['stats']['our_eff'][s]
            ep = p['stats']['enemy_eff'][s]
            wr = p['win_rates'][s]
            hits = p['stats']['debuff_total'].get(s) or {}
            hz = '、'.join('%s(%s)' % (to_zh(k), '/'.join(v))
                           for k, v in hits.items()) or '无'
            lv = LEVEL_ZH.get(card.get('tier') or '', '')
            # 有效参数（火力/存活性/机动性，已含生命值折算 + 增益 + 变项）
            if s in bstats:
                st3 = bstats[s]
                st_txt = ' %d/%d/%d=%d' % (st3[0], st3[1], st3[2], sum(st3))
            else:
                st_txt = ''
            got = bbypos.get(s) or {}
            # 该位置收到的增益：按**发出单位**是否装团队合作取 30%/60%，
            # 再乘「增益效果 −X%」条件的折算系数（−100% → 显示 +0%）
            bf = 1.0 + (cond_buff_suppress(n) if n.get('cond') else 0.0)
            buf_txt = ''
            if got:
                items = []
                for nm, cnt in got.items():
                    srcs = [b[0] for b in (p['stats'].get('buff_active') or [])
                            if b[3] == s and b[1] == nm]
                    pct = src_bstep(srcs[0]) if srcs else BUFF_BASE
                    items.append('%s %+d%%' % (nm, round(pct * bf * cnt * 100)))
                buf_txt = '  增益 ' + '、'.join(items)
            L.append('    %s 位置%d  敌方%d (%d) → 我方 %s%s%s [%d-%d] hp%d  '
                     '胜率 %3.0f%%  减益 %s%s'
                     % ('✓' if wr >= target - 1e-9 else '!', s + 1, s + 1,
                        ep, card['name'],
                        '（%s）' % lv if lv else '', st_txt, lo, hi,
                        card.get('hp', HP_MAX), wr * 100, hz, buf_txt))
        # 生效增益明细（= 绿色图标：提升相邻友军的**一条参数**，不是提升总战力）
        act = p['stats'].get('buff_active') or []
        if act:
            L.append('  生效增益（提升相邻友军的一条参数，已计入上面的参数与战力）：')
            bf2 = 1.0 + (cond_buff_suppress(n) if n.get('cond') else 0.0)
            for (src, btype, bdir, j) in act:
                src_slot = j + 1 if bdir == 'left' else j - 1
                L.append('    %s（位置%d）→ 位置%d · %s %+d%%'
                         % (src, src_slot + 1, j + 1, btype,
                            round(src_bstep(src) * bf2 * 100)))
        else:
            L.append('  生效增益：无（本据点无可生效的绿色增益图标）')
        L.append('  据点胜率下限 %.0f%%%s'
                 % (min_rate(p['win_rates']) * 100,
                    '' if p['meets_target'] else '（低于目标 %.0f%%）'
                    % (target * 100)))
        if n['review']:
            L.append('  待人工确认：%s' % '；'.join(n['review']))
        L.append('')

    total_prog = sum(p['nodes']['progress_tokens'] for p in plan)
    boosted = sum(p['nodes']['progress_tokens'] for p in plan
                  if p.get('efficiency'))
    eff = sum(1 for p in plan if p.get('efficiency'))
    L.append('进度代币（唯一参与决策的掉落物）：本阶段合计 %d' % total_prog)
    if total_prog:
        L.append('  已装「效率提升」的据点：%d 个 → 该部分收益 %d × %.2f = %d'
                 % (eff, boosted, 1 + EFFICIENCY_BONUS,
                    int(round(boosted * (1 + EFFICIENCY_BONUS)))))
    used_n = sum(1 for p in plan for nm in (p.get('std') or [])
                 if nm != '效率提升')
    if uses_max is None:
        L.append('次级代币使用：**未设上限**（玩家未指定）→ 已用 %d 次'
                 '（「效率提升」不占用上限，本次装 %d 个）' % (used_n, eff))
    else:
        L.append('次级代币使用：上限 %d 次 → 已用 %d 次'
                 '（「效率提升」不占用上限，本次装 %d 个）'
                 % (uses_max, uses_max - uses_left, eff))
    L.append('')
    used = [c for p in plan for c in p['cards']]
    L.append('战力留存报告：')
    L.append('  本阶段消耗 %d 张（每张阶段内只能参战一次）' % len(used))
    keep = sorted(remaining, key=lambda c: -c['base_power'][1])[:6]
    L.append('  保留高战力卡：%s'
             % ('、'.join(
                 '%s(%s%d-%d)' % (c['name'],
                                  LEVEL_ZH.get(c.get('tier') or '', '') + ' '
                                  if c.get('tier') else '',
                                  *c['base_power'])
                 for c in keep) or '无'))
    L.append('  后续阶段可用余量：%d / %d 张' % (len(remaining), len(pool)))
    if var_status != 'CONFIRMED':
        L.append('')
        L.append('⚠ 变项身份映射状态 = %s：变项分配结果基于未确认的槽位顺序，'
                 '请核对 data/var_config.json' % var_status)
    if var_notes:
        L.append('')
        L.append('变项分配说明：')
        for x in var_notes:
            L.append('  · %s' % x)
    L.append('')
    L.append('提示：截图请**只截战斗区**（敌方卡牌 + 中部部署区 + 变项工具栏 + 右侧面板），')
    L.append('      不要把我方卡组栏一起截进来 —— 它与变项工具栏紧邻、同为亮色，')
    L.append('      会干扰工具栏定位（本次已正常识别，但裁掉更稳）。')
    L.append('      我方卡牌的等级由 data/our_fleet.json 的 tier 提供，不依赖截图。')
    L.append('      掉落物**只识别进度代币**（判据是圆牌为金色）：'
             '次级代币、经验值、硬币等掉落物一律忽略，不影响任何决策。')
    return '\n'.join(L)


# ============ 输出：编排建议 → 结构化 JSON（M6-1） ============
def plan_to_json(inp, plan, remaining, uses_max, uses_left, notes,
                 target=TARGET_RATE):
    """编排建议 → 可 json.dumps 的 dict（喂给图形面板 / 外部脚本）

    schema = 'plan/v1'。字段口径与 format_plan 的文本输出**逐一对应**：
      - win_rate / min_win_rate 已四舍五入到 4 位；
      - debuffs 的键是中文弱点类型、值是命中方向列表（与文本一致）；
      - level 是卡牌等级中文（1级/2级/3级），来自 our_fleet.json 的 tier；
      - condition.quantified = 是否有已折算进胜率的属性类条件；
      - summary.secondary_uses = {max, left, used}：max/left 为 null 表示
        **不设上限**（玩家未指定）；used = 已装的次级代币标准变项数
        （不含「效率提升」，它不占上限）。

    2026-10-12 新增**可选字段**（既有字段名/位置一律未动，界面可忽略）：
      - condition.pairs = [{'attr','pct','scope'}] 逐条条件（单值 attr/scope
        可能是 m9 的错配，界面若要和胜率口径一致优先用 pairs）；
      - node.gold_save_reason = 'difficulty' | 'final_stage' | null
        （gold_saved 的原因：简单/普通据点，还是非最终阶段的末阶段预留）；
      - stage.total 可能是**面板 OCR 的**阶段总数（整局模式取最大阶段号，
        与玩家输入的 stage.total 不同时会以 OCR 为准）；
      - condition.disabled_all = 该据点是否**全部变项不可用**（区域
        「禁用变项」未填名单时的默认口径，11 门全禁；界面可据此把
        `disabled_variations` 的 11 个名字显示成一句话）；
      - node.efficiency_dropped = 该据点是否**为达标放弃了「效率提升」**
        （最后手段：卸掉它换取达标，进度代币 +10% 收益丢失）；
      - node.trimmed = [{'name','position'}] 收官修剪（plan_variations 第 6 步）
        卸掉的**冗余变项**（装上但卸掉仍 4/4 达标；不含「效率提升」与
        「快速维修」）—— 库存/次数已退还，variations 与 summary 均已是
        修剪后的口径。
    """
    nodes_out = []
    for p in plan:
        n = p['nodes']
        st = p['stats']
        bstats = st.get('our_stats') or {}
        equip = p.get('equip') or {}

        def src_bstep(src_name):
            for i, c in enumerate(p['cards']):
                if c['name'] == src_name and \
                        equip.get(i, {}).get('std') == '团队合作':
                    return BUFF_TEAMWORK
            return BUFF_BASE

        cards_out = []
        for s, c in enumerate(p['cards']):
            lo, hi = st['our_eff'][s]
            hits = st['debuff_total'].get(s) or {}
            e = equip.get(s, {})
            cards_out.append({
                'position': s + 1,
                'card_id': c['id'],
                'name': c['name'],
                'level': LEVEL_ZH.get(c.get('tier') or '', None) or None,
                'tier': c.get('tier') or None,   # 卡面档位 white/blue/gold/red
                'hp': c.get('hp', HP_MAX),
                'stats': bstats.get(s),
                'power_range': [lo, hi],
                'enemy_eff': st['enemy_eff'].get(s),
                'win_rate': round(p['win_rates'][s], 4),
                'variations': {'std': e.get('std'), 'spc': e.get('spc')},
                'debuffs': {to_zh(k): list(v) for k, v in hits.items()},
                'buffs_received': [{'attr': nm, 'pct': round(src_bstep(
                    next((b[0] for b in (st.get('buff_active') or [])
                         if b[3] == s and b[1] == nm), '')) * cnt
                    * (1.0 + (cond_buff_suppress(n) if n.get('cond')
                              else 0.0)), 2)}
                    for nm, cnt in
                    (st.get('buff_by_pos') or {}).get(s, {}).items()],
            })
        c = n.get('cond') or {}
        nodes_out.append({
            'name': n['name'],
            'difficulty': n['difficulty'],
            'difficulty_label': n.get('difficulty_label'),
            'enemy_powers': list(n['powers']),
            # 新增可选字段（2026-10-13）：面板 OCR 到的原始战力（= 已含
            # 「带入」敌方战力加成，绿字显示）与反推出的基础值对照。
            # 只有该据点的「敌方战力 ±X%」挂在「带入」段时才有这两个键 ——
            # `enemy_powers` 是**战斗口径**（基础值），界面若要复刻面板绿字
            # 请用 `enemy_powers_displayed`。
            **({'enemy_powers_displayed': list(n['powers_displayed']),
                'enemy_power_pct': dict(n['enemy_pct'])}
               if n.get('powers_displayed') else {}),
            'condition': {'attr': c.get('attr'), 'pct': c.get('pct'),
                          'scope': c.get('scope'),
                          'directive': c.get('directive'),
                          'ban_item': bool(c.get('ban_item')),
                          'ban_types': list(c.get('ban_types') or []),
                          # 新增可选字段：禁用条目的**逐行**读数 —— 一个据点
                          # 可以有多行「禁用的单位类型」（带入/区域/指定各一
                          # 行），每行 {'scope', 'types'}。拦人的只有区域行
                          # （`ban_types` 已经是那一部分）；界面要展示面板
                          # 原貌（含不生效的行）请用这个。
                          'ban_rows': [{'scope': r.get('scope'),
                                        'types': list(r.get('types') or [])}
                                       for r in (c.get('ban_rows') or [])],
                          'disabled_variations': list(n.get('disabled')
                                                      or []),
                          # 新增可选字段：True = 该据点**全部变项不可用**
                          # （区域「禁用变项」未填名单时的默认口径，11 门全禁）
                          'disabled_all': (set(n.get('disabled') or [])
                                           >= ALL_VAR_NAMES),
                          'quantified': has_cond(n),
                          # 新增可选字段：逐条 (作用域, 属性, 百分比)。
                          # 单值 attr/scope 可能是 m9 的**错配**（来自不同分节），
                          # 界面若要与胜率口径一致请优先用这个列表。
                          'pairs': [{'attr': pr.get('attr'),
                                     'pct': pr.get('pct'),
                                     'scope': pr.get('scope')}
                                    for pr in (c.get('our_pairs') or [])]},
            'weak': [sorted(w) for w in n['weak']],
            'progress_tokens': n['progress_tokens'],
            'is_progress': bool(n['is_progress']),
            'gold_saved': bool(p.get('gold_saved')),
            # 新增可选字段：gold_saved 的原因
            # 'difficulty' = 简单/普通据点（老口径）
            # 'final_stage' = 非最终阶段的据点（末阶段预留，新口径）
            'gold_save_reason': (p.get('gold_save_reason')
                                 if p.get('gold_saved') else None),
            'variations': {'std': list(p['std']), 'spc': list(p['spc'])},
            # 新增可选字段：该据点**为达标放弃了「效率提升」**（最后手段，
            # 进度代币 +10% 收益丢失；见 plan_variations 第 5 步）
            'efficiency_dropped': bool(p.get('efficiency_dropped')),
            # 新增可选字段：收官修剪（第 6 步）卸掉的**冗余变项**
            # [{'name','position'}]（位置 1 起；库存/次数已退还 ——
            # summary.secondary_uses 与 variations 都已是修剪后的口径）
            'trimmed': [{'name': nm, 'position': pos + 1}
                        for nm, pos in (p.get('trimmed') or [])],
            'active_buffs': [{'from': a, 'attr': b, 'dir': d,
                              'to_position': j + 1}
                             for (a, b, d, j) in (st.get('buff_active') or [])],
            'assignment': cards_out,
            'min_win_rate': round(min_rate(p['win_rates']), 4),
            'meets_target': bool(p['meets_target']),
            'review': list(n.get('review') or []),
        })
    used = [c for p in plan for c in p['cards']]
    keep = sorted(remaining, key=lambda c: -c['base_power'][1])[:6]
    return {
        'schema': 'plan/v1',
        'stage': {'current': inp['current'], 'total': inp['total'],
                  'target_rate': target, 'n_nodes': len(plan)},
        'nodes': nodes_out,
        'summary': {
            'cards_used': sorted(c['id'] for c in used),
            'cards_used_count': len(used),
            'kept_top': [{'id': c['id'], 'name': c['name'],
                          'power_hi': c['base_power'][1],
                          'level': LEVEL_ZH.get(c.get('tier') or '',
                                                None) or None,
                          'tier': c.get('tier') or None} for c in keep],
            'secondary_uses': {
                'max': uses_max, 'left': uses_left,
                # used = 已装的次级代币标准变项数（不含「效率提升」）
                'used': sum(1 for p in plan for nm in (p.get('std') or [])
                            if nm != '效率提升')},
            'progress_tokens_total': sum(p['nodes']['progress_tokens']
                                         for p in plan),
            'efficiency_nodes': sum(1 for p in plan if p.get('efficiency')),
            'notes': list(notes),
        },
    }


# ============ 跨阶段状态（生命值接力） ============
STATE_VERSION = 2


def load_state(path):
    """读取中间状态文件；不存在/损坏则返回 None

    v2 结构（**每阶段一份快照，便于回退**）：
        {
          'version': 2,
          'stage':   最近一次完成的阶段号,
          'hp':      {卡id: 剩余格数}    —— 与 snapshots[stage] 相同，冗余便于读取
          'used':    [本阶段参战卡 id],
          'snapshots': {
              '1': {'hp': {...}, 'used': [...], 'met': true/false,
                    'nodes': ['s1_kp1', ...], 'target': 1.0},
              ...
          }
        }

    约定：`snapshots[N]` = **阶段 N 开始时**的 hp（= 阶段 N−1 结束后），
    以及阶段 N 的用卡清单。因此 `--rollback-to N` 后可从阶段 N 原样重跑。
    """
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def save_state(path, fleet, damaged, used_ids, stage, extra=None,
               snapshots=None, base_state=None):
    """写入本阶段结束后的状态（供下一阶段接力 + 日后回退）

    hp 对**全部 30 张**结算：参战过 −1 格，未参战保持原值。
    snapshots 会保留历史各阶段的快照，新增/覆盖 `snapshots[stage]`。
    """
    used = set(int(i) for i in used_ids)
    hp = {}
    for c in fleet:
        cur = int(damaged.get(c['id'], int(c.get('hp', HP_MAX))))
        hp[str(c['id'])] = max(0, cur - (1 if c['id'] in used else 0))
    snaps = {}
    if isinstance(base_state, dict) and isinstance(base_state.get('snapshots'),
                                                   dict):
        snaps = {k: v for k, v in base_state['snapshots'].items()}
    if snapshots:
        snaps.update(snapshots)
    out = {'version': STATE_VERSION, 'stage': stage, 'hp': hp,
           'used': sorted(used), 'snapshots': snaps}
    if extra:
        out.update(extra)
    Path(path).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                          encoding='utf-8')
    return hp


def prev_damaged(state, current):
    """从状态里取出「上一阶段的 HP 结果」；阶段号对不上则忽略"""
    if not state:
        return {}, None
    if int(state.get('stage') or 0) != current - 1:
        return {}, state.get('stage')
    return {int(k): int(v) for k, v in (state.get('hp') or {}).items()}, None


def snapshot_of(state, stage):
    """取出阶段 `stage` **开始时**的 hp 快照 → {卡id: 格数} 或 {}"""
    if not state:
        return {}
    snaps = state.get('snapshots') or {}
    rec = snaps.get(str(stage)) or snaps.get(stage)
    if not rec:
        return {}
    return {int(k): int(v) for k, v in (rec.get('hp') or {}).items()}


def rollback(state, stage):
    """把状态回退到「阶段 `stage` 开始之前」

    即丢弃 snapshots 里 ≥ stage 的记录，并把 hp / stage 置为
    snapshots[stage] 的内容。返回新状态 dict（不写盘）。
    """
    if not state:
        return None
    snaps = {k: v for k, v in (state.get('snapshots') or {}).items()
             if int(k) < int(stage)}
    hp = snapshot_of(state, stage)
    out = {'version': STATE_VERSION, 'stage': int(stage) - 1,
           'hp': {str(k): v for k, v in hp.items()}, 'used': [],
           'snapshots': snaps}
    return out


def list_snapshots(state):
    """→ [(阶段号, 带伤张数, 是否达标, 据点数)]，按阶段号升序"""
    if not state:
        return []
    out = []
    for k, rec in (state.get('snapshots') or {}).items():
        hp = rec.get('hp') or {}
        out.append((int(k), sum(1 for v in hp.values() if int(v) < HP_MAX),
                    bool(rec.get('met')), len(rec.get('nodes') or [])))
    return sorted(out)


# ============ CLI ============
def _require_powers(nodes):
    """求解前校验：敌方战力必须全部读出（否则 m7 里 min(None) 深层崩溃）"""
    for n in nodes:
        if len(n['powers']) != 4:
            raise ValueError(
                '%s 只检出 %d 张敌方卡（应为 4）—— 卡列定位失败，'
                '请检查截图' % (n['name'], len(n['powers'])))
    bad = ['%s（卡%d）' % (n['name'], i + 1)
           for n in nodes
           for i, p in enumerate(n['powers']) if p is None]
    if bad:
        raise ValueError('敌方战力未读出：%s —— 截图界面/分辨率可能不受支持，'
                         '请对照识别结果人工补录' % '、'.join(bad))


def find_shot(stage_dir, name):
    """按名字找截图，扩展名不限（.jpg/.png/.jpeg/.webp/.bmp）"""
    for ext in SHOT_EXTS:
        p = Path(stage_dir) / (name + ext)
        if p.exists():
            return str(p)
    return None


def _dedupe_nodes(nodes):
    """同一据点多张截图 → 保留**最新**的一张（按文件修改时间）

    玩家常会对同一据点重截（补条件/补工具栏/换界面），重复进规划
    会把该据点算两次、重复消耗卡。返回 (nodes, notes)。
    """
    by_key = {}
    for n in nodes:
        key = (n.get('stage'), n.get('node'))
        prev = by_key.get(key)
        if prev is None or n.get('mtime', 0) > prev.get('mtime', 0):
            by_key[key] = n
    kept = list(by_key.values())
    notes = []
    if len(kept) != len(nodes):
        dropped = [n for n in nodes if n not in kept]
        for d in dropped:
            k = by_key[(d.get('stage'), d.get('node'))]
            if d['powers'] != k['powers']:
                notes.append(
                    '⚠ %s 与 %s 同为阶段%s据点%s 但敌方战力不同'
                    '（%s vs %s）—— 可能混入了**不同行动**的截图，'
                    '请按行动分目录存放'
                    % (d['name'], k['name'], d.get('stage'), d.get('node'),
                       d['powers'], k['powers']))
        notes.append('同一据点多张截图,保留最新:%s（丢弃 %s）'
                     % ('、'.join(n['name'] for n in
                                  sorted(kept, key=lambda x: x.get('mtime', 0))),
                        '、'.join(n['name'] for n in dropped)))
    return kept, notes


def _mid_stage_report(st, total, plan, skipped):
    """逐阶段的**中间结果**（一行）：完成一个阶段就打印一次（用户 4.30）"""
    bad = [p['nodes']['name'] for p in plan if not p.get('meets_target')]
    boss = [p for p in plan if p['nodes'].get('is_progress')]
    line = '【阶段 %d/%d 完成】据点 %d 个' % (st, total, len(plan))
    if skipped:
        line += ' · 跳过 %d 个' % len(skipped)
    line += ' · 未达标 %d 个' % len(bad)
    if boss:
        line += ' · 关底 %s' % ('、'.join(p['nodes']['name'] for p in boss))
        line += ('（**未达标**）' if any(not p.get('meets_target')
                                       for p in boss) else '（达标）')
    print(line, flush=True)


def _boss_failed_stage(groups, results):
    """返回「关底（掉进度代币的据点）没算出目标胜率」的最靠前阶段号，否则 None

    「逐阶段倒退」的触发条件（用户 4.30）：**只**在关底算不出时倒退 ——
    普通据点没算出不去动前序阶段，避免每次失败都重跑（否则代价 O(阶段数²)）。
    关底 = `is_progress`（规则手册：只有关底 BOSS 掉进度代币）；被跳过（卡不够）
    同样算失败。
    """
    for st in sorted(groups):
        for n in groups[st]:
            if not n.get('is_progress'):
                continue
            hit = next((x for x in (results.get(st, {}).get('plan') or [])
                        if x['nodes']['name'] == n['name']), None)
            if hit is None or not hit.get('meets_target'):
                return st
    return None


def plan_all_stages(fleet, inp, damaged0, nodes, target=TARGET_RATE,
                    uses_max=None, no_local=False, save_gold=True,
                    boss_reserve=True, boss_rollback=False, max_rollback=3):
    """整局模式（--all）：按**面板 OCR 的阶段号**分组，逐阶段顺序求解

    据点编号在整个行动内连续（据点1~7 跨阶段 1~4），但**部署约束是按阶段
    的**——每张卡每阶段只能参战一次，跨阶段可复用、每参战一次 −1 格。
    故不能把全部据点塞进一次 stage_plan，必须按阶段切开、串行求解，
    生命值在内存中逐阶段接力。

    返回 (stage_results, docs)：
      stage_results: {阶段号: {'plan','skipped','remaining','uses_left',
                               'notes','var_status','pool','damaged_start'}}
      docs:          [plan/v1 dict]，与 stage_results 同序
    uses_max 是**每个阶段**各自的上限（口径三的「本轮」= 一个阶段）；
    None = 不设上限（玩家未指定，默认），显式数字（含 0）才作为约束。

    **阶段总数以面板 OCR 为准**：取所有节点 `stage` 字段的最大值（玩家输入
    `stage.total` 常与实际不符 —— 实测新行动 5 阶段而输入写 4，会打出
    「阶段5（共4阶段）」这种自相矛盾的话，且「是否最终阶段」判断出错 →
    末阶段预留失效）。玩家输入只作兜底：取两者较大值，截图只覆盖前半段时
    不会把中间阶段误判成最终阶段（末阶段预留宁保守不失效）。
    每个阶段的 docs 里 `stage.total` 都是这个 OCR 口径的总数。

    **关底预留 `boss_reserve`（2026-10-13，用户方案）**：把关底阶段的据点
    先**单独**按「全卡满血」算一遍，取它用到的卡当**预留**，再让**它之前的
    所有阶段禁用这些卡**重跑正向 —— 保证关底那几张卡不会被前序阶段提前
    用掉（用掉一次就 −1 格，关底就弱了），从而**保证关底达标**。
    这是「事前预留」而非「事后回退」：回退要等失败才发现、且要整段重跑
    若干轮；预留一次就把资源让给关底了。`boss_rollback`（旧的事后回退）
    默认**关闭**，需要时用 `--boss-rollback` 打开。
    ⚠ 预留取的是**关底所在阶段整段**用到的卡，不只是关底那一关 ——
    同一阶段的据点共用卡池，且卡进关底阶段时的血量正是前面阶段留下的，
    只留关底那一关会用到的卡，同阶段其它据点仍可能把关底要用的卡打伤
    （阶段级 `plan_variations` 与血量接力都在阶段粒度上）。
    ⚠ 关底在**全卡满血**下都算不到目标胜率时**不做预留**（预留也救不了
    关底，只会白白削弱前面阶段），照常按顺序求解并照实报未达标。
    """
    nodes, dedupe_notes = _dedupe_nodes(nodes)
    for x in dedupe_notes:
        print('【去重】%s' % x)
    groups = {}
    for n in nodes:
        st = n.get('stage')
        if st is None:
            raise ValueError(
                '%s 面板未读出阶段号，整局模式无法分组' % n['name'])
        groups.setdefault(int(st), []).append(n)
    total = max([int(n['stage']) for n in nodes]
                + [int((inp or {}).get('total') or 1)])

    base_damaged = {int(k): int(v) for k, v in (damaged0 or {}).items()}
    slot_names, var_status = load_var_config()

    def stage_args(st):
        """某阶段求解的公共实参（预留解与正向跑必须完全一致）"""
        nodes_st = groups[st]
        _require_powers(nodes_st)
        inp_st = dict(inp)
        inp_st['current'] = st
        inp_st['total'] = total
        inv, inv_src = stage_inventory(nodes_st, inp_st, slot_names)
        return nodes_st, inp_st, inv, inv_src

    def solve_stage(st, pool, inv, frugal=False):
        return stage_solve(
            pool, groups[st], inv, target=target, uses_max=uses_max,
            save_gold=save_gold, final_stage=(st == total),
            use_local_search=not no_local, frugal_all=frugal)

    # --- 关底预留：先把关底阶段单独算一遍，取它要用的卡 ---
    boss_stages = sorted({int(n['stage']) for n in nodes
                          if n.get('is_progress')})
    frozen, reserved = {}, {}
    if boss_reserve and boss_stages:
        for st in boss_stages:
            _nodes_st, _inp_st, inv, _src = stage_args(st)
            out = solve_stage(st, build_pool(fleet, inp['missing'],
                                             base_damaged)[0], inv)
            plan0, skipped0, rem0, inv_left0, uses0, notes0 = out
            if any(not p.get('meets_target') for p in plan0) or skipped0:
                print('【关底预留】阶段 %d 在全卡满血下也没算出目标胜率'
                      '（未达标 %d 个、跳过 %d 个）→ 不做预留，照常按顺序求解'
                      % (st, sum(1 for p in plan0 if not p.get('meets_target')),
                         len(skipped0)), flush=True)
                continue
            ids = {c['id'] for p in plan0 for c in p['cards']}
            frozen[st], reserved[st] = out, ids
            names = sorted({c['name'] for p in plan0 for c in p['cards']})
            print('【关底预留】阶段 %d 先单独算一遍（全卡满血、可达标）：它要用 '
                  '%d 张卡 → 阶段 %s 一律禁用这些卡（%s%s）'
                  % (st, len(ids),
                     '、'.join(str(x) for x in sorted(
                         x for x in groups if x < st)) or '无（关底在第一阶段）',
                     '、'.join(names[:8]),
                     ' 等' if len(names) > 8 else ''), flush=True)

    def forward(frugal_stages=frozenset()):
        """正向跑一遍（`frugal_stages` 内的阶段强制省卡）→ (results, docs)

        每完成一个阶段打印一次**中间结果**（见 `_mid_stage_report`）。
        """
        damaged = dict(base_damaged)
        results, docs = {}, []
        for st in sorted(groups):
            nodes_st, inp_st, inv, inv_src = stage_args(st)
            # 关底预留：**本阶段之后**的关底阶段要用到的卡，本阶段一律不准碰
            hold = set()
            for st_b, ids in reserved.items():
                if st < st_b:
                    hold |= ids
            pool, dead = build_pool(
                fleet, list(inp['missing']) + sorted(hold), damaged)
            if st in frozen:
                # 关底阶段直接用预留解（它只用预留卡，那些卡必然满血在场）；
                # 只有「剩余卡池」按**真实**牌面重算，供显示与 plan/v1 用
                plan, skipped, _rem, inv_left, uses_left, notes = frozen[st]
                used_ids = {c['id'] for p in plan for c in p['cards']}
                remaining = [c for c in pool if c['id'] not in used_ids]
            else:
                plan, skipped, remaining, inv_left, uses_left, notes = \
                    solve_stage(st, pool, inv, frugal=(st in frugal_stages))
            note_inv = inv_source_note(inv_src, nodes_st)
            if note_inv:
                notes.insert(0, note_inv)
            if hold:
                notes.append('本阶段禁用了关底预留的 %d 张卡'
                             '（保证关底满血可用）' % len(hold))
            results[st] = {'plan': plan, 'skipped': skipped,
                           'remaining': remaining, 'uses_left': uses_left,
                           'notes': notes, 'var_status': var_status,
                           'pool': pool, 'damaged_start': dict(damaged)}
            docs.append(plan_to_json(
                inp_st, plan, remaining, uses_max, uses_left, notes,
                target=target))
            _mid_stage_report(st, total, plan, skipped)
            # 接力：本阶段参战卡 −1 格，作为下一阶段起点。
            # 「快速维修」修复过的卡以修复后的血量参战（p['repairs']）
            repairs = {}
            for p in plan:
                repairs.update(p.get('repairs') or {})
            used = {c['id'] for p in plan for c in p['cards']}
            damaged = {int(c['id']): max(0, int(repairs.get(
                c['id'], damaged.get(c['id'], int(c.get('hp', HP_MAX)))))
                - (1 if c['id'] in used else 0))
                for c in fleet}
        return results, docs

    results, docs = forward()
    if boss_rollback:
        # --- 逐阶段倒退（用户 4.30）：**只**在关底算不出时触发 ---
        # 由近及远把「关底阶段前面的阶段」改成强制省卡（禁 3 级 + 优先不用
        # 特殊变项），把资源让给关底，然后整段重跑。命中即停 —— 不做全量
        # 两两回退，故不是 O(阶段数²)。
        for k in range(1, int(max_rollback) + 1):
            st_boss = _boss_failed_stage(groups, results)
            if st_boss is None:
                break
            frugal = frozenset(x for x in groups
                               if st_boss - 1 - k < x < st_boss)
            if not frugal:
                break
            print('【倒退 %d/%d】关底所在阶段 %d 没算出 100%% → 把阶段 %s 改为'
                  '强制省卡后重跑' % (k, max_rollback, st_boss,
                                    '、'.join(str(x) for x in sorted(frugal))),
                  flush=True)
            results, docs = forward(frugal)
    return results, docs


def main(argv=None):
    ap = argparse.ArgumentParser(description='M5 编排规划器')
    ap.add_argument('--input', default=str(DATA / 'player_input.json'),
                    help='玩家输入（缺失卡牌 / 生命值 / 阶段 / 次级货币）')
    ap.add_argument('--shots', nargs='*', default=None,
                    help='本阶段各据点的截图名（不含扩展名）。'
                         '**省略则自动扫描 --stage-dir 下全部图片**'
                         '—— 一次丢进整个阶段的所有关卡即可')
    ap.add_argument('--stage-dir', default=str(DATA / 'stage'))
    ap.add_argument('--target', type=float, default=TARGET_RATE,
                    help='目标胜率下限，默认 1.0 = 100%%；'
                         '可用 0.95 / 0.9 等自行调低')
    ap.add_argument('--currency', type=int, default=None,
                    help='（旧参数）等价于次级代币次数上限：'
                         '省略 = 不设上限（默认）；填 0 = 本轮不用次级代币变项')
    ap.add_argument('--uses-max', type=int, default=None,
                    help='本轮次级代币可购买变项的次数上限'
                         '（「效率提升」不计入）：'
                         '省略 = 不设上限（默认）；填 0 = 本轮不用次级代币变项')
    ap.add_argument('--state', default=str(DATA / 'player_state.json'),
                    help='跨阶段中间状态文件（生命值接力）')
    ap.add_argument('--no-state', action='store_true',
                    help='不读也不写状态文件（每次从满血开始）')
    ap.add_argument('--no-ocr', action='store_true', help='跳过 OCR（更快）')
    ap.add_argument('--no-local', action='store_true', help='关闭局部搜索')
    ap.add_argument('--no-save-gold', action='store_true',
                    help='关闭等级治理（默认开启：简单/普通据点不动用'
                         ' 3 级舰，把高等级卡留给硬仗）')
    ap.add_argument('--no-boss-reserve', action='store_true',
                    help='关闭「关底预留」（**默认开启**：整局模式先把关底'
                         '阶段单独按全卡满血算一遍，它要用到的卡在**前面各'
                         '阶段一律禁用**，保证关底那几张卡满血可用、关底达标）')
    ap.add_argument('--boss-rollback', action='store_true',
                    help='**旧的事后回退**（默认关闭）：关底没算出 100%% 时'
                         '把前面的阶段改成强制省卡后整段重跑，最多回退 '
                         '--rollback-max 次。现由「关底预留」事前解决，'
                         '此开关只在预留也救不了时才有意义')
    ap.add_argument('--no-boss-rollback', action='store_true',
                    help='（已废弃）事后回退现在是默认关闭的，本开关无效果')
    ap.add_argument('--rollback-max', type=int, default=3, metavar='N',
                    help='事后回退的最大次数，默认 3（需 --boss-rollback；'
                         '每回退一次多把一个前序阶段改为强制省卡）')
    ap.add_argument('--all', action='store_true',
                    help='**整局模式**：--stage-dir 下放整个行动的全部截图，'
                         '按面板阶段号自动分组、逐阶段顺序求解'
                         '（每卡每阶段一次、跨阶段战损接力）')
    ap.add_argument('--out', default=str(DATA / 'plan.json'),
                    help='编排建议的结构化输出（JSON，plan/v1 schema）')
    ap.add_argument('--no-out', action='store_true',
                    help='不写 JSON 输出文件')
    ap.add_argument('--rollback-to', type=int, default=None, metavar='N',
                    help='回退到「阶段 N 开始之前」：丢弃 N 及之后的快照，'
                         '之后可从阶段 N 原样重跑。只改状态文件，不做规划')
    ap.add_argument('--snapshots', action='store_true',
                    help='列出状态文件里已有的各阶段快照，然后退出')
    args = ap.parse_args(argv)

    # --- 只查询 / 只回退：不跑规划 ---
    if args.snapshots or args.rollback_to is not None:
        st = load_state(args.state)
        if not st:
            print('（没有状态文件：%s）' % args.state)
            return 1
        if args.snapshots:
            rows = list_snapshots(st)
            if not rows:
                print('（状态文件里没有快照）')
                return 0
            print('已有快照（阶段号 = 该阶段**开始时**的状态）：')
            for (k, n_loss, met, n_node) in rows:
                print('  阶段 %-2d  带伤 %2d 张 · %d 个据点 · %s'
                      % (k, n_loss, n_node,
                         '达标' if met else '未达目标'))
            return 0
        new = rollback(st, args.rollback_to)
        if new is None:
            print('回退失败：未找到阶段 %d 的快照' % args.rollback_to)
            return 1
        Path(args.state).write_text(
            json.dumps(new, ensure_ascii=False, indent=2), encoding='utf-8')
        n_loss = sum(1 for v in (new['hp'] or {}).values()
                     if int(v) < HP_MAX)
        print('【已回退】状态回到「阶段 %d 开始之前」（视为阶段 %d 已结束），'
              '带伤 %d 张' % (args.rollback_to, args.rollback_to - 1, n_loss))
        print('  现在可以从阶段 %d 开始重跑。' % args.rollback_to)
        return 0

    inp_path = Path(args.input)
    if inp_path.exists():
        inp = load_player_input(inp_path)
    else:
        print('（未找到 %s，使用默认：拥有全部卡、满血、阶段 1/1、'
              '次级代币次数不设上限）' % inp_path)
        inp = {'current': 1, 'total': 1, 'uses_max': None,
               'missing': [], 'damaged': {}, 'banned': {}, 'disabled': {},
               'variations': None}
    v = args.uses_max if args.uses_max is not None else args.currency
    if v is not None:
        inp['uses_max'] = int(v)

    fleet = load_fleet()

    # --- 跨阶段生命值接力 ---
    state, carry_note = None, '未使用状态文件（全部按满血计算）'
    damaged = dict(inp['damaged'])
    if not args.no_state:
        state = load_state(args.state)
        prev, mismatch = prev_damaged(state, inp['current'])
        if prev:
            merged = dict(prev)
            merged.update(inp['damaged'])        # 用户显式输入优先
            damaged = merged
            n_loss = sum(1 for k, v in prev.items() if v < HP_MAX)
            carry_note = ('已从状态文件接力上一阶段的战损：%d 张卡带伤 '
                          '（来源 %s）' % (n_loss, Path(args.state).name))
        elif mismatch is not None:
            carry_note = ('状态文件里记录的是阶段 %s，与当前阶段 %d 不衔接 '
                          '→ 本次全部按满血计算' % (mismatch, inp['current']))

    pool, dead = build_pool(fleet, inp['missing'], damaged)

    shots = args.shots
    if shots is None:
        d = Path(args.stage_dir)
        shots = sorted(p.stem for p in d.iterdir()
                       if p.is_file() and p.suffix.lower() in SHOT_EXTS)
        if not shots:
            print('⚠ %s 下没有图片（%s）' % (d, '/'.join(SHOT_EXTS)))
            return 2
        print('【自动扫描】%s 下发现 %d 张截图：%s'
              % (d.name, len(shots), '、'.join(shots)))
    nodes = []
    for s in shots:
        path = find_shot(args.stage_dir, s)
        if path is None:
            print('⚠ 截图不存在，跳过：%s/%s{.jpg|.png|...}'
                  % (args.stage_dir, s))
            continue
        nodes.append(parse_node(s, path, ocr=not args.no_ocr,
                                banned=inp.get('banned') or {},
                                disabled=inp.get('disabled') or {}))
    if not nodes:
        print('无可用据点截图')
        return 2
    nodes, dedupe_notes = _dedupe_nodes(nodes)
    for x in dedupe_notes:
        print('【去重】%s' % x)

    slot_names, var_status = load_var_config()

    # ============ 整局模式：按面板阶段号分组，逐阶段顺序求解 ============
    if args.all:
        try:
            results, docs = plan_all_stages(
                fleet, inp, damaged, nodes, target=args.target,
                uses_max=inp['uses_max'], no_local=args.no_local,
                save_gold=not args.no_save_gold,
                boss_reserve=not args.no_boss_reserve,
                boss_rollback=args.boss_rollback,
                max_rollback=args.rollback_max)
        except ValueError as e:
            print('⚠ %s' % e)
            print('  （改用单阶段模式：把某阶段的截图放进单独目录后重跑，'
                  '或检查面板 OCR 的 --no-ocr 设置）')
            return 2
        stage_list = sorted(results)
        # 阶段总数以面板 OCR 为准（plan_all_stages 已按此口径写入 docs）
        total_ocr = docs[0]['stage']['total'] if docs else inp['total']
        if total_ocr != inp['total']:
            print('【阶段总数】以面板 OCR 为准：共 %d 个阶段'
                  '（玩家输入 stage.total 写的是 %d）'
                  % (total_ocr, inp['total']))
        print('【整局模式】共 %d 个阶段 · %d 张截图按面板阶段号分组：%s'
              % (total_ocr, len(nodes), '；'.join(
                  '阶段%d=%d个据点' % (s, len(results[s]['plan'])
                                      + len(results[s]['skipped']))
                  for s in stage_list)))
        print('【战损接力】%s' % carry_note)
        state = None if args.no_state else load_state(args.state)
        for st in stage_list:
            r = results[st]
            nodes_st = [n for n in nodes if n.get('stage') == st]
            inp_st = dict(inp)
            inp_st['current'] = st
            inp_st['total'] = total_ocr
            print('━' * 46)
            print('【阶段 %d】' % st)
            print(format_plan(r['plan'], r['pool'], r['remaining'], nodes_st,
                              inp_st, inp['uses_max'], r['uses_left'],
                              r['notes'], r['skipped'], r['var_status'],
                              target=args.target))
            if not args.no_state:
                used_ids = [c['id'] for p in r['plan'] for c in p['cards']]
                snap_start = {str(c['id']): int(v)
                              for c, v in
                              ((c, r['damaged_start'].get(c['id'],
                                int(c.get('hp', HP_MAX))))
                               for c in fleet)}
                snap = {str(st): {
                    'hp': snap_start,
                    'used': sorted(int(i) for i in used_ids),
                    'met': bool(r['plan']) and all(p['meets_target']
                                                   for p in r['plan']),
                    'nodes': [p['nodes']['name'] for p in r['plan']],
                    'target': args.target,
                }}
                save_state(args.state, fleet, r['damaged_start'], used_ids,
                           st, extra={'progress_tokens':
                                      sum(p['nodes']['progress_tokens']
                                          for p in r['plan'])},
                           snapshots=snap, base_state=state)
                state = load_state(args.state)
        if not args.no_state:
            rows = list_snapshots(load_state(args.state))
            print('【状态已保存】%s：%d 个阶段快照（%s）'
                  % (args.state, len(rows),
                     '、'.join('阶段%d' % k for k, *_ in rows)))
            print('  任一阶段不达标可回退重跑：--rollback-to N')
        if not args.no_out:
            all_doc = {'schema': 'plan_all/v1', 'stages': docs,
                       'target_rate': args.target}
            Path(args.out).write_text(
                json.dumps(all_doc, ensure_ascii=False, indent=2),
                encoding='utf-8')
            print('【结构化输出】%s（schema plan_all/v1，%d 个阶段）'
                  % (args.out, len(docs)))
        n_bad = sum(1 for s in stage_list
                    for p in results[s]['plan'] if not p['meets_target'])
        n_nodes = sum(len(results[s]['plan']) for s in stage_list)
        print()
        print('【整局汇总】%d 个阶段 · %d 个据点 · 未达标 %d 个'
              % (len(stage_list), n_nodes, n_bad))
        return 0

    inv, inv_src = stage_inventory(nodes, inp, slot_names)

    try:
        _require_powers(nodes)
    except ValueError as e:
        print('⚠ %s' % e)
        return 2

    # 单阶段模式能否判断「是否最终阶段」：以玩家输入的 current/total 为准
    # （单阶段截图里只有本阶段的节点，OCR 推不出整局总数）。
    plan, skipped, remaining, inv_left, uses_left, notes = stage_solve(
        pool, nodes, inv, target=args.target, uses_max=inp['uses_max'],
        save_gold=not args.no_save_gold,
        final_stage=(inp['current'] >= inp['total']),
        use_local_search=not args.no_local)
    note_inv = inv_source_note(inv_src, nodes)
    if note_inv:
        notes.insert(0, note_inv)

    text = format_plan(plan, pool, remaining, nodes, inp,
                       inp['uses_max'], uses_left, notes, skipped,
                       var_status, target=args.target)
    print('【战损接力】%s' % carry_note)
    print(text)

    if not args.no_out:
        doc = plan_to_json(inp, plan, remaining, inp['uses_max'], uses_left,
                           notes, target=args.target)
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False,
                                             indent=2), encoding='utf-8')
        print('【结构化输出】%s（schema %s）—— 可供图形面板 / 外部脚本使用'
              % (args.out, doc['schema']))

    if not args.no_state:
        used_ids = [c['id'] for p in plan for c in p['cards']]
        # 快照记的是**本阶段开始时**的 hp —— 回退到本阶段就是要回到这个状态。
        # 注意必须取自 damaged（已合并上一阶段战损），不能取 fleet 的原始 hp。
        snap_start = {str(c['id']): int(damaged.get(c['id'], HP_MAX))
                      for c in fleet}
        # 「快速维修」以修复后的血量参战 → 结算用修复后起点（快照仍记到达状态）
        damaged_battle = dict(damaged)
        for p in plan:
            for cid, hp_s in (p.get('repairs') or {}).items():
                damaged_battle[cid] = int(hp_s)
        snap = {str(inp['current']): {
            'hp': snap_start,
            'used': sorted(int(i) for i in used_ids),
            'met': bool(plan) and all(p['meets_target'] for p in plan),
            'nodes': [p['nodes']['name'] for p in plan],
            'target': args.target,
        }}
        hp = save_state(args.state, fleet, damaged_battle, used_ids,
                        inp['current'],
                        extra={'progress_tokens':
                               sum(p['nodes']['progress_tokens']
                                   for p in plan)},
                        snapshots=snap, base_state=state)
        n_dmg = sum(1 for v in hp.values() if v < HP_MAX)
        print()
        print('【状态已保存】%s：阶段 %d 结束后的生命值已记录'
              '（当前带伤 %d 张）—— 下一阶段会自动接力'
              % (args.state, inp['current'], n_dmg))
        print('【快照】已存 %d 份（阶段 %s）—— 若后续阶段达不到目标，'
              '可回退重跑：`--rollback-to N`'
              % (len(list_snapshots(load_state(args.state))),
                 '、'.join(str(k) for k, *_ in
                           list_snapshots(load_state(args.state)))))

        # 达不到目标 → 明确告诉用户可以从哪一步回退
        bad = [p for p in plan if not p['meets_target']] or skipped
        if bad:
            rows = list_snapshots(load_state(args.state))
            if rows:
                print()
                print('⚠ 本阶段有 %d 个据点未达目标 %.0f%%。若想换一种分卡思路，'
                      '可回退到更早的阶段重跑：'
                      % (len(bad), args.target * 100))
                for (k, n_loss, met, n_node) in rows:
                    print('     --rollback-to %-2d  （回到阶段 %d 开始时，'
                          '带伤 %d 张）' % (k, k, n_loss))
    return 0


if __name__ == '__main__':
    sys.exit(main())
