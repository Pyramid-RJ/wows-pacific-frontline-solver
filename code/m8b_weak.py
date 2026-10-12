"""M3b 重做 —— 敌方卡头弱点图标：定位 + 分类（v2，取代 m8_weak 的 M3b 部分）

结论先说
─────────────────────────────────────────────────────────────────────
在人工标注集（`data/weak_labeled/`，5 图 / 20 卡 / **39 枚**）上：
    定位 **39/39**，分类 **39/39（100%）**，零混淆
参数为**宽平台**而非刀尖：lim 0.55~0.70 × thr 140~200 全部 100%，
归一化边长 20~48 全部 100%（见 `evaluate_labeled()` 的扫描）。

前一版为什么低（README 记 11/25 = 44%）
─────────────────────────────────────────────────────────────────────
README 把原因归为「符号倾斜 30~45°」。**实测否定该结论**：
把图标裁片放大后，符号是**正置**的，与 40×40 标准图标同形同向。
真正的原因是三处工程问题：

| # | 问题 | 现象 | 修法 |
| --- | --- | --- | --- |
| 1 | 卡片定位用了「顶部 42% 亮带 + 宽度 40~300px」 | 相邻卡连成 340px 超宽 run 被丢弃 → `4.png` 只切出 1 列 | 改用**底部文本条**亮列投影，卡宽恒 255px、间距 440px，5 图 100% 稳定 |
| 2 | 掩膜把徽章外环（含 4 处浅色缺口，rr≈0.83）圈进来 | 掩膜变成「环 + 符号」，形状被污染 | 径向只取 `rr ≤ 0.65`，并**只保留最大连通域** |
| 3 | 用**圆盘归一化**比较 | 查询 R≈26 vs 模板 R=19.5，符号占比被拉偏 | 改为**按符号 bbox 归一化**（形状对齐） |

规则（用户确认）
─────────────────────────────────────────────────────────────────────
**每张敌方卡有 1 或 2 个弱点 —— 至少 1 个，最多 2 个。**
因此定位不猜数量，只判定「槽 2 是否存在徽章」：
    槽 1 恒存在；槽 2 由圆环覆盖率决定（实测 2 枚卡 = 1.00，1 枚卡 = 0.08，
    阈值 0.5，间隔 6 倍以上，余量充足）。

几何（实测 5 张标注图，全部一致）
─────────────────────────────────────────────────────────────────────
    卡片宽 255px、间距 440px
    图标 1 中心 = 卡左 + 36      图标 2 中心 = 卡左 + 109
    徽章半径 ≈ 26（= 卡宽 × 0.102）
    徽章 = 深色外环（rr 0.88~1.0）+ 深底(rr<0.85, 灰度 ~110) + 白色符号(~220)
    战力数字在 卡左 + 184 之后（因此槽位不会撞到数字）
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PIL import Image

from m2_layout import (ROOT, light_mask, to_gray, connected_boxes, load)

LABELED_DIR = ROOT / 'data' / 'weak_labeled'
TRUTH = LABELED_DIR / 'truth.json'
LABELED_IMAGES = LABELED_DIR / 'images'
CLEAN_DIR = ROOT / 'data' / 'weak_ref_clean'

WEAK_TYPES = ['antiair', 'atkair', 'detect', 'largecal', 'rapid', 'smoke']
CLEAN_REF = {
    'detect':   ['A1_detect', 'B1_detect_big'],
    'antiair':  ['A2_antiair'],
    'rapid':    ['A3_rapid'],
    'atkair':   ['A4_atkair'],
    'largecal': ['A5_largecal'],
    'smoke':    ['A6_smoke'],
}

# ---- 卡片定位 ----
STRIP = (0.84, 0.99)      # 底部文本条所在纵向区间（占图高）
CARD_TH = 0.25            # 亮列投影阈值
CARD_MIN_W = 60

# ---- 几何（相对卡宽，标定于卡宽 255px）----
CARD_W_REF = 255.0
SLOT1_FRAC = 36 / CARD_W_REF      # 0.1412
SLOT2_FRAC = 109 / CARD_W_REF     # 0.4275
R_LO_FRAC, R_HI_FRAC = 0.085, 0.120
SLOT_TOL_FRAC = 8 / CARD_W_REF    # 中心搜索半宽 ±8px
CY_LO_FRAC, CY_HI_FRAC = 0.09, 0.33   # 徽章中心相对卡顶的纵向范围
PRESENCE_TH = 0.5         # 圆环覆盖率阈值（2 枚卡 1.00 / 1 枚卡 0.08）

# ---- 符号提取（宽平台实测：lim 0.55~0.65 × thr 140~200 全部 100%）----
# 取平台中部：lim 再大（≥0.70）时徽章外环的浅色缺口会漏入掩膜，
# 只有把阈值抬到 ≥170 才能压住 —— 故 lim 留在 0.60 最稳。
LIM = 0.60
THR = 175.0
SAT_MAX = 0.45
NORM_PX = 32

_TPL_CACHE = {}
_RING_NA = 32


# ============ 卡片定位 ============
def card_boxes(a):
    """底部文本条亮列投影 → [(x0, top, x1), ...]

    实测：5 张标注图全部切出 4 张卡，每张恒为 255px、间距 440px。
    旧的「顶部 42% 亮带」法在 4.png 只能切出 1 列（相邻卡连成 340px 超宽 run）。
    """
    H, W = a.shape[:2]
    lm = light_mask(a)
    cs = lm[int(H * STRIP[0]):int(H * STRIP[1])].mean(axis=0)
    runs, inr = [], False
    s = 0
    for x in range(W):
        if cs[x] > CARD_TH and not inr:
            inr, s = True, x
        elif cs[x] <= CARD_TH and inr:
            inr = False
            if x - s >= CARD_MIN_W:
                runs.append((s, x))
    if inr and W - s >= CARD_MIN_W:
        runs.append((s, W))
    out = []
    for (s, e) in runs:
        rows = lm[:, s:e].mean(axis=1)
        top = next((y for y in range(H) if rows[y] > 0.5), 0)
        out.append((s, top, e))
    return out


# ============ 整屏截图：敌方卡牌行裁剪 ============
# `card_boxes()` 靠「底部文本条」定位卡片，只适用于**已裁到卡牌行**的图
# （标注集那 5 张）。整屏截图的底部是我方卡组栏（同样是亮色），会误检，
# 因此整屏输入必须先裁出敌方卡牌行再交给它。
ROW_BAND = 0.45         # 卡牌行所在纵向区间（占图高，整屏截图实测）
ROW_BANDS = (0.45, 0.75, 1.00)   # 回退链：已裁好的卡牌行需退到全高
ROW_TH = 0.25           # 亮列占比阈值
ROW_MIN_FRAC = 0.05     # 单张卡最小宽度（占图宽）
CARD_HW = 1.51          # 卡高 / 卡宽（实测整屏 385/255、标注图 380/255）
TEXT_FRAC = 0.17        # 卡底文本条高度占卡高（`card_boxes` 取的是 0.84~0.99）


def _card_bottom(a):
    """卡名条（卡底亮色文本区）的底边 → 卡牌行底边

    比「卡顶 + 固定比例」稳：整屏截图、已裁卡牌行、任意尺度都适用。

    历史教训（2026-10-11，地图/部署界面实测）：按「下方首次大片青色」
    找卡底在地图界面全面失效 —— 卡行上方是青色地图、卡内海面蓝带与
    地图同色、且卡行下方的部署区里还夹着别的卡。**卡名条是每张敌卡
    唯一稳定位于卡底的亮色结构**：取 [0.10H, 0.60H] 内最后一个
    「亮行 + 随后 ≥15 行非亮」的行（= 最后一段亮带的底边）。
    战斗界面 = 卡名条；地图界面 = 卡名条；已裁卡牌行的标注图 = 图底。
    """
    H, W = a.shape[:2]
    lm = light_mask(a)
    lf = lm[:, :max(1, int(W * 0.65))].mean(axis=1)
    bot = H
    for y in range(int(H * 0.10), int(H * 0.60)):
        if lf[y] > 0.45 and lf[y + 1:y + 16].max() < 0.35:
            bot = y
    return bot


def _runs_in(lm, W, y0, y1):
    cs = lm[y0:max(y0 + 1, y1)].mean(axis=0)
    runs, inr, s = [], False, 0
    for x in range(W):
        if cs[x] > ROW_TH and not inr:
            inr, s = True, x
        elif cs[x] <= ROW_TH and inr:
            inr = False
            if x - s >= ROW_MIN_FRAC * W:
                runs.append((s, x))
    if inr and W - s >= ROW_MIN_FRAC * W:
        runs.append((s, W))
    return runs


def crop_card_row(a):
    """整屏截图 → 敌方卡牌行裁剪（保留卡底文本条）→ (crop, info)

    实测整屏截图的敌方卡牌行：卡宽 255px、间距 440px，卡高 ≈ 卡宽 × 1.51，
    卡底是米白文本区（承载舰名），正是 `card_boxes()` 需要的「底部文本条」。

    先用「卡底之下出现大片地图底」定位行底边、取其上方 17% 作文本条做列投影；
    若不成立（图已裁到卡牌行、下方没有地图底）再退到固定比例带 0.45/0.75/1.00。
    """
    H, W = a.shape[:2]
    lm = light_mask(a)
    bot = _card_bottom(a)
    ys = np.where(lm[:, 0:max(1, W // 4)].mean(axis=1) > ROW_TH)[0]
    top = int(ys[0]) if len(ys) else 0
    y0 = max(0, int(bot - TEXT_FRAC * max(1, bot - top)))

    runs = _runs_in(lm, W, y0, bot)
    if len(runs) < 4 and bot < H:
        runs = _runs_in(lm, W, y0, H)
    if len(runs) < 4:
        for band in ROW_BANDS:
            runs = _runs_in(lm, W, 0, int(H * band))
            if len(runs) >= 4:
                break
    if not runs:
        return None, {'runs': [], 'top': top, 'bot': bot, 'y0': y0}
    # 地图/部署界面（2026-10-11）：卡间掉落徽章（金盘+数字）也会在
    # 文本条投影里切出亮列区段，混进卡列 → 后续定位/分类全错。
    # 卡列宽度一致（±30% 以内），徽章明显更窄 —— 按众数宽度过滤。
    if len(runs) > 4:
        wmed = float(np.median([e - s for s, e in runs]))
        runs = [(s, e) for s, e in runs
                if abs((e - s) - wmed) <= 0.3 * wmed]
    runs = runs[:4]
    cw = int(round(float(np.median([e - s for s, e in runs]))))
    y1 = min(H, bot + 4)
    x0, x1 = max(0, runs[0][0] - 8), min(W, runs[-1][1] + 8)
    return a[0:y1, x0:x1], {'runs': runs, 'cw': cw, 'top': top, 'bot': bot,
                            'y0': y0, 'y1': y1, 'box': (x0, 0, x1, y1)}


def read_screen(path, tpl=None):
    """整屏截图主入口 → 与 `read_weaknesses()` 同形

    = 原生尺度读图 → 裁出敌方卡牌行 → **按卡宽归一化到标定尺度（255px）**
      → 复用标注集那套定位 + 分类。

    为什么按**卡宽**而不是按屏宽归一化：本模块的几何、圆环半径、
    模板都是按「卡宽 255px」标定的（标注集即此尺度）。若先按屏宽缩到 1920，
    卡宽会变成 207，符号随之变小，smoke↔largecal 的判别力明显下降
    （实测 kp2 第 4 卡会从正确的 largecal 翻成 smoke）。
    按卡宽归一化可让本模块**在任何分辨率下都跑在标定尺度上**。

    实测：玩家 2375×1061 的整屏截图，kp1/kp2/kp3 三张共 24 枚弱点全部判对
    （含「无烟幕弱点」的 kp2 —— 旧 `m8_weak` 会把大口径系统性误判成烟幕）。
    """
    a = load(str(path))
    crop, info = crop_card_row(a)
    if crop is None or crop.size == 0:
        return []
    cw = info.get('cw') or 0
    runs = [(s0 - info['box'][0], e0 - info['box'][0])
            for s0, e0 in info.get('runs') or []]
    native = crop                       # 原生分辨率（未缩放），供分类用
    if cw and abs(cw - CARD_W_REF) > 2:
        s = CARD_W_REF / float(cw)
        im = Image.fromarray(crop.astype(np.uint8)).resize(
            (max(1, int(round(crop.shape[1] * s))),
             max(1, int(round(crop.shape[0] * s)))), Image.LANCZOS)
        crop = np.array(im).astype(int)
        # runs 与 crop 同步缩放（否则 locate 在缩放后的图上找错列）
        runs = [(int(round(s0 * s)), int(round(e0 * s))) for s0, e0 in runs]
    else:
        s = 1.0
    tpl = tpl or load_templates()
    out = []
    for rec in locate_icons(crop, runs=runs):
        items = []
        for (cx, cy, R, ring) in rec['icons']:
            # 分类用**原生分辨率**图标 —— 上采样（如 232→255）会模糊符号，
            # 实测把 largecal 拉成 smoke（0.578 vs 0.540，2026-10-11 地图界面）
            ic = icon_crop(native if s != 1.0 else crop,
                           int(round(cx / s)), int(round(cy / s)),
                           int(round(R / s)))
            v = norm_symbol(symbol_mask(ic))
            cand = classify(v, tpl, top=3)
            t = cand[0][0] if cand else None
            sc = cand[0][1] if cand else 0.0
            m = (cand[0][1] - cand[1][1]) if len(cand) > 1 else 0.0
            items.append({'cx': cx, 'cy': cy, 'R': R, 'ring': round(ring, 3),
                          'type': t, 'score': round(float(sc), 3),
                          'margin': round(float(m), 3),
                          'needs_review': bool(t is None or sc < CONF_IOU
                                               or m < CONF_MARGIN),
                          'candidates': [(c, round(float(x), 3)) for c, x in cand]})
        out.append({'card': rec['card'], 'bbox': rec['bbox'],
                    'weak': [it['type'] for it in items], 'icons': items,
                    'row': info})
    return out


# ============ 徽章定位 ============
def _dark_ring(a):
    g = to_gray(a)
    r, b = a[:, :, 0].astype(int), a[:, :, 2].astype(int)
    return (g < 160) & (np.abs(r - b) < 55)


def _ring_cover(a, ring, cx, cy, R):
    """圆环角度覆盖率：半径 R-2/R/R+2 三圈共 NA 个采样点的命中比例"""
    ang = np.arange(_RING_NA) * (2 * np.pi / _RING_NA)
    hit = 0
    for rr in (R - 2, R, R + 2):
        ys = np.rint(cy + rr * np.sin(ang)).astype(int)
        xs = np.rint(cx + rr * np.cos(ang)).astype(int)
        ok = (ys >= 0) & (ys < a.shape[0]) & (xs >= 0) & (xs < a.shape[1])
        hit += int(ring[ys[ok], xs[ok]].sum())
    return hit / float(3 * _RING_NA)


def _best_badge(a, ring, cx0, cy0, cy1, card_w):
    """在给定窗口内找最优徽章 → (score, cx, cy, R)

    先粗搜（cx/cy 步长 2）再在峰值邻域精搜，避免全窗口逐像素 × 每个半径
    的暴搜（暴搜约 15 秒/批，粗精两级后降到 ~4 秒/批，结果一致）。
    """
    r_lo = max(6, int(round(card_w * R_LO_FRAC)))
    r_hi = max(r_lo + 1, int(round(card_w * R_HI_FRAC)))
    tol = max(4, int(round(card_w * SLOT_TOL_FRAC)))

    def scan(cxs, cys, rs):
        best = (0.0, cx0, cy0, r_lo)
        for cy in cys:
            for cx in cxs:
                for R in rs:
                    s = _ring_cover(a, ring, cx, cy, R)
                    if s > best[0]:
                        best = (s, cx, cy, R)
        return best

    b = scan(range(cx0 - tol, cx0 + tol + 1, 2), range(cy0, cy1, 2),
             range(r_lo, r_hi + 1))
    _, bx, by, bR = b
    b2 = scan(range(bx - 2, bx + 3), range(by - 2, by + 3),
              range(max(r_lo, bR - 1), min(r_hi, bR + 1) + 1))
    return b2 if b2[0] >= b[0] else b


def locate_icons(a, runs=None):
    """→ [{'card','bbox','icons':[(cx,cy,R,score)...]}]

    规则：每卡 1~2 枚符号。槽 1 恒存在；槽 2 由覆盖率判定。

    `runs`：crop_card_row 已验证过的卡列 x 区段（可选）。地图/部署界面
    （2026-10-11）的卡间掉落徽章会被 card_boxes 的文本条投影误检成
    第 5 张卡 → 全部错位；外部传入 runs 时跳过内部定位。
    """
    if runs:
        boxes = []
        for (s, e) in runs[:4]:
            rows = light_mask(a)[:, s:e].mean(axis=1)
            top = next((y for y in range(a.shape[0]) if rows[y] > 0.5), 0)
            boxes.append((s, top, e))
    else:
        boxes = card_boxes(a)
    ring = _dark_ring(a)
    out = []
    for ci, (x0, y0, x1) in enumerate(boxes):
        w = x1 - x0
        cy0 = int(y0 + w * CY_LO_FRAC)
        cy1 = int(y0 + w * CY_HI_FRAC)
        icons = []
        for fi, frac in ((0, SLOT1_FRAC), (1, SLOT2_FRAC)):
            cx0 = int(x0 + w * frac)
            s, cx, cy, R = _best_badge(a, ring, cx0, cy0, cy1, w)
            if fi == 1 and s < PRESENCE_TH:
                break                      # 只有 1 枚弱点
            if fi == 0 or s >= PRESENCE_TH:
                icons.append((cx, cy, R, s))
        out.append({'card': ci + 1, 'bbox': (x0, y0, x1), 'icons': icons})
    return out


# ============ 符号提取与归一化 ============
def icon_crop(a, cx, cy, R):
    y0, y1 = max(0, cy - R), min(a.shape[0], cy + R + 1)
    x0, x1 = max(0, cx - R), min(a.shape[1], cx + R + 1)
    return a[y0:y1, x0:x1]


def symbol_mask(sub, lim=LIM, thr=THR, sat_max=SAT_MAX):
    """徽章内部 → 白色符号掩膜（只保留最大连通域）

    必须只取 rr ≤ lim：徽章外环在 4 个方位有浅色缺口（rr≈0.83），
    否则掩膜会被污染成「环 + 符号」，形状判别失效。
    """
    if sub is None or sub.size == 0:
        return None
    g = to_gray(sub)
    h, w = g.shape
    ccx, ccy = (w - 1) / 2.0, (h - 1) / 2.0
    Rr = min(w, h) / 2.0
    yy, xx = np.mgrid[0:h, 0:w]
    rr = np.sqrt((xx - ccx) ** 2 + (yy - ccy) ** 2) / Rr
    inner = rr <= lim
    if inner.sum() < 30:
        return None
    mx = sub.max(axis=2).astype(float)
    mn = sub.min(axis=2).astype(float)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    m = ((g > thr) & (sat < sat_max) & inner).astype(np.uint8)
    if m.sum() == 0:
        return None
    boxes = connected_boxes(m, min_px=6)
    if boxes:
        b = max(boxes, key=lambda q: (q[2] - q[0]) * (q[3] - q[1]))
        k = np.zeros_like(m)
        k[b[1]:b[3], b[0]:b[2]] = m[b[1]:b[3], b[0]:b[2]]
        m = k
    return m if m.sum() >= 8 else None


def norm_symbol(m, size=NORM_PX):
    """按符号 bbox 等比归一化（形状对齐，去掉尺度与位置差异）

    实测：圆盘归一化只有 74.4%，换成 bbox 归一化后 **100%**
    —— 查询图标 R≈26 而模板 R=19.5，圆盘归一化会把符号占比拉偏。
    """
    if m is None:
        return None
    ys, xs = np.where(m > 0)
    if len(ys) == 0:
        return None
    c = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    ch, cw = c.shape
    sc = size / float(max(ch, cw))
    nh, nw = max(1, int(round(ch * sc))), max(1, int(round(cw * sc)))
    im = np.array(Image.fromarray(c).resize((nw, nh), Image.BILINEAR),
                  dtype=np.float32) / 255.0
    cv = np.zeros((size, size), np.float32)
    oy, ox = (size - nh) // 2, (size - nw) // 2
    cv[oy:oy + nh, ox:ox + nw] = im
    return (cv > 0.5).astype(np.float32)


def _iou(a, b):
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    u = float(np.maximum(a, b).sum())
    return float(np.minimum(a, b).sum()) / u if u else 0.0


# ============ 模板库 ============
def load_templates(lim=LIM, thr=THR, sat_max=SAT_MAX, size=NORM_PX):
    """从 40×40 / 64×64 标准图标建立模板（每类可多实例）

    缓存**按参数键**，否则先调默认参数后，再换参数会静默复用旧模板
    （参数扫描时曾因此得到假的「平台」结论）。
    """
    key = (round(lim, 4), round(thr, 3), round(sat_max, 4), size)
    if key in _TPL_CACHE:
        return _TPL_CACHE[key]
    tpl = {}
    for lab, names in CLEAN_REF.items():
        lst = []
        for nm in names:
            p = CLEAN_DIR / ('%s.png' % nm)
            if not p.exists():
                continue
            a = load(str(p))
            v = norm_symbol(symbol_mask(a, lim, thr, sat_max), size)
            if v is not None:
                lst.append(v)
        if lst:
            tpl[lab] = lst
    _TPL_CACHE[key] = tpl
    return tpl


def classify(v, tpl, top=3):
    """归一化掩膜 → [(类型, IoU), ...] 降序"""
    if v is None or not tpl:
        return []
    sc = sorted(((t, max(_iou(v, t2) for t2 in lst))
                 for t, lst in tpl.items()), key=lambda z: -z[1])
    return sc[:top]


CONF_IOU = 0.35        # top1 绝对下限（实测 39/39 的 top1 都远高于此）
CONF_MARGIN = 0.02     # top1−top2 下限


def read_weaknesses(path, tpl=None, with_candidates=True):
    """主入口：读一张截图 → 每张卡的弱点列表

    返回 [{'card', 'bbox', 'weak', 'icons'}, ...]
      icons: [{'cx','cy','R','ring','type','score','margin','needs_review',
               'candidates'}]
    与旧 m8_weak.read_weaknesses 同形，可直接替换。
    """
    tpl = tpl or load_templates()
    a = load(str(path))
    out = []
    for rec in locate_icons(a):
        items = []
        for (cx, cy, R, ring) in rec['icons']:
            v = norm_symbol(symbol_mask(icon_crop(a, cx, cy, R)))
            cand = classify(v, tpl, top=3) if with_candidates else []
            t = cand[0][0] if cand else None
            s = cand[0][1] if cand else 0.0
            m = (cand[0][1] - cand[1][1]) if len(cand) > 1 else 0.0
            items.append({'cx': cx, 'cy': cy, 'R': R, 'ring': round(ring, 3),
                          'type': t, 'score': round(float(s), 3),
                          'margin': round(float(m), 3),
                          'needs_review': bool(t is None or s < CONF_IOU
                                               or m < CONF_MARGIN),
                          'candidates': [(c, round(float(x), 3)) for c, x in cand]})
        out.append({'card': rec['card'], 'bbox': rec['bbox'],
                    'weak': [it['type'] for it in items], 'icons': items})
    return out


# ============ 标注集评估 ============
def load_truth():
    d = json.loads(TRUTH.read_text(encoding='utf-8'))
    return d['images'], d['classes']


def evaluate_labeled(lim=LIM, thr=THR, size=NORM_PX, verbose=False):
    """在人工标注集上评估

    返回 (卡级定位正确, 卡总数, 检出枚数, 真值枚数, 分类正确, 分类总数, 错例)
    """
    images, _ = load_truth()
    tpl = load_templates(lim, thr, SAT_MAX, size)
    card_ok = card_n = det = tot = cls_ok = cls_n = 0
    errs = []
    for name in sorted(images):
        a = load(str(LABELED_IMAGES / name))
        recs = locate_icons(a)
        for ci, lab in enumerate(images[name]):
            rec = recs[ci]
            card_n += 1
            tot += len(lab)
            det += len(rec['icons'])
            if len(rec['icons']) != len(lab):
                card_ok += 0
                errs.append('%s 卡%d 真值 %d 枚 → 检出 %d 枚'
                            % (name, ci + 1, len(lab), len(rec['icons'])))
            else:
                card_ok += 1
            for k, labk in enumerate(lab):
                if k >= len(rec['icons']):
                    errs.append('%s 卡%d 槽%d 真值 %s → 漏检'
                                % (name, ci + 1, k + 1, labk))
                    continue
                cx, cy, R, _ = rec['icons'][k]
                v = norm_symbol(symbol_mask(icon_crop(a, cx, cy, R), lim, thr))
                cand = classify(v, tpl, top=2)
                cls_n += 1
                pred = cand[0][0] if cand else None
                ok = (pred == labk)
                cls_ok += ok
                if verbose and not ok:
                    errs.append('%s 卡%d 槽%d 真值 %s → 预测 %s (IoU %.3f)'
                                % (name, ci + 1, k + 1, labk, pred,
                                   cand[0][1] if cand else 0))
    return card_ok, card_n, det, tot, cls_ok, cls_n, errs


if __name__ == '__main__':
    co, cn, det, tot, cco, ccn, errs = evaluate_labeled(verbose=True)
    print('标注集: 5 图 / %d 卡 / %d 枚弱点' % (cn, tot))
    print('  定位（卡级）%d/%d    检出枚数 %d / 真值 %d' % (co, cn, det, tot))
    print('  分类 %d/%d (%.1f%%)' % (cco, ccn, 100.0 * cco / max(1, ccn)))
    for e in errs:
        print('   !', e)
