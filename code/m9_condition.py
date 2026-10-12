"""战斗条件面板识别（M3c）

版式（实测 1920×786~830，面板左边界 x=0.772W，宽约 438px）
┌──────────────┬──────────────┐
│ 阶段/目标/星级  │              │
├──────────────┤              │
│ 战斗条件        │  ← y≈0.285H 起
│ 区域/指定/带入   │  ← 作用域子标题
│ [属性行] [−24%] │  ← 属性与已结算百分比
├──────────────┤              │
│ 印章区 / 按钮   │  ← y≈0.875H 止
└──────────────┴──────────────┘

设计取舍（经 OCR 方案实测评估后确定）
────────────────────────────────────────
游戏内中文渲染体（尤楷/衬线）导致通用 OCR 在印章上必然失败：
实测 RapidOCR 把「无战斗条件」读成「无战斗条」/「PACIHU」，置信度仅 0.6~0.75。
但我们**只需要印章的存在性**，不需要读出它的文字 —— 印章是虚线方框，
几何特征极其鲜明，因此不走 OCR，改用几何判定：

    印章 = 倾斜虚线矩形 → bbox 内暗像素**稀疏**（填充率 0.052~0.055）
    文字 = 实心字形     → bbox 内暗像素**稠密**（填充率 0.191~0.253）

两组间隔 3.5 倍，阈值取 0.12，14 张样本全对，且 0.10~0.18 任意取值均 14/14，
容错区间极宽。详见 .work/seal_geom.py。

其余语义字段（阶段号/据点号/难度/属性名/百分比/指令/禁用变项）
为固定词表 + 数字，用 OCR 读取，实测合计 62/63 = 98.4%（唯一 1 例经查为标注错误）。
"""
import os
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA = str(ROOT / 'data' / 'stage')

import numpy as np
from PIL import Image
from m2_layout import load, load_screen, to_gray

# ---- 版式常量 ----
PANEL_X = 0.772        # 面板左边界占宽比（实测 1482/1920 = 0.772）
EDGE_PAD = 6           # 去掉面板左缘青色装饰条
COND_Y0 = 0.285        # 条件区上界（占高）
COND_Y1 = 0.875        # 条件区下界
DARK_THR = 110         # 暗像素灰度阈值
NEUTRAL_TOL = 26       # 中性判据：排除青色（b 明显高于 r）
SEAL_FILL_MAX = 0.12   # 印章填充率上限（几何判定）

DIFFS = ('普通', '简单', '困难')
ATTRS = ('存活性', '火力', '机动性', '被侦察性', '战力',
         '增益效果', '减益效果')
SCOPES = ('全场', '区域', '指定', '带入', '全域')
_PCT = re.compile(r'([+\-−]?\d+)\s*%')

_GLYPH_DIR = None
_GLYPH_TPL = None


def _glyph_templates():
    """禁用舰种图标模板（data/type_glyphs/ban_*.png）→ {类型: [24x24 0/1]}

    每个类型可有**多个渲染变体**（文件名 ban_类型.png 与 ban_类型_2.png...），
    匹配取各变体的最高分。图标来源（2026-10-11 用户实拍确认）：
      ban_战列舰 —— 面板「禁用的单位类型」实拍裁剪；
      ban_飞机（+ _2 变体）—— 用户单独提供的禁用图标剪影 + 面板实拍。
    舰种旗（潜艇/驱逐舰/巡洋舰/战列舰/航空母舰 *.png）是**舰名旁的类型旗**
    另一套体系，其中战列舰旗与禁用图标同形，此处不用作匹配源。

    ⚠ 2026-10-12 补：原先只有**战列舰 / 飞机**两个模板 —— 面板上的「巡洋舰」
    禁用图标找不到对应模板，被硬配成**战列舰**（实测 mygame_s4 有 8 个据点全中）。
    现补 `ban_巡洋舰`（真值来自用户实拍：区域·禁用的单位类型 = 巡洋舰 + 飞机）
    与 `ban_飞机_3`（该面板渲染下的俯视机影，原 `ban_飞机` 与它只 0.27）；
    `ban_战列舰` 保留 —— mygame_s1 的实拍确实匹配它，两者可区分。
    """
    global _GLYPH_DIR, _GLYPH_TPL
    if _GLYPH_TPL is not None:
        return _GLYPH_TPL
    _GLYPH_DIR = Path(DATA).parent / 'type_glyphs'
    out = {}
    for f in sorted(_GLYPH_DIR.glob('ban_*.png')):
        t = f.stem[4:].split('_')[0]
        a = np.array(Image.open(f).convert('L'))
        im = Image.fromarray((a < 150).astype(np.uint8) * 255).resize(
            (24, 24), Image.LANCZOS)
        out.setdefault(t, []).append((np.array(im) > 100).astype(np.uint8))
    _GLYPH_TPL = out
    return out


def _glyph_score(crop, tpl):
    """两幅 0/1 小图的相似度：统一 24x24 后取 IoU"""
    from PIL import Image
    im = Image.fromarray(crop.astype(np.uint8) * 255).resize(
        (24, 24), Image.LANCZOS)
    b = (np.array(im) > 100).astype(np.uint8)
    inter = int((b & tpl).sum())
    union = int((b | tpl).sum())
    return inter / union if union else 0.0


def _scope_above(cond, y):
    """某条目**上方最近的作用域标题行** → 该条目所属分区（无则 None）

    条件区是「分区标题 + 该分区下的条目」的两级结构。带文字的条目由
    `_attr_pairs` 按标题分节归属；而「禁用的单位类型」的图标行**没有文字**，
    只能按**上方最近的分区标题**归属。
    ⚠ 4.28：原先禁用条目用**节点级** scope 判定，导致同一分区语义行为不一致
    （带入段的禁用被当区域生效）。
    """
    best = None
    for it in cond:
        t = _norm(it['text']).replace(' ', '')
        if t in SCOPES and it['y'] < y and (best is None or it['y'] > best[1]):
            best = (t, it['y'])
    return best[0] if best else None


def _read_ban_glyphs(panel, cond):
    """「禁用的单位类型」→ 图标匹配出被禁舰种 + 该条目所属分区

    图标**没有文字**，只能按图标识别：定位条件区里含「单位类型」的
    锚文本行，其下方的暗色小图形即被禁舰种图标（1~2 枚）。
    返回 (ban_types, review_notes, ban_scope)。识别不出/相似度不足 → review。
    ban_scope = 该条目上方最近的分区标题（带入/区域/指定/全场/全域）。
    """
    tpls = _glyph_templates()
    if not tpls:
        return [], [], None
    anchor = next((i for i in cond if '单位类型' in i['text']), None)
    if anchor is None:
        return [], [], None
    bscope = _scope_above(cond, anchor['y'])
    H, W = panel.shape[:2]
    x0 = max(0, int(anchor.get('x', 0)) - 10)
    band = panel[int(anchor['y']) + 4: int(anchor['y']) + int(H * 0.05),
                 x0: min(W, x0 + int(W * 0.30))]
    dark = (to_gray(band) < 150).astype(np.uint8)
    # 行分组取**最后一组**：锚文本（「禁用的单位类型」）与图标行之间有
    # 空隙，直接整带提取会把文本笔画混进来
    rowhas = dark.sum(axis=1) > 0
    groups, cur = [], None
    for i, v in enumerate(rowhas):
        if v and cur is None:
            cur = i
        elif not v and cur is not None:
            groups.append((cur, i))
            cur = None
    if cur is not None:
        groups.append((cur, len(rowhas)))
    if not groups:
        return [], [], bscope
    gy0, gy1 = groups[-1]
    from m2_layout import connected_boxes
    boxes = connected_boxes(dark[gy0:gy1], min_px=12)
    # 每个 blob 对每个类型取**变体最高分**；再按分数降序做**互斥分配**
    # （被禁类型互不相同，高分 blob 先选）—— 避免两个 blob 抢同一类型、
    # 另一类型落空（实测「禁用战列舰+飞机」飞机被抢成战列舰）
    pairs = []
    for b in boxes:
        w, h = b[2] - b[0], b[3] - b[1]
        if not (6 <= w <= 45 and 5 <= h <= 35):
            continue
        crop = dark[gy0 + b[1]:gy0 + b[3], b[0]:b[2]]
        for t, variants in tpls.items():
            best_v = max(_glyph_score(crop, v) for v in variants)
            if best_v >= 0.40:
                pairs.append((best_v, b[0], t, crop))
    out, review = [], []
    used_t, used_b = set(), set()
    for s, bx, t, crop in sorted(pairs, reverse=True):
        if t in used_t or bx in used_b:
            continue
        used_t.add(t)
        used_b.add(bx)
        out.append(t)
    if out:
        review.append('禁用舰种来自图标匹配（%s）%s，建议抽检一次'
                      % ('、'.join(sorted(set(out))),
                         '（%s 段）' % bscope if bscope else ''))
    return sorted(set(out)), review, bscope

_OCR = None


def _ocr_engine():
    """RapidOCR 单例（首次调用约 1~2s 载入模型，之后每图 0.8s）"""
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


# ---------- 几何层：面板与印章 ----------
def panel_crop(a):
    """裁出右侧面板"""
    H, W = a.shape[:2]
    return a[:, int(W * PANEL_X):]


def _cond_mask(panel):
    """条件区中性暗像素掩膜（排除青色装饰与红色面板）"""
    g = to_gray(panel)
    r, b = panel[:, :, 0], panel[:, :, 2]
    gg = panel[:, :, 1]
    neutral = (abs(r - b) < NEUTRAL_TOL) & (abs(gg - b) < NEUTRAL_TOL)
    H = panel.shape[0]
    ya, yb = int(H * COND_Y0), int(H * COND_Y1)
    return (g[ya:yb] < DARK_THR) & neutral[ya:yb]


def seal_geometry(panel):
    """几何判定印章

    返回 (has_seal, fill_ratio, evidence)
    印章 = 倾斜虚线矩形，bbox 内暗像素稀疏（填充率 <0.12）
    文字 = 实心字形，填充率 >0.19
    """
    m = _cond_mask(panel[:, EDGE_PAD:])
    ys, xs = np.where(m)
    if not len(ys):
        return False, 0.0, {'dark_px': 0}
    bw = int(xs.max() - xs.min() + 1)
    bh = int(ys.max() - ys.min() + 1)
    fill = len(ys) / float(bw * bh)
    has = fill < SEAL_FILL_MAX
    return has, fill, {'dark_px': int(len(ys)), 'bbox': (bw, bh),
                       'neutral': True}


# ---------- 语义层：OCR ----------
def _read_panel(panel):
    """对面板跑一次 OCR，返回 [{'y','x','text','conf'}]"""
    res, _ = _ocr_engine()(panel.astype(np.uint8))
    out = []
    for box, txt, conf in (res or []):
        out.append({
            'y': int(min(p[1] for p in box)),
            'x': int(min(p[0] for p in box)),
            'text': txt.strip(),
            'conf': float(conf),
        })
    return out


def _norm(t):
    """归一化：OCR 常把减号/空格读成杂字符"""
    return (t.replace(' ', '').replace('−', '-')
             .replace('—', '-').replace('：', ':'))


def parse_semantics(items):
    """从 OCR 结果解析语义字段"""
    norm = [_norm(i['text']) for i in items]
    joined = ' '.join(norm)
    flat = joined.replace(' ', '')
    res = {'items': items}

    m = re.search(r'阶段(\d+)', flat)
    res['stage'] = int(m.group(1)) if m else None

    m = re.search(r'关键据点(\d+)', flat)
    res['node'] = int(m.group(1)) if m else None

    res['difficulty'] = next((d for d in DIFFS if d in joined), None)

    # ---- M3d：阶段面板字段 ----

    # 阶段目标：「阶段目标：摧毁8个单位」/「主要目标：妙高级」
    # 注意「请先完成上个阶段的目标」表示**该阶段尚未解锁**，
    # 此时面板不显示具体目标，属正常状态而非识别失败。
    goal = None
    locked = ('请先完成上个阶段的目标' in joined) or ('进行中' not in joined
                                                  and '主要目标' not in joined
                                                  and '阶段目标' not in joined)
    mg = re.search(r'(?:阶段目标|主要目标)[:：]\s*(\S+?)(\s|$)', joined)
    if mg:
        goal = mg.group(1)
    else:
        mfull = re.search(r'(摧毁\s*\d+\s*个单位)', flat)
        if mfull:
            goal = mfull.group(1)
        elif '主要目标' in joined:
            goal = '主要目标'
    res['goal'] = goal
    res['locked'] = bool(locked)   # 阶段未解锁（面板提示先完成上个阶段）

    # 星级：★ 的个数。面板上有多个据点格，每格一组星。
    # 当前据点 = 星数与「据点序号 N」数字相邻（|Δx|、|Δy| 小）的那组 ——
    # 实测 s2_kp4：面板同时显示据点4 ★★ 与据点5 ★★★ 两个标签，
    # 旧实现取全面板 max 会把当前据点 2 星读成 3（真值标注时发现，2026-10-11）。
    stars_all = []
    for i in items:
        t = i['text']
        n = t.count('★')
        if n:
            # 「2★」形式：星前数字为据点序号
            mnum = re.match(r'^(\d+)\s*★', t)
            stars_all.append({
                'x': i['x'], 'y': i['y'], 'count': n,
                'index': int(mnum.group(1)) if mnum else None,
            })
    res['stars_all'] = stars_all
    star_cur = None
    if stars_all and res.get('node') is not None:
        for i in items:
            t = i['text'].strip()
            if t.isdigit() and int(t) == res['node']:
                near = [s['count'] for s in stars_all
                        if abs(s['x'] - i['x']) < 60
                        and abs(s['y'] - i['y']) < 45]
                if near:
                    star_cur = near[0]
                    break
    res['star_max'] = star_cur if star_cur is not None else \
        max((s['count'] for s in stars_all), default=None)

    return res, joined


def _region_items(items, panel_h):
    """只保留条件区内的 OCR 行"""
    ya, yb = panel_h * COND_Y0, panel_h * COND_Y1
    return [i for i in items if ya <= i['y'] <= yb]


def _attr_pairs(cond_items):
    """条件区 OCR 行 → [{'attr','pct','scope'}] 逐行配对（多行属性削弱）

    一个分节可能挂**多条**「属性 −X%」（实测 PixPin_22-00-31 的 区域 段
    同时有「存活性 −12%」与「机动性 −12%」）。旧的单 attr/pct 只留一条，
    其余被静默吞掉。这里按作用域标题行分节、逐行配对，全部产出；
    **作用域闸门不在这里** —— 哪条生效由 m11 的 cond_pct 按 pair 的
    scope 判定（只有 区域 / 未读出 折算，与 _cond_active 口径一致）。
    百分比若在属性行读不到，取同一行带（y ±8）里的百分比
    （数值常右侧对齐、单独成行）。
    """
    rows = sorted(cond_items, key=lambda i: (i['y'], i['x']))
    scope, pairs = None, []
    for it in rows:
        t = _norm(it['text']).replace(' ', '')
        if not t:
            continue
        if t in SCOPES:
            scope = t
            continue
        attr = next((a for a in ATTRS if a in t), None)
        if attr is None:
            continue
        m = _PCT.search(t)
        p = int(m.group(1)) if m else None
        if p is None:
            for it2 in rows:
                if abs(it2['y'] - it['y']) > 8:
                    continue
                m2 = _PCT.search(_norm(it2['text']))
                if m2:
                    p = int(m2.group(1))
                    break
        if p is not None:
            pairs.append({'attr': attr, 'pct': p, 'scope': scope})
    return pairs


def parse(path, ocr=True):
    """解析一张截图的阶段面板

    返回 dict：
      stage/ node / difficulty      阶段面板字段
      has_seal / seal_fill          几何印章判定
      pct / attr / scope            战斗条件（属性类）
      directive                    战斗条件（指令类原文）
      ban_item                     是否禁用变项
      needs_review                 需要人工确认的字段列表
    """
    a = load_screen(path)      # 入口尺度归一化（面板虽然用比例坐标，
                               # 但 OCR 与印章判据的像素阈值同样按 1920 标定）
    panel = panel_crop(a)
    ph = panel.shape[0]
    out = {'file': os.path.basename(path)}

    # 几何层（始终执行）
    has_seal, fill, ev = seal_geometry(panel)
    out['has_seal'] = has_seal
    out['seal_fill'] = round(fill, 4)
    out['seal_evidence'] = ev

    if not ocr:
        return out

    items = _read_panel(panel)
    sem, joined = parse_semantics(items)
    out.update(sem)
    out['panel_h'] = ph

    # 条件区内的行
    cond = _region_items(items, ph)
    ctext = ' '.join(_norm(i['text']) for i in cond)
    cflat = ctext.replace(' ', '')
    out['cond_text'] = ctext

    # 条件区小字 2x 重读（2026-10-11）：整版 1x OCR 偶发漏读属性名
    # （实测「火力」→「力」，据点2 区域·火力−24% → attr=None 无法折算）。
    # 对条件区单独放大 2x 再 OCR 一次，文本并入 ctext —— 属性/百分比/
    # 作用域/禁用的判定自动受益；重复读出的内容无害。
    try:
        from PIL import Image
        y0, y1 = int(ph * COND_Y0), int(ph * COND_Y1)
        reg = panel[y0:y1].astype('uint8')
        big = np.array(Image.fromarray(reg).resize(
            (reg.shape[1] * 2, reg.shape[0] * 2), Image.LANCZOS))
        res2 = _ocr_engine()(big)
        extra = ' '.join(_norm(t) for _bb, t, _c in res2[0])
        if extra:
            ctext = (ctext + ' ' + extra).strip()
            cflat = ctext.replace(' ', '')
            out['cond_text'] = ctext
    except Exception:
        pass                    # 2x 重读是增强，失败时退回 1x 结果

    # 多行属性削弱：全部 (attr, pct, scope) 配对（按作用域标题行分节、逐行配对）
    out['our_pairs'] = _attr_pairs(cond)
    # 属性 + 百分比（attr 含「战力」= 总战力削弱，由 m11 按全参数折算）
    # ⚠ 兼容字段优先取**第一个配对**：配对是「同一 OCR 行内」的属性+百分比，
    #   杜绝跨节错配。旧的单值路径（ctext 里第一个属性名 + 第一个百分比）两者
    #   可能来自不同分节 —— 实测 13-56-44 的「区域·增益效果 −100%｜全域·火力 −12%」
    #   被错配成「火力 −100%」，m11 据此折出幽灵削弱（4.21 #2）。
    if out['our_pairs']:
        out['attr'] = out['our_pairs'][0]['attr']
        out['pct'] = out['our_pairs'][0]['pct']
    else:
        out['attr'] = next((s for s in ATTRS if s in ctext), None)
        mp = _PCT.search(ctext)
        out['pct'] = int(mp.group(1)) if mp else None
    out['scope'] = next((s for s in SCOPES if s in ctext), None)

    # 禁用舰种：「禁用的单位类型」以**图标**列出（无文字），按图标模板匹配
    # （与「禁用变项」ban_item 是两回事）。**同时返回它所属的分区** ——
    # 只有「区域」段的禁用才生效（4.28）。
    bans, ban_review, ban_scope = _read_ban_glyphs(panel, cond)
    out['ban_types'] = bans
    out['ban_scope'] = ban_scope

    # 指令类条件（如「摧毁3个敌方单位以移除战斗条件」）
    if '摧毁' in ctext or '移除战斗条件' in cflat:
        _d = next(
            (i['text'] for i in cond if '摧毁' in _norm(i['text'])), None)
        # OCR 常把左侧的「禁止/失败」图标读成孤立的 X/×/✕，去掉它
        if _d:
            _d = re.sub(r'^[Xx×✕✗Χ]\s*', '', _d).strip()
        out['directive'] = _d
    else:
        out['directive'] = None
    out['ban_item'] = '禁用变项' in ctext

    # 冲突校验：几何说有印章，OCR 却读到属性/指令 → 需人工确认
    review = []
    review.extend(ban_review)
    if out['has_seal'] and (out['attr'] or out['directive']):
        review.append('印章与条件文字冲突')
    if not out['has_seal'] and not (out['attr'] or out['directive']
                                    or out['ban_item']):
        review.append('无印章但也无条件文字')
    if out['attr'] and out['pct'] is None:
        review.append('有属性但未读到百分比')
    if out['attr'] is None and out['pct'] is not None:
        # 实测（PixPin_17-54-24 带入条件）：OCR 漏读属性名只留 "-24%"
        # → 不知道削弱哪项参数，无法折算进胜率
        review.append('有百分比但未读到属性名（未计入胜率，需人工确认）')
    if out['stage'] is None or out['node'] is None:
        review.append('阶段号或据点号缺失')
    if out['goal'] is None and not out['locked']:
        review.append('阶段目标缺失')
    out['needs_review'] = review
    return out


if __name__ == '__main__':
    FILES = ['kp1', 'kp2', 'kp3', 'boss', 'ex_kp1', 'ex_kp2', 'ex_kp3',
             'n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'n7']
    for f in FILES:
        r = parse(DATA + '/%s.jpg' % f)
        seal = '印章(无)' if r['has_seal'] else '有条件'
        goal = r.get('goal') or ('未解锁' if r.get('locked') else '-')
        print('%-7s %s 阶段%s 据点%s %-4s 星级%s 目标=%-12s 属性=%s %s%% 禁变项=%s'
              % (f, seal, r.get('stage'), r.get('node'),
                 r.get('difficulty') or '-', r.get('star_max') or '-',
                 goal[:12],
                 r.get('attr') or '-',
                 r.get('pct') if r.get('pct') is not None else '-',
                 r.get('ban_item')))
        if r['needs_review']:
            print('        待确认: %s' % ' / '.join(r['needs_review']))