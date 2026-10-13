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
# 敌方侧属性：面板上写作「敌方战力 +8%」，与**我方**的「战力 −X%」是两回事
# （前者是敌方加成，后者是我方总战力削弱）。旧实现只按子串匹配 ATTRS，把它
# 读成「战力」并走我方全参数通道（m11.cond_all_pct）—— 当它落在「区域」段时
# 会把我方参数凭空虚增 8%。2026-10-13 拆成独立属性，只由 m11 按敌方口径处理。
ENEMY_ATTR = '敌方战力'
SCOPES = ('全场', '区域', '指定', '带入', '全域')
_PCT = re.compile(r'([+\-−]?\d+)\s*%')

_GLYPH_DIR = None
_GLYPH_TPL = None


SOLID_ARM_MIN = 0.40    # 实心度 ≥ 此值 → 实心族（旗形/箭头）
THIN_ARM_MAX = 0.25     # 实心度 ≤ 此值 → 细臂族（飞机机影）
GLYPH_MIN_SCORE = 0.40  # 图标匹配的最低 IoU（低于它算「认不出」→ 提示人工确认）
STRIPE_ROWS_MIN = 2     # 出现「2 道斜纹」的行数下限（见 _stripe_class）


def _solidity(mask):
    """形体「实心度」：3x3 结构元连续腐蚀两次后，剩余像素占形体的比例

    实心块（旗形/箭头）腐蚀两轮仍留大片核心（实测 0.43~0.61），细臂十字
    （飞机机影）的机翼/尾翼两轮就没了（实测 0.16~0.20）。
    ⚠ 必须在**原始像素尺度**上量：归一化到 24x24 时纵横比被拉伸，飞机的
    十字会被压成实心块，判据随即失效（实测归一化后 0.40，与旗形重叠）。
    """
    m = mask.astype(bool)
    n = int(m.sum())
    if not n:
        return 0.0
    for _ in range(2):
        e = m.copy()
        e[1:] &= m[:-1]
        e[:-1] &= m[1:]
        e[:, 1:] &= m[:, :-1]
        e[:, :-1] &= m[:, 1:]
        m = e
    return int(m.sum()) / n


def _arm_class(s):
    """按实心度分「形体族」→ 'solid' / 'thin' / None（落在过渡带，不判）"""
    if s >= SOLID_ARM_MIN:
        return 'solid'
    if s <= THIN_ARM_MAX:
        return 'thin'
    return None


def _stripe_class(mask):
    """旗形上的**内部斜纹条数** → 0 / 1 / 2（2 = 两道及以上）

    斜纹是旗形内部的浅色斜线，水平切过去就是「夹在两块前景之间的一段空白」，
    所以逐行数这种**内部空白段**、取各行最大值即可。实测（2026-10-13 玩家实拍
    + 全部入库样本，20 个 blob 逐个人工判读）：
      实心旗（驱逐舰）/ 飞机机影 → 0 道
      巡洋舰（1 道斜纹）        → 1 道
      战列舰（2 道斜纹）        → 2 道，且**有 3~4 行**都是 2 道
    ⚠ 为什么不能只靠 IoU：实测战列舰的实拍 blob（阶段3 据点7）巡洋舰 0.81 ＞
    战列舰 0.78 —— IoU 被旗形**轮廓**主导，1 道与 2 道斜纹只差 0.03，读错。
    故此处按条数当硬闸门；要求至少 `STRIPE_ROWS_MIN` 行出现 2 道，避免单个
    噪声孔把实心旗误判成双斜纹。
    """
    best, n_two = 0, 0
    for row in mask:
        runs, cur, n = [], row[0], 1
        for v in row[1:]:
            if v == cur:
                n += 1
            else:
                runs.append((cur, n)); cur, n = v, 1
        runs.append((cur, n))
        while runs and not runs[0][0]:      # 去掉首尾的边界空白（不属于旗形内部）
            runs.pop(0)
        while runs and not runs[-1][0]:
            runs.pop()
        gaps = sum(1 for v, _n in runs if not v)
        best = max(best, gaps)
        if gaps >= 2:
            n_two += 1
    if best >= 2 and n_two >= STRIPE_ROWS_MIN:
        return 2
    return best


def _glyph_templates():
    """禁用舰种图标模板（data/type_glyphs/ban_*.png）
    → {类型: [(24x24 的 0/1 掩码, 原始尺度实心度, 斜纹条数), ...]}

    每个类型可有**多个渲染变体**（文件名 ban_类型.png 与 ban_类型_2.png...），
    匹配取各变体的最高分。图标来源（2026-10-11 用户实拍确认）：
      ban_战列舰 —— 面板「禁用的单位类型」实拍裁剪；
      ban_飞机（+ _3 变体）—— 用户单独提供的禁用图标剪影 + 面板实拍。
    舰种旗（潜艇/驱逐舰/巡洋舰/战列舰/航空母舰 *.png）是**舰名旁的类型旗**
    另一套体系，其中战列舰旗与禁用图标同形 —— 因此**只在拿不到面板实拍时**
    才拿它反相推导模板（见下 ban_驱逐舰）。

    ⚠ 2026-10-12 补：原先只有**战列舰 / 飞机**两个模板 —— 面板上的「巡洋舰」
    禁用图标找不到对应模板，被硬配成**战列舰**（实测 mygame_s4 有 8 个据点全中）。
    现补 `ban_巡洋舰`（真值来自用户实拍：区域·禁用的单位类型 = 巡洋舰 + 飞机）
    与 `ban_飞机_3`（该面板渲染下的俯视机影，原 `ban_飞机` 与它只 0.27）；
    `ban_战列舰` 保留 —— mygame_s1 的实拍确实匹配它，两者可区分。

    ⚠ 2026-10-13 改名：`ban_飞机_2.png` 实为**巡洋舰**图标（用户指出），
    已 `git mv` 为 `ban_巡洋舰_2.png`。改名前后逐样本比对过：它在所有已入库
    样本里都不是最高分（<0.41，被同类型的其它变体盖住），所以当时没造成误判，
    但标签错了迟早会在别的面板渲染上把巡洋舰读成飞机。

    ⚠ 2026-10-13 补 `ban_驱逐舰`：「光板旗」（无斜纹）此前没有模板，被巡洋舰
    模板抢走（实测该 blob：巡洋舰 0.70 胜出，而实心度 0.59 说明它是实心旗）。
    新模板由**旗标反相推导**（`驱逐舰.png` 是深底浅形，反相后才是面板的浅底
    深形；紧裁后纵横比 20:9 与实拍 22:10 一致）。没用玩家实拍裁剪，因为那张
    截图只有 1280 宽、图标仅 15×7 像素，斜纹都已糊掉；反相推导那份更干净 ——
    对实拍 blob 得分 0.83（巡洋舰 0.70）。光板旗＝驱逐舰由用户确认。
    """
    global _GLYPH_DIR, _GLYPH_TPL
    if _GLYPH_TPL is not None:
        return _GLYPH_TPL
    _GLYPH_DIR = Path(DATA).parent / 'type_glyphs'
    out = {}
    for f in sorted(_GLYPH_DIR.glob('ban_*.png')):
        t = f.stem[4:].split('_')[0]
        a = np.array(Image.open(f).convert('L'))
        core = (a < 150).astype(np.uint8)
        im = Image.fromarray(core * 255).resize((24, 24), Image.LANCZOS)
        out.setdefault(t, []).append(
            ((np.array(im) > 100).astype(np.uint8), _solidity(core),
             _stripe_class(core)))
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


def _match_glyph(crop, tpls):
    """一个图标 blob → 最像的舰种，返回 (相似度, 类型)；都不够像 → None

    两层闸门先过滤模板、再比 IoU，都是「形状结构」上的硬判据，因为 IoU 在
    这两处都被外形轮廓主导、余量只有 0.03：
      · **形体族**：实心旗 vs 细臂十字（飞机机影）。拉伸归一化会把飞机的
        十字压成实心块、让实心模板赢（实测飞机的 20x12 机影：实心「驱逐舰」
        模板 0.56 ＞「飞机」0.53）；
      · **斜纹条数**（只对实心族生效）：0 / 1 / 2 道 —— 实测战列舰的实拍 blob
        巡洋舰 0.81 ＞ 战列舰 0.78（阶段3 据点7，玩家实拍）。
    族或条数落在过渡带时不设限，退回按全部模板比。
    """
    bcls = _arm_class(_solidity(crop))
    bstripe = _stripe_class(crop) if bcls == 'solid' else None
    best = None
    for t, variants in tpls.items():
        scores = [_glyph_score(crop, v) for v, s, st in variants
                  if (bcls is None or _arm_class(s) in (None, bcls))
                  and (bstripe is None or st == bstripe)]
        if not scores:
            continue
        s = max(scores)
        if s >= GLYPH_MIN_SCORE and (best is None or s > best[0]):
            best = (s, t)
    return best


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


def _read_ban_row(panel, cond, anchor):
    """**一行**「禁用的单位类型」→ (舰种列表, 提示, 该条目所属分区)

    图标**没有文字**，只能按图标识别：定位含「单位类型」的锚文本行，其**下方
    最后一段暗色图形**就是被禁舰种图标（1~2 枚）。行分组取**最后一组** ——
    锚文本与图标行之间有空隙，整带提取会把文本笔画混进来。
    scope = 该条目**上方最近的分区标题**（带入/区域/指定/全场/全域），见
    `_scope_above`；一个据点可以有多行（见 `_read_ban_rows`）。
    """
    tpls = _glyph_templates()
    if not tpls:
        return [], [], None
    bscope = _scope_above(cond, anchor['y'])
    H, W = panel.shape[:2]
    x0 = max(0, int(anchor.get('x', 0)) - 10)
    band = panel[int(anchor['y']) + 4: int(anchor['y']) + int(H * 0.05),
                 x0: min(W, x0 + int(W * 0.30))]
    dark = (to_gray(band) < 150).astype(np.uint8)
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
    # 每个 blob 对每个类型取**变体最高分**；再按分数降序做**互斥分配**
    # （被禁类型互不相同，高分 blob 先选）—— 避免两个 blob 抢同一类型、
    # 另一类型落空（实测「禁用战列舰+飞机」飞机被抢成战列舰）
    pairs = []
    n_blob = n_wide = 0
    for b in connected_boxes(dark[gy0:gy1], min_px=12):
        w, h = b[2] - b[0], b[3] - b[1]
        if not (6 <= w <= 45 and 5 <= h <= 35):
            # 过宽 = 两枚图标**粘在一起**了（面板上它们本就紧邻），不该静默丢掉
            n_wide += 1 if w > 45 else 0
            continue
        n_blob += 1
        crop = dark[gy0 + b[1]:gy0 + b[3], b[0]:b[2]]
        hit = _match_glyph(crop, tpls)      # 含「形体」「斜纹」两层闸门
        if hit:
            pairs.append((hit[0], b[0], hit[1], crop))
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
    elif n_blob or n_wide:
        # 认不出**必须出声**：静默跳过 = 玩家以为禁用了、程序照样上（实测就是
        # 这么被发现的）。过宽的那条尤其要提 —— 两枚图标粘在一起了。
        review.append('「禁用的单位类型」有图标但没认出舰种%s（%s 段）'
                      '—— 请照面板用 banned_types 补录'
                      % ('（有 %d 处图形过宽，疑似两枚图标粘连）' % n_wide
                         if n_wide else '',
                         bscope or '未读出作用域'))
    return sorted(set(out)), review, bscope


def _read_ban_glyphs(panel, cond):
    """兼容入口：只读**第一行** → (ban_types, review, ban_scope)

    生产路径已改走 `_read_ban_rows`（多行）；这个单行版本留给
    `parse()` 之外的调用与逐行诊断使用。
    """
    anchor = next((i for i in cond if '单位类型' in i['text']), None)
    if anchor is None:
        return [], [], None
    return _read_ban_row(panel, cond, anchor)


def _read_ban_rows(panel, cond):
    """面板上**每一行**「禁用的单位类型」→ ([{'scope','types'}], review)

    ⚠ 2026-10-13 修（玩家实拍：阶段3 据点7）：一个据点可以有**多行**禁用条目
    —— 带入 / 区域 / 指定 各挂一行，三行各自禁不同的舰种。原先只读**第一行**
    （那份实拍的第一行是「带入」段），于是**区域段的禁用根本没被看过**：
    玩家看到的就是「区域明明禁了战列舰和驱逐舰，程序照样上驱逐舰」。

    行归属仍是「上方最近的分区标题」，每行独立判断；**是否生效**由
    `ban_effective` 按「只认区域」口径决定。
    """
    rows, review = [], []
    for anchor in (i for i in cond if '单位类型' in i['text']):
        types, notes, sc = _read_ban_row(panel, cond, anchor)
        rows.append({'scope': sc, 'types': types})
        review.extend(notes)
    return rows, review


def ban_effective(rows):
    """多行禁用条目 → **实际生效**的舰种（只认「区域」段 / 未读出作用域）

    用户口径（同 m11._scope_region）：全胜后条件被移除，带入 / 指定 / 全场 /
    全域的禁用不会积累到头上 —— 只有「区域」（永久、仅本据点）拦人。
    """
    out = set()
    for r in rows or []:
        if r.get('scope') in (None, '区域'):
            out.update(r.get('types') or [])
    return sorted(out)


def ban_scope_label(rows):
    """多行禁用条目 → 兼容旧字段 `ban_scope` 的**单个**分区标签

    `ban_types` 只装生效的舰种，这个标签用来解释它们出自哪一段：有区域行就
    是「区域」；否则退回第一行的分区 —— 此时 `ban_types` 为空，标签只用于
    显示「第一行挂在带入段，所以不生效」。
    """
    rows = list(rows or [])
    if any(r.get('scope') in (None, '区域') for r in rows):
        return '区域'
    return rows[0].get('scope') if rows else None


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
    """对面板跑一次 OCR，返回 [{'y','x','h','w','text','conf'}]"""
    res, _ = _ocr_engine()(panel.astype(np.uint8))
    out = []
    for box, txt, conf in (res or []):
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append({
            'y': int(min(ys)),
            'x': int(min(xs)),
            'h': int(max(ys) - min(ys)),
            'w': int(max(xs) - min(xs)),
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


def attr_in_text(t):
    """条件行文字 → 属性名（含敌方侧「敌方战力」）；无则 None

    「敌方战力」必须**先判**：ATTRS 里的 '战力' 会按子串命中它。
    OCR 偶把「敌方战力」读成「敌战力 / 敌方战刀」（实测窄带 1x），
    故凡以「敌」开头且含「战力」者都算敌方侧 —— 既不让我方侧通道误收，
    也让 m11 能识别出这条显示加成。
    """
    tt = _norm(t).replace(' ', '')
    if ENEMY_ATTR in tt or (tt.startswith('敌') and '战力' in tt):
        return ENEMY_ATTR
    return next((a for a in ATTRS if a in tt), None)


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
        attr = attr_in_text(t)
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


def _rescue_missing_attrs(path, panel, cond):
    """条件行「属性名漏读」的逐行窄带重读（2026-10-13）

    整块条件区 OCR 会把「火力」读成「力」—— 尤见于玩家高分辨率截图被
    入口归一化到 1920 基准之后（实测 2270×931 的 1945 行动截图，
    区域·火力−24% 只读出「-24%」，属性名丢失）。丢名的那一行 m11
    既折不了、也不报警告以外的东西，等于**静默漏折算**（胜率偏乐观）。
    实测把该行**单独裁成窄带**、按**原始分辨率**放大 3x 再 OCR，即可稳定
    读回「火力」（整块重读做不到，与缩放无关，是行分组的问题）。

    兜底原则（宁缺勿错）：
      · 只在该行**本来就没有属性名**时用（绝不覆盖已读出的属性）；
      · 窄带取自百分比行自己 OCR 框的上下沿（不外扩到相邻行）；
      · 重读结果必须**只命中唯一一个属性**，否则放弃（交人工确认）。

    返回 (extra_pairs, notes)；extra_pairs 每条带 `_rescued: True`。
    """
    import numpy as np
    from PIL import Image

    pcts = [i for i in cond if _PCT.search(_norm(i['text']))]
    if not pcts:
        return [], []
    named = [i for i in cond if attr_in_text(i['text'])]
    ph = panel.shape[0]
    nat = load(path)
    npanel = nat[:, int(nat.shape[1] * PANEL_X):]
    sy = npanel.shape[0] / float(ph)
    # 用「两者中更大」的那一份：截图比 1920 基准大 → 原始分辨率更清晰；
    # 比基准小（实测 s5 为 1710 宽）→ 归一化后的那份才是放大过的。
    if sy >= 1.0:
        base, ymap, hmap = npanel, (lambda y: int(round(y * sy))), sy
    else:
        base, ymap, hmap = panel, (lambda y: int(y)), 1.0
    base = np.asarray(base).astype('uint8')
    pairs, notes = [], []
    for p in pcts:
        txt = _norm(p['text'])
        if '摧毁' in txt or '移除' in txt:
            continue                      # 指令行（「每摧毁…可移除 X%」）本就无属性名
        if any(abs(a['y'] - p['y']) <= 10 for a in named):
            continue                      # 本行已有属性名
        yn = ymap(p['y'])
        # 窄带几何：**固定 26px**（在 base 自己的像素尺度上，不再按 sy 放大），
        # 落点比该行 OCR 框顶再高 6px。实测高度是关键 —— 用 OCR 框自带高度
        # （实测 35px）或按 sy 放大 26px（实测 31px）都会把相邻行切进来，
        # 识别随即退化成「力」；正好 26px 才是稳定读出「火力」的那一档。
        band = base[max(0, yn - 6):min(base.shape[0], yn + 20), :]
        if band.size == 0:
            continue
        big = np.array(Image.fromarray(band).resize(
            (band.shape[1] * 3, band.shape[0] * 3), Image.LANCZOS))
        res, _ = _ocr_engine()(big)
        got = set()
        for _b, t2, cf in (res or []):
            if float(cf) < 0.6:
                continue
            a = attr_in_text(t2)
            if a:
                got.add(a)
        if len(got) != 1:
            continue                      # 读不出或多义 → 交人工确认
        m = _PCT.search(txt)
        if not m:
            continue
        attr = got.pop()
        sc = _scope_above(cond, p['y'])
        pairs.append({'attr': attr, 'pct': int(m.group(1)), 'scope': sc,
                      '_rescued': True})
        if sc in (None, '区域') or attr == ENEMY_ATTR:
            # 只有真正影响结果的补出才提示抽检：区域 / 未读出作用域 的行会折算
            # 进胜率；「敌方战力 ±X%」无论挂在哪一段都会参与基础值反推。
            # 带入 / 全域 / 指定 的普通属性行按口径不折算，补出来只为显示完整。
            notes.append('条件行属性名漏读，按逐行重读补出：%s %s%%（%s）'
                         '—— 建议抽检一次'
                         % (attr, m.group(1), sc or '未读出作用域'))
    return pairs, notes


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
    # 属性名漏读的行 → 逐行窄带重读兜底（2026-10-13；整块 OCR 会把
    # 「火力」读成「力」，实测 1945 行动的 区域·火力−24% 整条静默丢失）
    rescued, rescue_notes = _rescue_missing_attrs(path, panel, cond)
    if rescued:
        have = {(pr.get('attr'), pr.get('scope')) for pr in out['our_pairs']}
        for pr in rescued:
            if (pr['attr'], pr['scope']) not in have:
                out['our_pairs'].append(pr)
                have.add((pr['attr'], pr['scope']))
    out['rescued'] = [{'attr': pr['attr'], 'pct': pr['pct'],
                       'scope': pr['scope']} for pr in rescued]
    # 属性 + 百分比（attr 含「战力」= 总战力削弱，敌方侧「敌方战力」单列，
    # 由 m11 按敌方口径处理）
    # ⚠ 兼容字段优先取**第一个配对**：配对是「同一 OCR 行内」的属性+百分比，
    #   杜绝跨节错配。旧的单值路径（ctext 里第一个属性名 + 第一个百分比）两者
    #   可能来自不同分节 —— 实测 13-56-44 的「区域·增益效果 −100%｜全域·火力 −12%」
    #   被错配成「火力 −100%」，m11 据此折出幽灵削弱（4.21 #2）。
    if out['our_pairs']:
        out['attr'] = out['our_pairs'][0]['attr']
        out['pct'] = out['our_pairs'][0]['pct']
    else:
        out['attr'] = attr_in_text(ctext)
        mp = _PCT.search(ctext)
        out['pct'] = int(mp.group(1)) if mp else None
    out['scope'] = next((s for s in SCOPES if s in ctext), None)

    # 禁用舰种：「禁用的单位类型」以**图标**列出（无文字），按图标模板匹配
    # （与「禁用变项」ban_item 是两回事）。一个据点可有**多行**（带入/区域/
    # 指定各一行），逐行读、逐行归属分区；只有「区域」行拦人（4.28），
    # 但每行都记下来（`ban_rows`）供显示与人工核对。
    ban_rows, ban_review = _read_ban_rows(panel, cond)
    out['ban_rows'] = ban_rows
    out['ban_types'] = ban_effective(ban_rows)
    out['ban_scope'] = ban_scope_label(ban_rows)

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
    review.extend(rescue_notes)
    if out['has_seal'] and (out['attr'] or out['directive']):
        # 几何印章（虚线框）判定在「条件区没有深色高亮指令行」时会误报 ——
        # 实测 1945 行动的据点8~13（条件全挂在「带入」段、无深色指令行，
        # 暗像素分布稀疏 → 被判成印章）。读到条件文字时**以文字为准**，
        # 这里只提示抽检一次，不必当成冲突。
        review.append('几何印章判定与条件文字并存（无深色指令行时印章易误报；'
                      '本次以文字为准，建议抽检一次）')
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