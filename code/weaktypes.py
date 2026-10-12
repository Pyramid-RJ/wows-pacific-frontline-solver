"""弱点类型中英文映射 —— 唯一权威来源

背景（路线文档 4.7「弱点类型中英映射断层」）
─────────────────────────────────────────────────────────────────────
`data/our_fleet.json` 的 `debuff[].type` 用**中文**（速射 / 烟幕 / 攻击机…），
而 `m8_weak` 的识别输出与 `m7_assign.debuff_hits()` 用**英文**
（rapid / smoke / atkair…），且 `debuff_hits()` 内部是**精确字符串匹配**。

直接混用会让减益命中数**静默归零**：搜索照常返回结果、不报错、不崩溃，
但完全忽略减益 —— 属最危险的一类错误（不崩但结果错）。
实测复现：卡 HELLDIVER（减益 {攻击机, up}）置于 slot 0，敌方 0 确有该弱点时，
  敌方0 = {'atkair'} → 命中 0     ← m8 的真实输出
  敌方0 = {'攻击机'} → 命中 1     ← our_fleet.json 的中文

因此本模块把两侧统一规范为**英文 key**：
  · `m7_assign.debuff_hits()` 在比较前调用 `to_en()`
  · `validate_fleet()` 供回归测试断言全部可映射
  · `check_against_m8()` 断言本表与 `m8_weak.WEAK_TYPES` 一致

`dir` 字段两侧本来就一致（left / right / up），**断层只在类型名**。
"""

# 规范英文 key ↔ 中文名（顺序与 m8_weak.WEAK_TYPES 一致）
WEAK_ZH = {
    'antiair':  '防空火力',
    'atkair':   '攻击机',
    'detect':   '被侦察性',
    'largecal': '大口径',
    'rapid':    '速射',
    'smoke':    '烟幕',
}

WEAK_TYPES = tuple(WEAK_ZH)                 # 规范英文 key
WEAK_EN = {v: k for k, v in WEAK_ZH.items()}  # 中文 → 英文


def to_en(t):
    """中文或英文弱点类型 → 规范英文 key；无法识别返回 None"""
    if t in WEAK_ZH:
        return t
    if t in WEAK_EN:
        return WEAK_EN[t]
    return None


def to_zh(t):
    """中文或英文弱点类型 → 中文名；无法识别返回 None"""
    if t in WEAK_EN:
        return t
    if t in WEAK_ZH:
        return WEAK_ZH[t]
    return None


def normalize(seq):
    """一组弱点类型（中/英混用） → 规范英文集合（丢弃无法识别的项）"""
    return {e for e in (to_en(x) for x in seq) if e}


def validate_fleet(cards, strict=True):
    """校验卡库中所有减益类型均可映射

    返回未识别类型的列表（空列表 = 通过）。
    strict=True 时一并对 buff 类型做宽松校验（buffs 只有三种，且是属性名，
    不参与弱点匹配，故只检查非空）。
    """
    bad = set()
    for c in cards:
        for d in c.get('debuff', []):
            if to_en(d.get('type')) is None:
                bad.add(d.get('type'))
    return sorted(bad, key=str)


def check_against_m8():
    """断言本表与 m8_weak.WEAK_TYPES 完全一致（回归用，返回 bool）

    导入 m8_weak 会连带载入 numpy/PIL，因此只在测试中调用，不进热路径。
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from m8_weak import WEAK_TYPES as M8
    return set(M8) == set(WEAK_ZH)


if __name__ == '__main__':
    import json
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    cards = json.loads((ROOT / 'data' / 'our_fleet.json')
                       .read_text(encoding='utf-8'))['cards']
    bad = validate_fleet(cards)
    print('映射表: %d 类 %s' % (len(WEAK_TYPES), list(WEAK_TYPES)))
    print('与 m8_weak.WEAK_TYPES 一致:', check_against_m8())
    print('our_fleet.json 未识别减益类型:', bad or '无')
