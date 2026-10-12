"""敌方卡头弱点图标识别（m8_weak.py）

卡头结构（实测 1920 宽截图，y 0~90）：
    [圆形图标1] [圆形图标2]  ...  [战力数字]
    圆形图标 = 深灰外环(灰度 55~145) + 深蓝灰内底(~100~130) + 白色符号(~200~230)

识别三步：
    1. 圆环角度覆盖评分定位图标中心（112/112 命中）
    2. **圆盘归一化 + IoU 匹配**，模板取自 40×40 标准游戏图标

**模板来源决定成败**
---------------------------------------------------------------
用户提供了 6 张**40×40 标准图标资源**（data/weak_ref_clean/），
每张含完整的外环 + 内底 + 白色符号。这是目前形态最清晰的模板来源：

| 模板来源 | 符号掩膜尺寸 | 真实截图准确率 |
| --- | --- | --- |
| **40×40 标准图标**（首选） | 24~27 px | **15/16** |
| 从截图裁出的人工核验图（data/weak_auth/） | 20~24 px | 10/16 |
| 悬浮提示图自动定位（data/weak_ref/） | 18~22 px | 8/16 |

**归一化方式必须与之配套** —— 二者只改一项都会退化：

| 归一化 | 40×40 图标模板 | 截图裁模板 |
| --- | --- | --- |
| **圆盘归一化**（保留符号在圆盘中的占比） | **15/16** | 10/16 |
| 按符号 bbox 等比缩放 | 7/16 | 10/16 |

**模板侧与实战侧用不同提取参数**（实测标定，不可统一）：
    模板侧：lim=0.84、Otsu 自适应阈值+6（下限 150）、sat<0.30、**不去噪点**
    实战侧：lim=0.44、thr=180、sat<0.35
    · 模板侧要**大 lim**（完整覆盖符号）与**不去噪点**（干净图无噪，
      去噪会削掉符号末端细笔画，模板像素少 20~30）
    · 实战侧要**小 lim**：大 lim 会把内底亮部（月牙装饰）圈进掩膜
    · 两侧**都不要开运算**：符号笔画本就细，腐蚀会削掉有效笔画

**输出定位：候选建议 + 人工确认，不是最终结论**
    真实截图（1920×828）real2/real3 共 16 枚实测：
    · top1 正确 **15/16**（real2 全对 8/8，real3 7/8）
    · IoU 只有 0.20~0.38：模板符号 24~27px vs 实战符号仅 13~19px，
      相差约 1.7 倍，像素重叠天然有限
    · **绝对 IoU 无判别力**（正确中位 0.250 vs 错误 0.248），
      判断力全在相对 margin（正确中位 0.014，错误 0.009）
    · 唯一错例 real3#7（smoke→largecal）margin 仅 0.009，
      该枚符号笔画已糊到无法判断 —— 属输入分辨率上限

**要再提升只有一条路**：提高输入分辨率（游戏内截图放大）。
1920 宽下卡头符号仅 13~19px，而干净图标是 24~27px，
这个差距无法靠算法弥合。

**为什么不用 data/icon_templates/tip_*.png 那套模板**
    那些图是 420×420 的高分辨率斜置拍摄图（红底白字），
    而卡头符号只有约 13~19 正置像素，细节不可逆丢失，
    实测跨域 IoU 上限仅 0.42。

失败尝试记录（避免重复踩坑）：
    · 灰度窗 + 列/行投影切分：卡面沙色系时投影基线抬高，整段连成一片
    · 滑动窗口密度峰值：窗口宽=图标直径必然同时覆盖相邻两图标半边，峰位偏 27px
    · 动态 _inner_radius() + Otsu：真实图回退到 lim=0.52 截掉符号
    · 旋转容错匹配（±45°，步长 15°）：**净损失**（10/16 → 6/16）。
      斜置符号旋转后会与错误类别偶然重合，IoU 最大值反而选错。
    · 径向特征余弦 / 投影特征：7~8/16，不如圆盘归一化的 15/16
    · 多实例模板无监督扩充（权威图做种子收编实战样本）：**净损失**
      （10/16 → 6~7/16）。会自我强化初始误差 —— 初始分配曾出现
      66/112 全落 antiair，收编后 largecal 误收 18 枚。
    · 位置先验（卡内次枚恒为 atkair）：**不成立**（仅 21/56 命中）。
      视觉上像，实测否定。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json

import numpy as np
from PIL import Image

from m2_layout import (DATA, ROOT, card_vertical, connected_boxes, find_card_columns,
                       load, otsu_threshold, to_gray)

WEAK_TYPES = ['antiair', 'atkair', 'detect', 'largecal', 'rapid', 'smoke']
TPL_DIR = ROOT / 'data' / 'icon_templates'
REF_TPL = {t: TPL_DIR / f'tip_{t}_t.png' for t in WEAK_TYPES}

# 游戏内弱点悬浮提示截图（data/weak_ref/）—— **卡头域**权威标注。
# 每张图包含该卡的两枚图标，悬浮框给出的名字对应其中一枚。
# 6 张图按「形态唯一性」核对后确定下表（见 doc/设计方案 4.7 节的形态对照）：
#   烟幕      斜置圆罐 + 斜线          （smoke）
#   速射      三道平行斜线
#   攻击机    十字交叉 + 中心圆
#   被侦察性  同心圆环 + 中心实心圆
#   防空火力  五指竖条（手掌）
#   大口径    斜置粗管 + 斜线（比烟幕更粗、无中心圆）
WEAK_REF = {
    'w1_smoke':    ('smoke',    0),   # 左=斜罐
    'w2_rapid':    ('rapid',    1),   # 右=三斜线
    'w3_atkair':   ('atkair',   1),   # 右=十字
    'w4_detect':   ('detect',   1),   # 右=同心圆
    'w5_antiair':  ('antiair',  0),   # 左=手掌
    'w6_largecal': ('largecal', 0),   # 左=粗管
}
REF_DIR = ROOT / 'data' / 'weak_ref'

# 六张**权威标注图**（data/weak_auth/）—— 首选模板来源。
# 每张图只含一枚弱点图标，且中心坐标已人工核验（x, y, 半径）。
# 相比 data/weak_ref/ 的自动定位（_ring_score + _pick_peaks），
# 权威图避免了自动定位在低分辨率下选错相邻图标的问题：
# 真实截图回归 10/16（旧自动定位 8/16）。
AUTH_REF = {
    'rapid':    ('L1_rapid',    (57, 60), 28),
    'smoke':    ('L2_smoke',    (50, 64), 28),
    'largecal': ('L3_largecal', (53, 56), 28),
    'atkair':   ('L4_atkair',   (117, 63), 28),
    'detect':   ('L5_detect',   (42, 43), 28),
    'antiair':  ('L6_antiair',  (55, 59), 28),
}
# 六张**权威参考图**（data/weak_ref_clean/A1~A6_*.png）—— 首选模板来源。
# 每张是**标准游戏图标资源**（含外环 + 内底 + 白色符号），
# 由用户直接从游戏图标文件导出，因此：
#   · 中心与半径由图像边长直接推出，无需人工给坐标
#   · 无截图背景污染，不带月牙形残影
#   · 形态完整，是目前唯一能提供「清晰形态」的模板来源
#
# **图标边长不固定**：用户提供的同类图标存在 40×40 与 64×64 两种规格
# （例如 detect 同时有 A1_detect 40px 与 B1_detect_big 64px）。
# 实测同一类 detect 的符号掩膜：
#     40px 图标 → 符号 26×27 px
#     64px 图标 → 符号 47×41 px     （大一倍）
# 尺寸更大的模板信息量更足，故按 `CLEAN_REF` 列出**全部**实例，
# 分类时取与任一实例的最大 IoU（`build_templates` 本就支持多实例）。
CLEAN_REF = {
    'detect':   ['A1_detect', 'B1_detect_big'],
    'antiair':  ['A2_antiair'],
    'rapid':    ['A3_rapid'],
    'atkair':   ['A4_atkair'],
    'largecal': ['A5_largecal'],
    'smoke':    ['A6_smoke'],
}
CLEAN_DIR = ROOT / 'data' / 'weak_ref_clean'

# 模板侧提取参数（40×40 图标）
TPL_LIM = 0.84         # 径向截断：符号外接半径约 0.6×19.5，留到 0.84 可完整覆盖
                        # 且不含外环（外环内缘在 0.85 附近）
TPL_THR = 170.0
TPL_SAT_MAX = 0.30
TPL_DESEECKLE = False   # 模板侧不去小连通域：干净图标上无噪点，
                        # 去噪反而削掉符号末端的细笔画（实测模板像素
                        # 少20~30，两版本模板互IoU 仅 0.78~0.92）
ICON_PX = 40# 标准图标边长
ICON_R = 19.5          # (ICON_PX - 1) / 2

AUTH_DIR = ROOT / 'data' / 'weak_auth'

TPL_JSON = ROOT / 'data' / 'weak_templates.json'

NORM = 40             # 归一化掩膜边长（须≥ 2*QUERY_DST_R，否则画布裁切内容）
NRING, NANG = 6, 24    # 径向重采样：6 环 × 24 角
RADII = (20, 23, 26, 29)

# 符号提取参数（针对真实截图标定）
#
# 三处关键修正（均由真实截图回归驱动，见模块 docstring「失败尝试记录」）：
#   1. 径向范围用**固定 lim=0.66**。旧实现用动态 _inner_radius()，
#      在真实图上会回退到 lim=0.52 把符号整体截断，
#      而实测符号笔画延伸到 d≈0.55~0.60。
#   2. 用**固定白阈值 170**。旧实现用 Otsu 自适应阈值，
#      在浅色卡面上被拉到 142，把外环边缘一起纳入。
#   3. 开运算剔除符号周围残留的细环弧，必须用「邻域计数」实现
#      （腐蚀 = 补集膨胀求补）。早期用布尔转置的版本逻辑写反，
#      会把掩膜从 343px 膨胀到 823px（退化成实心块），
#      准确率从 10/16 掉到 8/16。
SYM_LIM = 0.66         # 内底径向截断：外环内缘实测在 0.55~0.62R
SYM_THR = 170.0        # 白像素阈值：内底~105 / 符号~210 / 卡面沙色~215
SYM_SAT_MAX = 0.35     # 饱和度上限：内底(深蓝灰)实测 0.58~0.61，
                       # 白色符号仅 0.03~0.15 —— 饱和度比灰度更能
                       # 区分「符号」与「内底/卡面」，是排除浅色卡面的主力判据
OPEN_R = 1             # 开运算结构元半径（去细环弧）

# 实战截图侧的提取与归一化参数（由真实截图 real2/real3 网格标定）
Q_LIM = 0.44# 径向截断：实战图标 R=20/23，符号外接约 0.6R。
                        # 实测比模板侧更小的 lim 更优 —— 大 lim 会把内底
                        # 亮部（月牙装饰）圈进掩膜，实战图该装饰更明显。
Q_THR = 180.0        # 白像素阈值：略高于模板侧，抑制实战图的抗锯齿灰边
QUERY_DST_R = 12     # 圆盘归一化的目标半径（≈ 实战符号的实际半径）
Q_OPEN = False      # 实战侧不做开运算：实测加上会掉分
                     #（符号笔画本就细，开运算的腐蚀把有效笔画削掉）
TPL_OPEN = False# 模板侧同理


# ============ 符号掩膜提取 ============
def _despeckle(sym, min_px=4):
    out = np.zeros_like(sym)
    for (x0, y0, x1, y1) in connected_boxes(sym, min_px=min_px):
        out[y0:y1, x0:x1] = sym[y0:y1, x0:x1]
    return out


def _shift_or(s, r):
    """布尔膨胀：(s ⊕ 方形结构元 r)。任一邻域前景即前景。"""
    h, w = s.shape
    d = np.zeros((h, w), dtype=bool)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            ys0, ys1 = max(0, dy), h + min(0, dy)
            xs0, xs1 = max(0, dx), w + min(0, dx)
            d[ys0:ys1, xs0:xs1] |= s[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx]
    return d


def _open_mask(s, r=OPEN_R):
    """形态学开运算 = 腐蚀 ∘ 膨胀，用邻域计数实现（不引入 scipy 依赖）

    作用：删掉细于结构元的环弧/毛刺，只保留实心笔画。
    注意：腐蚀会让笔画整体变细，结构元必须小于最细的有效笔画宽度；
    实测 r=1 合适，r>=2 会把 detect 的同心圆环整圈抹掉。"""
    if r <= 0 or s.sum() == 0:
        return s.astype(np.uint8)
    dil = _shift_or(s.astype(bool), r)         # 膨胀
    ero = ~_shift_or(~dil, r)                  # 腐蚀 = 补集膨胀求补
    out = ero.astype(np.uint8)
    return out if out.sum() >= 10 else s.astype(np.uint8)


def _symbol_mask(sub, ccx, ccy, R, lim=SYM_LIM, thr=SYM_THR,
                 sat_max=SYM_SAT_MAX, use_open=True, despeckle=True):
    """从图标方形裁片提取白色符号掩膜

    ccx/ccy = 圆心在裁片内的坐标；R = 图标半径。
    用固定 lim/thr 而非自适应阈值，理由见模块头部「符号提取的三处关键修正」。
    模板侧（40×40 图标）与实战侧（20~23px 卡头）用不同参数，见 Q_LIM / TPL_LIM。

    thr 为 None 时按内底/符号双峰做Otsu + 6 下限 150（模板侧使用 ——
    干净图标无卡面污染，自适应阈值比固定值覆盖更完整）。"""
    g = to_gray(sub)
    h, w = g.shape
    yy, xx = np.mgrid[0:h, 0:w]
    rr = np.sqrt((xx - ccx) ** 2 + (yy - ccy) ** 2) / float(R)
    inner = rr <= lim
    if inner.sum() < 20:
        return None
    if thr is None:
        thr = max(float(otsu_threshold(g[inner].astype(int))) + 6, 150.0)
    mx = sub.max(axis=2).astype(float)
    mn = sub.min(axis=2).astype(float)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    s = ((g > thr) & (sat < sat_max) & inner).astype(np.uint8)
    if despeckle:
        s = _despeckle(s, min_px=3)
    if s.sum() < 10:
        return None
    return _open_mask(s) if use_open else s


def _sym_disk(sym, src_R, dst_R, size=NORM):
    """圆盘归一化：把 2*src_R 的方形裁片缩到 2*dst_R 后居中放入size×size 画布

    与按符号 bbox 归一化的本质区别：**保留符号在圆盘中的相对位置与占比**。
    只有这样，40×40 的清晰模板（符号 24~27px）与 20×23px 的实战提取
    （符号 13~19px）之间才具备可比性 —— 实测这是准确率从 10/16 提到 15/16
    的关键（见 docstring「三种归一化对比」）。

    画布尺寸自适应：dst_R 较大时不裁切内容。"""
    n = max(4, int(round(2 * dst_R)))
    size = max(size, n)
    crop = (sym > 0).astype(np.uint8) * 255
    small = np.array(Image.fromarray(crop).resize((n, n), Image.BILINEAR),
                     dtype=np.float32) / 255.0
    canvas = np.zeros((size, size), np.float32)
    oy, ox = (size - n) // 2, (size - n) // 2
    canvas[oy:oy + n, ox:ox + n] = small
    return (canvas > 0.5).astype(np.float32)


def _query_symbol(sub, ccx, ccy, R):
    """实战截图侧的符号提取：用 Q_LIM / Q_THR 专用参数

    与模板侧的 TPL_LIM=0.84 / TPL_THR=170 刻意不同 ——
    实战图有抗锯齿灰边与更明显的内底装饰，需更保守的阈值与更小的径向范围。"""
    return _symbol_mask(sub, ccx, ccy, R, lim=Q_LIM, thr=Q_THR, use_open=Q_OPEN)


def _icon_symbol(box, a):
    """从图标框内提取符号掩膜（白色符号 / 深蓝灰内底）"""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w < 10 or h < 10:
        return None
    sub = a[y0:y1, x0:x1]
    # find_weak_icons 用 half = R + 6 留边，故圆心在正中、R = 半边 - 6
    R = min(w, h) // 2 - 6
    if R < 5:
        return None
    return _query_symbol(sub, (w - 1) / 2.0, (h - 1) / 2.0, R)


def _icon_radius(box):
    """图标框 → 图标半径（find_weak_icons 用 half = R + 6 留边）"""
    x0, y0, x1, y1 = box
    return max(5, min(x1 - x0, y1 - y0) // 2 - 6)


def _sym_norm(sym, R=None, size=NORM):
    """符号掩膜 → 归一化到 size×size 的0/1 掩膜

    R 不为 None 时走**圆盘归一化**（`_sym_disk`），保持符号在圆盘中的占比；
    R 为 None 时回退到按符号 bbox 等比缩放（保长宽比）。

    实测（真实截图 real2/real3 共 16 枚）：
        圆盘归一化 + 40×40 干净模板   15/16
        按 bbox 保长宽比 + 截图裁模板  10/16
        按 bbox 保长宽比 + 干净模板      7/16
    可见「模板质量」与「归一化方式」必须同时改，只改一项都会退化。"""
    if R is not None:
        return _sym_disk(sym, R, QUERY_DST_R, size)
    ys, xs = np.where(sym > 0)
    if len(ys) == 0:
        return None
    crop = sym[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    ch, cw = crop.shape
    scale = size / float(max(ch, cw))
    nh, nw = max(1, int(round(ch * scale))), max(1, int(round(cw * scale)))
    small = np.array(Image.fromarray(crop).resize((nw, nh), Image.BILINEAR),
                     dtype=np.float32) / 255.0
    canvas = np.zeros((size, size), np.float32)
    oy, ox = (size - nh) // 2, (size - nw) // 2
    canvas[oy:oy + nh, ox:ox + nw] = small
    return (canvas > 0.5).astype(np.float32)


def radial_feature(v, nring=6, nang=24, nfold=6):
    """旋转不变的极坐标特征

    两步：
        1. 极坐标重采样：nring 个半径 × nang 个角度
        2. 角度按 nfold 折叠，**扇区内取最大值**（等价于「该方向上最粗的笔画」）

    为什么用扇区取最大值而不是直接对角度平均：
        直接平均会把三斜杠（rapid）与四向十字（atkair）抹成同一团，
        实测 112 个样本聚类时这两类大量混入同一簇。
        取最大值相当于求该方向上的**径向最大值投影**，
        既保留笔画根数与方向性，又对任意旋转免疫。"""
    h, w = v.shape
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    R = min(w, h) / 2.0
    th = (np.arange(nang) + 0.5) * (2 * np.pi / nang)
    out = np.zeros((nring, nfold))
    per = nang // nfold
    for i in range(nring):
        r = (i + 0.5) * (0.80 / nring)
        ys = np.rint(cy + r * R * np.sin(th)).astype(int)
        xs = np.rint(cx + r * R * np.cos(th)).astype(int)
        ok = (ys >= 0) & (ys < h) & (xs >= 0) & (xs < w)
        buf = np.zeros(nang)
        buf[ok] = v[ys[ok], xs[ok]]
        buf = buf.reshape(nfold, per)
        out[i] = buf.max(axis=1)
    return out.ravel()


def _unit(x):
    x = np.asarray(x, dtype=np.float32)
    n = np.linalg.norm(x)
    return x / n if n > 0 else x


# ============ 图标定位 ============
def _ring_score(ring, radii=RADII):
    """圆环角度覆盖评分：对每个候选中心与半径，在 24 个角度上采样环掩膜"""
    H, W = ring.shape
    NA = 24
    ang = np.arange(NA) * (2 * np.pi / NA)
    dy, dx = np.sin(ang), np.cos(ang)
    cy, cx = np.mgrid[0:H, 0:W]
    cy = cy.ravel().astype(float)
    cx = cx.ravel().astype(float)
    score = np.zeros(cy.shape[0])
    best_r = np.full(cy.shape[0], radii[0], dtype=float)
    for r in radii:
        ys = np.rint(cy[:, None] + r * dy[None, :]).astype(int)
        xs = np.rint(cx[:, None] + r * dx[None, :]).astype(int)
        ok = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
        hit = np.zeros_like(ok)
        hit[ok] = ring[ys[ok], xs[ok]]
        s = hit.sum(axis=1) / float(NA)
        upd = s > score
        score[upd] = s[upd]
        best_r[upd] = r
    return score.reshape(H, W), best_r.reshape(H, W)


def _pick_peaks(score, min_dist=30, max_n=2, min_score=0.72):
    H, W = score.shape
    flat = score.ravel()
    order = np.argsort(flat)[::-1]
    picked = []
    for idx in order:
        if flat[idx] < min_score:
            break
        y, x = divmod(int(idx), W)
        if all((x - px) ** 2 + (y - py) ** 2 > min_dist ** 2 for px, py in picked):
            picked.append((x, y))
        if len(picked) >= max_n:
            break
    return picked


def find_weak_icons(a, card):
    """在卡头左侧定位圆形弱点图标，返回 [(x0,y0,x1,y1), ...]（≤2 个）"""
    x0, y0, x1, y1 = card['bbox']
    h = y1 - y0
    top = y0 + int(h * 0.01)
    bh = int(h * 0.32)
    band = a[top:top + bh, x0:x1]
    if band.size == 0:
        return []
    g = to_gray(band)
    ring = (g > 55) & (g < 145)
    if ring.sum() < 200:
        return []
    score, best_r = _ring_score(ring)
    out = []
    for (cx, cy) in _pick_peaks(score):
        R = int(best_r[cy, cx])
        half = R + 6
        out.append((x0 + max(0, cx - half), top + max(0, cy - half),
                    x0 + min(band.shape[1], cx + half),
                    top + min(band.shape[0], cy + half)))
    out.sort(key=lambda b: b[0])
    return out


# ============ 特征与模板库 ============
def sample_features(path):
    """从一张截图提取全部图标特征 → [(box, feature), ...]"""
    a = load(path)
    out = []
    for (x0, x1) in find_card_columns(a):
        yt, yb = card_vertical(a, x0, x1)
        for ib in find_weak_icons(a, {'bbox': (x0, yt, x1, yb)}):
            sym = _icon_symbol(ib, a)
            if sym is None:
                out.append((ib, None))
                continue
            v = _sym_norm(sym, R=_icon_radius(ib))
            out.append((ib, radial_feature(v) if v is not None else None))
    return out


def _kmeans(F, k, iters=80, seed=0):
    """KMeans。label 初值必须置为 -1，否则首次迭代就可能与初值相同而提前退出，
    导致所有点被归到同一簇（曾出现 56/56 全落一簇）。"""
    rng = np.random.default_rng(seed)
    C = F[rng.choice(len(F), k, replace=False)].copy()
    lab = np.full(len(F), -1, dtype=int)
    for _ in range(iters):
        d = ((F[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
        new = d.argmin(axis=1)
        if (new == lab).all():
            break
        lab = new
        for j in range(k):
            if (lab == j).sum():
                C[j] = F[lab == j].mean(axis=0)
    return lab, C


def _clean_templates():
    """从 data/weak_ref_clean/ 的标准图标提取模板

    这批图由用户从游戏图标资源直接导出，中心与半径由边长推出
    （图标居中，R = (边长-1)/2），不必人工给坐标。

    **图标边长不固定**：实测用户提供的同类图标存在 40x40 与 64x64 两种
    规格（detect 同时有 A1_detect 40px 与 B1_detect_big 64px），
    同一类 detect 的符号掩膜分别为 26x27px 与 47x41px —— 后者信息量高一倍。
    因此按图像实际尺寸算 R，并把同一类的全部实例都存进模板库，
    分类时取与任一实例的最大 IoU。"""
    out = {}
    for label, names in CLEAN_REF.items():
        if isinstance(names, str):
            names = [names]
        for name in names:
            p = CLEAN_DIR / ('%s.png' % name)
            if not p.exists():
                continue
            a = load(str(p))
            if a.ndim != 3 or a.shape[0] != a.shape[1]:
                continue
            px = a.shape[0]
            if px < 16:
                continue
            R = (px - 1) / 2.0
            sym = _symbol_mask(a, R, R, R,
                               lim=TPL_LIM, thr=None, sat_max=TPL_SAT_MAX,
                               use_open=TPL_OPEN, despeckle=TPL_DESEECKLE)
            if sym is None:
                continue
            v = _sym_disk(sym, R, QUERY_DST_R)
            if v is not None:
                out.setdefault(label, []).append(v)
    return out


def ref_icons():
    """抽出 6 类弱点的归一化符号掩膜（每类可含多个尺度实例）

    三级来源，逐级回退：
        1. data/weak_ref_clean/ —— 标准图标资源（**首选**），形态最完整，
           支持 40/64 等多种边长
        2. data/weak_auth/       —— 从截图裁出的人工核验图（带月牙残影）
        3. data/weak_ref/        —— 悬浮提示图自动定位（低分辨率下易选错邻居）

    实测（真实截图 16 枚）：1 级 15/16，2 级 10/16。
    返回 {类型: [归一化掩膜, ...]}"""
    out = _clean_templates()
    if all(out.get(t) for t in CLEAN_REF):
        return out

    for label, (name, (cx, cy), R) in AUTH_REF.items():
        if label in out:
            continue
        p = AUTH_DIR / ('%s.png' % name)
        if not p.exists():
            continue
        a = load(str(p))
        sym = _symbol_mask(a[max(0, cy - R):cy + R, max(0, cx - R):cx + R], R, R, R)
        if sym is not None:
            v = _sym_norm(sym, R=R)
            if v is not None:
                out.setdefault(label, []).append(v)
    if all(out.get(t) for t in CLEAN_REF):
        return out

    for name, (label, which) in WEAK_REF.items():
        if label in out:
            continue
        p = REF_DIR / ('%s.png' % name)
        if not p.exists():
            continue
        a = load(str(p))
        g = to_gray(a)
        H, W = g.shape
        band = np.zeros((H, W), dtype=bool)
        band[:int(H * 0.28), :int(W * 0.32)] = True
        ring = ((g > 55) & (g < 160)) & band
        radii = tuple(sorted({int(H * 0.05), int(H * 0.06), int(H * 0.07),
                              int(H * 0.08), int(H * 0.09)}))
        score, best_r = _ring_score(ring, radii=radii)
        picks = sorted(_pick_peaks(score, min_dist=int(H * 0.16), max_n=2,
                                   min_score=0.85), key=lambda t: t[0])
        if len(picks) <= which:
            continue
        cx, cy = picks[which]
        R = int(best_r[cy, cx])
        box = (max(0, cx - R - 6), max(0, cy - R - 6),
               min(W, cx + R + 6), min(H, cy + R + 6))
        sym = _icon_symbol(box, a)
        if sym is None:
            continue
        v = _sym_norm(sym, R=_icon_radius(box))
        if v is not None:
            out.setdefault(label, []).append(v)
    return out


def build_templates(save=True):
    """建立弱点模板库。

    模板取自 data/weak_ref_clean/ 的标准图标资源（支持 40/64 等多种边长，
    同类可有多个实例）。每个类型存**多个样本掩膜**，
    分类时取与任一实例的最大 IoU，容忍尺度与描边粗细差异。"""
    masks = ref_icons()
    tpl = {t: (v if isinstance(v, list) else [v]) for t, v in masks.items()}
    if save:
        TPL_JSON.write_text(json.dumps(
            {t: [m.tolist() for m in vs] for t, vs in tpl.items()}), encoding='utf-8')
    return tpl


def load_templates():
    if not TPL_JSON.exists():
        return build_templates()
    d = json.loads(TPL_JSON.read_text(encoding='utf-8'))
    return {t: [np.array(m, dtype=np.float32) for m in vs] for t, vs in d.items()}


def _default_files():
    return ['kp1', 'kp2', 'kp3', 'boss',
            'ex_kp1', 'ex_kp2', 'ex_kp3'] + ['n%d' % i for i in range(1, 8)]


def classify_all(v, tpl, top=3):
    """归一化符号掩膜 → [(类型, IoU), ...] 按 IoU 降序，最多 top 个

    对每类的每个实例取 IoU 最大值。
    **不做旋转容错**：实测（真实截图 16 枚图标）带旋转容错反而从
    10/16 掉到 6/16 —— 斜置符号旋转后会与错误类别偶然重合，
    取最大值等于主动放大误判。"""
    if v is None or not tpl:
        return []
    sc = []
    for t, samples in tpl.items():
        best = 0.0
        for s in samples:
            u = float(np.maximum(v, s).sum())
            if u:
                best = max(best, float(np.minimum(v, s).sum()) / u)
        sc.append((t, best))
    sc.sort(key=lambda p: -p[1])
    return sc[:top]


# 判定可靠性分档（真实截图 real2/real3 共 16 枚实测标定）
#   采用圆盘归一化 + 40×40 干净模板后，IoU 整体下移到0.22~0.38
#   （模板符号 24~27px vs 实战符号 13~19px，像素重叠有限）
#   正确 10 枚 IoU 中位 0.297，错误 6 枚中位 0.280 —— 仍然几乎重叠，
#   因此 CONF_IOU 只作「是否提取到可用掩膜」的粗筛，取 0.18 全量保留。
#   相对 margin = top1 − top2 依旧是唯一有效信号。
CONF_IOU = 0.18        # top1 绝对下限（低于此视为提取失败；实测 16枚全在0.20~0.38）
CONF_MARGIN = 0.02# top1−top2 相对差距下限（实测唯一有效判据：
                     #   >=0.02 时保留 7/15 正确、误放行 0/1）


def classify(v, tpl, thr=CONF_IOU, margin=CONF_MARGIN):
    """归一化符号掩膜 → (类型, top1 IoU, margin, 需人工确认?)

    margin 无论是否低于 thr 都要算出来 —— 调用方需要它做分档，
    早期实现在 thr 分支提前 return 导致 margin 恒为 0、分档完全失效。"""
    sc = classify_all(v, tpl, top=2)
    if not sc:
        return None, 0.0, 0.0, True
    m = sc[0][1] - (sc[1][1] if len(sc) > 1 else 0.0)
    if sc[0][1] < thr:
        return None, sc[0][1], m, True
    return sc[0][0], sc[0][1], m, (m < margin)


def verify_flags(res, iou_thr=CONF_IOU, margin_thr=CONF_MARGIN):
    """汇总一张截图的弱点识别可靠性，返回需人工确认的条目列表"""
    bad = []
    for r in res:
        for it in r['icons']:
            if it.get('needs_review'):
                bad.append({'card': r['card'], **it})
    return bad


def read_weaknesses(path, tpl=None):
    """主入口：读取一张截图，返回每张卡的弱点列表（含候选与置信度）"""
    tpl = tpl or load_templates()
    a = load(path)
    out = []
    for i, (x0, x1) in enumerate(find_card_columns(a)):
        yt, yb = card_vertical(a, x0, x1)
        icons = find_weak_icons(a, {'bbox': (x0, yt, x1, yb)})
        items = []
        for ib in icons:
            sym = _icon_symbol(ib, a)
            v = _sym_norm(sym, R=_icon_radius(ib)) if sym is not None else None
            cand = classify_all(v, tpl, top=3)
            t, sc, mg, need = classify(v, tpl)
            items.append({'box': ib, 'type': t, 'score': round(float(sc), 3),
                          'margin': round(float(mg), 3), 'needs_review': bool(need),
                          'candidates': [(c, round(float(s), 3)) for c, s in cand]})
        out.append({'card': i + 1, 'bbox': (x0, yt, x1, yb),
                    'weak': [it['type'] for it in items], 'icons': items})
    return out


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--build', action='store_true', help='重建模板库')
    args = ap.parse_args()
    if args.build:
        t = build_templates()
        print('已重建模板库: %s → %s' % (list(t), TPL_JSON))
    tpl = load_templates()
    print('模板库 %d 类: %s' % (len(tpl), list(tpl)))
    tot = rev = 0
    for f in _default_files():
        res = read_weaknesses(DATA + '/' + f + '.jpg', tpl)
        bad = verify_flags(res)
        n = sum(len(r['icons']) for r in res)
        tot += n; rev += len(bad)
        print('--- %-7s 图标 %d，待确认 %d' % (f, n, len(bad)))
        for r in res:
            for it in r['icons']:
                cands = ' '.join('%s:%.2f' % c for c in it['candidates'])
                print('  卡%d %-9s %.3f m=%.3f%s  [%s]'
                      % (r['card'], it['type'], it['score'], it['margin'],
                         ' !' if it['needs_review'] else '  ', cands))
    print('\n合计图标 %d，待人工确认 %d (%.0f%%)' % (tot, rev, 100.0 * rev / max(1, tot)))
