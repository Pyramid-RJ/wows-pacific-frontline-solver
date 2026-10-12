"""m8v: 弱点图标识别 v2（推倒重来）—— **已被 `m8b_weak.py` 取代，本文件仅存档**

⚠ 状态：**superseded**。M3b 已于 2026-10-10 由 `code/m8b_weak.py` 重做完成，
在人工标注集（5 图 / 20 卡 / 39 枚）上 **定位 20/20、检出 39/39、分类 39/39 = 100%**。
本文件当时只做到「自适应分割」这一处改动且处于暂停状态，保留作过程记录，
新工作请直接用 `m8b_weak.py`（回归见 `code/test_m3b.py`）。

当时的诊断（部分已被推翻）：把失败归因于「固定 thr 削掉抗锯齿边缘」，
但重做时实测真正的三处原因分别是卡片定位、徽章外环污染掩膜、圆盘归一化，
与阈值策略关系不大 —— 详见 `data/weak_labeled/README.md`。

动机
────────────────────────────────────────────────────────────────────
前一版 `m8_weak` 在固定参数（thr=180、lim=0.44）下：
    · 实战截图 real2/real3 15/16
    · 人工标注集仅 11/25（44%），且出现两个**系统性**混淆：
        大口径 → 烟雾（4次，IoU 0.250）
        速射→ 攻击机（4 次，IoU 0.253，margin 仅 0.011）
      margin极小说明「很自信地错了」——不是分辨率不足，是特征提取有偏。

判断：固定 thr 把符号的抗锯齿边缘整圈削掉，符号变成无结构的团块，
于是相近的两类（大口径 / 烟雾都是斜置管状）无法区分。

v2 的做法
────────────────────────────────────────────────────────────────────
**自适应分割**替代固定阈值：
  1. 只统计内底区域（rr ≤ INNER_LIM）的灰度直方图
  2. Otsu 得内底/符号分界 `t0`，再取 `thr = t0 - EDGE_KEEP`
     （EDGE_KEEP>0 表示把抗锯齿过渡带纳入，这正是 v1 丢掉的部分）
  3. 饱和度门限放宽为软约束（仅剔除明显彩色像素）

评估
────────────────────────────────────────────────────────────────────
真值来自 data/weak_labeled/（用户逐枚人工标注，5 张截图共 39 枚），
真值文件 truth.json、截图 images/，说明见该目录 README.md。

**当前状态：暂停**（2026-10-10）。
已确认的阻塞点见 README「遗留问题」：定位对不同卡片布局不稳定、
符号倾斜角度与模板不一致。恢复时应先解决定位，不要再扫分割参数。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np
from PIL import Image

from m2_layout import ROOT, otsu_threshold, to_gray

LABELED_DIR = ROOT / 'data' / 'weak_labeled'
TRUTH = LABELED_DIR / 'truth.json'
LABELED_IMAGES = LABELED_DIR / 'images'
CLEAN_DIR = ROOT / 'data' / 'weak_ref_clean'

WEAK_TYPES = ['antiair', 'atkair', 'detect', 'largecal', 'rapid', 'smoke']
CLEAN_REF = {
    'detect': ['A1_detect', 'B1_detect_big'],
    'antiair': ['A2_antiair'],
    'rapid': ['A3_rapid'],
    'atkair': ['A4_atkair'],
    'largecal': ['A5_largecal'],
    'smoke': ['A6_smoke'],
}

INNER_LIM = 0.70# 统计内底的径向范围
EDGE_KEEP = 18# 阈值下探量：把抗锯齿边缘纳入
SAT_MAX = 0.32# 饱和度上限（仅剔除明显彩色）
DESPEECKLE = True
OPEN_R = 0# 开运算半径（0 = 不做）

# 归一化
NORM = 40
DST_R = 12# 圆盘归一化目标半径

# 模板侧（40×40 / 64×64 标准图标）
TPL_LIM = 0.84
TPL_SAT_MAX = 0.30


def _open_mask(s, r):
    if r <= 0:
        return s
    from scipy import ndimage  # noqa: F401  （可选依赖，缺省不触发）
    return ndimage.binary_opening(s, iterations=r).astype(np.uint8)


def _despeckle(s, min_px=3):
    from m2_layout import connected_boxes
    out = np.zeros_like(s)
    for (x0, y0, x1, y1) in connected_boxes(s, min_px=min_px):
        out[y0:y1, x0:x1] = s[y0:y1, x0:x1]
    return out


def symbol_mask(sub, ccx, ccy, R, inner_lim=INNER_LIM, edge_keep=EDGE_KEEP,
                sat_max=SAT_MAX, adaptive=True):
    """从图标方形裁片提取白色符号掩膜（自适应阈值）

    ccx/ccy = 圆心在裁片内的坐标；R = 图标半径。"""
    g = to_gray(sub)
    h, w = g.shape
    if h < 8 or w < 8:
        return None
    yy, xx = np.mgrid[0:h, 0:w]
    rr = np.sqrt((xx - ccx) ** 2 + (yy - ccy) ** 2) / float(R)
    inner = rr <= inner_lim
    if inner.sum() < 20:
        return None
    if adaptive:
        thr = max(float(otsu_threshold(g[inner].astype(int))) - edge_keep, 90.0)
    else:
        thr = 180.0
    mx = sub.max(axis=2).astype(float)
    mn = sub.min(axis=2).astype(float)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    m = ((g > thr) & (sat < sat_max) & inner).astype(np.uint8)
    if DESPEECKLE:
        m = _despeckle(m, min_px=3)
    if m.sum() < 10:
        return None
    if OPEN_R:
        m = _open_mask(m, OPEN_R)
    return m


def sym_disk(sym, src_R, dst_R=DST_R, size=NORM):
    """圆盘归一化：2*src_R 缩到 2*dst_R 后居中，保留符号在圆盘中的占比"""
    n = max(4, int(round(2 * dst_R)))
    size = max(size, n)
    crop = (sym > 0).astype(np.uint8) * 255
    small = np.array(Image.fromarray(crop).resize((n, n), Image.BILINEAR),
                     dtype=np.float32) / 255.0
    cv = np.zeros((size, size), np.float32)
    oy, ox = (size - n) // 2, (size - n) // 2
    cv[oy:oy + n, ox:ox + n] = small
    return (cv > 0.5).astype(np.float32)


def iou(a, b):
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    u = float(np.maximum(a, b).sum())
    return float(np.minimum(a, b).sum()) / u if u else 0.0


def load_templates(dst_r=DST_R, edge_keep=None):
    """从 40×40 / 64×64 标准图标建立模板库（每类可多实例）"""
    tpl = {}
    for label, names in CLEAN_REF.items():
        lst = []
        for nm in names:
            p = CLEAN_DIR / ('%s.png' % nm)
            if not p.exists():
                continue
            a = np.array(Image.open(p).convert('RGB'), dtype=float)
            px = a.shape[0]
            if px < 16:
                continue
            R = (px - 1) / 2.0
            g = to_gray(a)
            yy, xx = np.mgrid[0:px, 0:px]
            rr = np.sqrt((xx - R) ** 2 + (yy - R) ** 2) / R
            inner = rr <= TPL_LIM
            thr = max(float(otsu_threshold(g[inner].astype(int)))
                      - (EDGE_KEEP if edge_keep is None else edge_keep), 90.0)
            mx = a.max(axis=2)
            mn = a.min(axis=2)
            sat = (mx - mn) / np.maximum(mx, 1.0)
            m = ((g > thr) & (sat < TPL_SAT_MAX) & inner).astype(np.uint8)
            if m.sum() < 10:
                continue
            v = sym_disk(m, R, dst_r)
            if v is not None:
                lst.append(v)
        if lst:
            tpl[label] = lst
    return tpl


def classify(v, tpl, top=3):
    """归一化掩膜 → [(类型, IoU), ...] 按 IoU 降序"""
    if v is None or not tpl:
        return []
    sc = sorted(((max(iou(v, tv) for tv in lst), t)
                 for t, lst in tpl.items()), key=lambda p: -p[0])
    return sc[:top]


def load_truth():
    """读取人工标注真值

    返回 (images_map, classes)：
        images_map = {"1.png": [["smoke","rapid"], ...], ...}
        classes    = {"smoke": "烟雾", ...}
    真值以images/ 下的文件名为键，详见 data/weak_labeled/README.md。"""
    d = json.loads(TRUTH.read_text(encoding='utf-8'))
    return d['images'], d['classes']


def labeled_image(name):
    """按文件名读取标注集截图（name 如 "1.png"）"""
    p = LABELED_IMAGES / name
    if not p.exists():
        raise FileNotFoundError('标注集缺少 %s（应在 %s）' % (name, LABELED_IMAGES))
    from m2_layout import load
    return load(str(p))