"""底部变项工具栏识别（M3e）

版式（实测 1920×786~830，样本13 张 + 真实截图 1 张）
─────────────────────────────────────────────────────────────────────
  ...  总战力↓ [灰][灰][灰][灰][灰][灰]│[绿][绿][绿][绿][绿]
  y≈0.80~0.92H  ├─ 灰底 = 标准变项（商店可买，6 种）
     └─ 绿底 = 特殊变项（关卡掉落，5 种）
                 右下角黑底白字 = 该变项的**库存数量**

设计要点：利用「排列固定」这一前提大幅简化
─────────────────────────────────────────────────────────────────────
实测 11 个槽位的方块本体 60×59 px，中心间距恒为 **84~88px**，
第6→7 槽额外多 19~20px 分隔（标准与特殊之间有一道竖线）。顺序固定不变。
因此**不做逐个图标识别**，改为：

1. 行投影定位工具栏行段
2. 列投影切出**可用**槽（灰色不可用槽在`b−r` 上等同于地图底，
   会被此判据漏掉 —— 故改用「亮像素 b−r」二次判据补全，见下）
3. 由等间距反推全部 11 槽坐标，**灰色槽由位置补齐**
4. 逐槽判三态：`special` / `std` / `none`（灰色 = 不存在该插件）
5. 读右下角黑底白字角标的**具体数值**

依据用户确认：**图标排列相对固定；不存在相应插件时该图标为灰色。**

两级判据（缺一不可）
─────────────────────────────────────────────────────────────────────
**判据一`b − r < 40`（蓝红差）** —— 用于切出可用槽的方块本体。
与亮度无关，故对灰底与绿底同样成立：

| 区域 | `b − r` 实测 |
| --- | --- |
| 地图青底 | +100 ~ +137 |
| 灰底方块（标准） | +2 ~ +10 |
| 绿底方块（特殊） | +39 ~ +45 |

地图底与方块相差 4 倍以上，阈值 40 完全分开。

**判据二「亮像素 `b − r`」** —— 用于区分可用槽与灰色不可用槽。
灰色槽的方块被整体染成青色（等同地图底），判据一无法检出；
但两者的**图标亮度**差异极大：

| 槽状态 | 亮像素（top10%）`b − r` | 实测来源 |
| --- | --- | --- |
| 可用（std / special） | **0 ~ 16** | 样本 13 张 + 真实截图 |
| **灰色 = 不存在该插件** | **+98 ~ +110** | 真实截图 slot0/4/5/8 |

原理：可用槽的图标是白色/浅色（各通道接近），
灰色槽的图标被染成与地图底一致的青色（蓝通道显著偏高）。

**只用判据一的失败实测**：真实截图只切出 7 段（应为 11），
灰色槽 0/4/5/8 全丢，库存数字误识为 46472。

角标数字识别
─────────────────────────────────────────────────────────────────────
不靠黑底框定位（绿底方块边缘会与之连通导致 bbox 溢出），
而是在方块下半部用「白字连通域尺寸 + 位置双筛」定位数字，
再复用 `data/stage/glyph_tpl.npz`（覆盖 0~9 全数字）余弦匹配。
实测位数 143/143、数值 143/143。

注意：本模块**不识别图标身份**（是炮术训练还是心无旁骛）——
图标形态映射交由用户按槽位序号对应，程序只负责「有哪些可用、各有多少个」。

**禁用变项由用户输入**
禁用状态在截图上与普通可用槽**视觉完全一致**，程序无法自行判断，
故提供 `prompt_disables()` 交互询问与 `apply_disables()` 合并接口。
以**全歼**为目标时禁用变项通常不构成约束，故默认「不考虑禁用」。
"""

import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

import numpy as np
from PIL import Image
from m2_layout import load, load_screen, to_gray, connected_boxes, neutral_mask
from m3_token import glyph_vec

# ---- 版式常量（实测） ----
# BAR_Y_MIN 0.60：战斗界面工具栏在画面下 1/4，但地图/部署界面（2026-10-11
# 用户实测）工具栏在画面 ~67% 高度处 —— 收窄到上 3/5 起，误报由
# 「须能切出 9~13 个等宽方块」的列投影校验兜底。
BAR_Y_MIN = 0.60
X_SEARCH_MAX = 0.78 # 工具栏只占左侧 78% 宽
ROW_FRAC = 0.30    # 行投影占比阈值（定位工具栏行段）
                  # 需低于 0.40：灰色不可用槽在判据一下不可见，
                  # 真实截图工具栏行段占比仅 0.35（7/11 槽可见），
                  # 而底部我方卡组栏占比高达 0.99 → 0.30~0.40 可分开
N_SLOT = 11# 固定 11 槽
N_STD = 6           # 前 6 个为标准变项
PITCH = 86          # 槽位中心间距（实测 84~88）
GAP_EXTRA = 20# 第6→7 槽的额外间隔
BOX_W = 62          # 方块宽度（实测 60~65）
BOX_H = 60          # 方块高度（实测 58~61）
GLYPH_TPL = ROOT / 'data' / 'stage' / 'glyph_tpl.npz'

# ---- 底色判据 ----
MAP_TINT_MIN = 40   # b − r < 此值才算非地图底（判据一）
COL_FRAC = 0.55     # 列投影占比阈值
RUN_MIN_W = 15      # 区段最小宽度
GREEN_THR = 18      # 绿度阈值：绿底 +39~45 vs 灰底 +2~10
# 判据二：亮像素 b − r
BRIGHT_PCT = 90     # 取亮度 top10% 像素作为「图标」
AVAIL_TINT_MAX = 60 # 亮像素 b − r < 此值 → 可用；灰色不可用为 +98~110

# ---- 角标判据 ----
BAND_Y = 0.50       # 取方块下半部
BADGE_WHITE = 165   # 角标白字阈值（比m3 的 150 更严，压掉抗锯齿边）
DIG_H_MIN = 11      # 数字笔画最小高度
DIG_W_MAX = 12      # 数字笔画最大宽度（宽于此为图标残片/粘连）
DIG_TOP_MIN_FRAC = 0.25  # 兜底：黑底框未检出时，数字不许从下半部顶边开始
DIG_MIN_PX = 15     # 连通域最小像素
BADGE_BOX_H = (16, 30)   # 黑底 bbox 高度范围
BADGE_BOX_W = (16, 50)   # 黑底 bbox 宽度范围
BADGE_TOL = 3            # 块心落在黑底内的容差


# ---------- 工具栏定位 ----------
def _not_map(a):
    """非地图青底掩膜（判据一：b − r < 40）"""
    r = a[:, :, 0].astype(int)
    b = a[:, :, 2].astype(int)
    return (b - r) < MAP_TINT_MIN


def bar_rows(a):
    """行投影定位工具栏行段 → (y0, y1) 或 None

    只在画面下 1/4 找。限定范围是必需的 —— 顶部卡牌区同样满足
    `b−r<40`，不加限定会误取卡牌行段（实测卡牌列间距 358px、方块 85px，
    两者在列投影阶段可区分，但先排除更省事）。

    另需**校验列投影结果**：底部我方卡组栏同样满足行判据，
    但其列投影会切出超宽区段（实测 129/434/218/650 px），
    故返回前先验证「能切出 9~13 个宽度相近的方块」。
    """
    H, W = a.shape[:2]
    nm = _not_map(a)
    x0, x1 = int(W * 0.25), int(W * 0.60)
    rows = nm[:, x0:x1].mean(axis=1)
    ys = [y for y in range(int(H * BAR_Y_MIN), H) if rows[y] > ROW_FRAC]
    segs = []
    for y in ys:
        if segs and y - segs[-1][1] <= 2:
            segs[-1][1] = y
        else:
            segs.append([y, y])
    segs = [s for s in segs if s[1] - s[0] >= 30]
    # 择优：取「能切出最多合格方块」的行段。
    # 工具栏行段高 58~61px，切出 7~11 个宽 60~65px 的方块；
    # 底部我方卡组栏行段会切出超宽区段（129/433/217/649px）而被淘汰。
    best = (0, None)
    for y0, y1 in sorted(segs, key=lambda t: t[0]):
        boxes = _cols_in_band(nm, y0, y1, W)
        good = [b for b in boxes
                if 0.6 * BOX_W <= (b[1] - b[0]) <= 1.4 * BOX_W]
        if len(good) > best[0]:
            best = (len(good), (y0, y1))
        if len(good) >= 7:
            return y0, y1
    return best[1]


def _cols_in_band(nm, y0, y1, W):
    """在给定行段内做列投影，切出宽度合理的方块区段"""
    cols = nm[y0:y1 + 1, :int(W * X_SEARCH_MAX)].mean(axis=0)
    xs = [x for x in range(int(W * X_SEARCH_MAX)) if cols[x] > COL_FRAC]
    runs, inr = [], False
    for x in xs:
        if not inr:
            runs.append([x, x]); inr = True
        elif x - runs[-1][1] <= 2:
            runs[-1][1] = x
        else:
            runs.append([x, x])
    return [(s, e) for s, e in runs if e - s >= RUN_MIN_W]


def slot_boxes(a):
    """列投影切出**可用**方块的区段 → [(x0, x1), ...]

    注意：灰色不可用槽在判据一下与地图底同色，**不会出现在结果里**。
    全部 11 槽由 `slot_states()` 按等间距补齐。
    """
    band = bar_rows(a)
    if band is None:
        return []
    return _cols_in_band(_not_map(a), band[0], band[1], a.shape[1])


# ---------- 槽位状态 ----------
def _cell_at(a, y0, y1, cx):
    """按中心 x 取出方块本体（以 cx 为中心的固定宽度窗）"""
    x0 = int(cx - BOX_W / 2)
    x1 = x0 + BOX_W
    H, W = a.shape[:2]
    if x0 < 0:
        x0, x1 = 0, BOX_W
    if x1 > W:
        x0, x1 = W - BOX_W, W
    return a[y0:y1 + 1, x0:x1]


def _classify(cell):
    """判定方块状态 → (kind, tint, green_val, bright_frac)

    判据二：取亮度 top10% 像素（图标），看其 `b − r`。
      可用（std / special）→ 0~16
      灰色不可用           → +98~+110

    再用绿度区分 std / special。绿度口径修正（2026-10-11）：旧口径取
    「最亮 10% 像素」的中位数 —— 白色图标在亮像素里占多数，绿底信号
    被淹没（real1 的 6/7/9/10 绿槽全部误判 std，n 样本同样；回归期望
    与程序输出自洽，没暴露）。改用**全格中位数**：绿底占方块大面积，
    实测 std 9~11 / special 43~47（新旧样本同口径，阈值 18 仍无重叠）；
    灰色槽在 tint 判据就已返回 none，不走绿度。
    """
    g = to_gray(cell)
    r = cell[:, :, 0].astype(int)
    b = cell[:, :, 2].astype(int)
    bright = g >= np.percentile(g, BRIGHT_PCT)
    if bright.sum() < 8:
        return 'none', 999, 0, 0.0
    med = np.median(cell[bright], axis=0)
    tint = int(med[2]) - int(med[0])
    if tint > AVAIL_TINT_MAX:
        return 'none', tint, 0, 1.0
    med_all = np.median(cell.reshape(-1, 3), axis=0)
    green_val = int(med_all[1]) - (int(med_all[0]) + int(med_all[2])) // 2
    kind = 'special' if green_val > GREEN_THR else 'std'
    return kind, tint, green_val, round(float((g >= g.min()).mean()), 3)


# ---------- 角标数字 ----------
_tpl_cache = None


def _tpl():
    """加载 0~9 全覆盖字形模板（h_* 高亮体 / p_* 百分比体变体）"""
    global _tpl_cache
    if _tpl_cache is None:
        z = np.load(str(GLYPH_TPL))
        _tpl_cache = {k: z[k] for k in z.files}
    return _tpl_cache


def _badge_region(low):
    """下半部里的黑底 bbox（中性暗色连通块）

    绿底方块边缘可能与黑底连通 → 用高度先验过滤
    （角标实测高 16~30px，方块本体高 58~61px）。
    """
    boxes = connected_boxes(neutral_mask(low).astype(np.uint8), min_px=100)
    cand = [b for b in boxes
            if BADGE_BOX_H[0] <= (b[3] - b[1]) <= BADGE_BOX_H[1]
            and BADGE_BOX_W[0] <= (b[2] - b[0]) <= BADGE_BOX_W[1]]
    if not cand:
        return None
    cand.sort(key=lambda b: -((b[2] - b[0]) * (b[3] - b[1])))
    return cand[0]


def _digit_boxes(low):
    """下半部白字 → 数字笔画 bbox 列表

    两级筛选：
      尺寸   数字高 11~13、宽 ≤12；图标残片（弧线装饰）被尺寸剔除
      位置   块心须落在黑底 bbox 内（±3px），剔除残余污染
    实测两级合用后位数 143/143。
    """
    bm = (to_gray(low) > BADGE_WHITE).astype(np.uint8)
    boxes = connected_boxes(bm, min_px=DIG_MIN_PX)
    bb = _badge_region(low)
    out = []
    for b in boxes:
        w, h = b[2] - b[0], b[3] - b[1]
        if h < DIG_H_MIN or w > DIG_W_MAX:
            continue
        if bb is not None:
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            if not (bb[0] - BADGE_TOL <= cx <= bb[2] + BADGE_TOL
                    and bb[1] - BADGE_TOL <= cy <= bb[3] + BADGE_TOL):
                continue
        else:
            # 兜底先验：黑底框没检出时（个别截图会失败），
            # 角标恒在方块**下半部偏下**；图标边缘的细白条会从顶边开始，
            # 用它剔除。⚠ 只做「不贴顶」这一条，不做右半限制 ——
            # 3~4 位角标的最左一位会落在方块中部，限制横向会误杀。
            if b[1] < DIG_TOP_MIN_FRAC * low.shape[0]:
                continue
        out.append(b)
    out.sort(key=lambda b: b[0])
    return out, bm


def read_badge(cell):
    """读方块右下角黑底白字的库存数字 → (int|None, conf)

    思路：不靠黑底框切数字（绿底方块边缘会与之连通、bbox 溢出），
    而是在方块下半部用「白字连通域 + 尺寸/位置双筛」定位数字，
    再用 glyph_tpl.npz 做余弦模板匹配。
    """
    low = cell[int(cell.shape[0] * BAND_Y):, :]
    if low.size == 0:
        return None, False
    # 版式缩放（2026-10-11）：地图/部署界面在 1920 归一化后工具栏方块
    # 只有 ~52px（标定 58~61），角标数字缩小到 DIG_H_MIN 以下 → 全部
    # 读不出。方块高度不足时按 60px 基准放大再读（只影响角标，
    # 不影响槽位定位与灰绿分类）。
    if cell.shape[0] < 55:
        f = 60.0 / cell.shape[0]
        im = Image.fromarray(low.astype(np.uint8)).resize(
            (max(1, int(round(low.shape[1] * f))),
             max(1, int(round(low.shape[0] * f)))), Image.LANCZOS)
        low = np.array(im).astype(int)
    boxes, bm = _digit_boxes(low)
    if not boxes:
        return None, False
    tpl = _tpl()
    num, conf = '', True
    for b in boxes:
        v = glyph_vec(b, bm, w=16, h=24)
        if v is None:
            return None, False
        best, bs = None, -1.0
        for k, t in tpl.items():
            s = float(v @ t)
            if s > bs:
                bs, best = s, k
        num += best[-1]
        if bs < 0.55:
            conf = False
    return (int(num) if num.isdigit() else None), conf


# ---------- 主流程 ----------
def _step_of(centers):
    """由相邻中心差估计槽位步长（取接近 PITCH 的差值中位数）"""
    if len(centers) < 2:
        return PITCH
    diffs = sorted(centers[i+1] - centers[i] for i in range(len(centers)-1))
    near = [d for d in diffs if abs(d - PITCH) <= 6]
    return near[len(near) // 2] if near else PITCH


def _slot_index(centers, step):
    """由可见槽中心推断每个可见槽的槽位序号（0~10）

    核心：**必须按绝对 x 坐标逐段反推，不能按可见顺序编号**。
    灰色不可用槽在判据一下不可见，会造成整段缺失
    （真实截图可见 7 槽，真序号 1,2,3,6,7,9,10，缺 0/4/5/8/10）。

    算法（逐段独立判定）：
      对每一对相邻可见槽，其间距 d 决定「中间隔了几个槽位」：
          n_skip = round(d / step) - 1
      再用**右端锚点**确定绝对基准：实测末槽中心恒在
      1101~1118（占宽 0.573~0.583），故最后一个可见槽恒为 slot10。
      由右向左逐段累加 n_skip 反推全部序号。

    例（真实截图）：
      中心   323, 409, 494, 771, 856, 1027, 1113
      间距    86, 85, 277,85, 171, 86
      n_skip   0,  0,   2,  0,   1,  0
      右锚1113 = slot10 → 1027=9, 856=10-1-1=8 ✗

      注：856 真值为 slot7、1027 真值为 slot9，
      说明**末槽 1113 对应的是 slot10，但1027 与 1113 之间
      只隔 1 槽（间距 86）**，即 1027 = slot9 ✓；
      而 856 与 1027 间距 171 = 2 槽 → 856 = slot7 ✓
      → 左推：494(277=3槽) = slot6-3 = slot3 ✓ → 409=2, 323=1 ✓
      完全吻合。故基准是「最后一个可见槽 = slot10」，
      n_skip 按 round(d/step)-1 逐段累加。
    """
    n = len(centers)
    if n == 0:
        return []
    if n == 1:
        return [N_SLOT - 1]
    # 逐段跨越的槽位数
    skips = [0] * n
    for i in range(n - 1):
        d = centers[i+1] - centers[i]
        skips[i+1] = max(1, int(round(d / float(step))))
    # 右端锚定：最后一个可见槽 = slot10
    idx = [0] * n
    idx[n - 1] = N_SLOT - 1
    for i in range(n - 2, -1, -1):
        idx[i] = idx[i + 1] - skips[i + 1]
    return idx


def _expand_slots(a, y0, y1, centers, step, idx):
    """把可见槽外推成完整 N_SLOT 槽位列表（含灰色不可用槽）"""
    if not centers:
        return []
    # 每个可见槽的序号 → 中心 x
    by_idx = {k: c for k, c in zip(idx, centers)}
    # 用标准段（序号 < N_STD）与特殊段（序号 ≥ N_STD）分别拟合
    out = []
    for k in range(N_SLOT):
        if k in by_idx:
            cx = by_idx[k]
        else:
            # 取同段最近的已知槽外推
            same = [j for j in by_idx if (j < N_STD) == (k < N_STD)]
            if not same:
                out.append((k, None)); continue
            near = min(same, key=lambda j: abs(j - k))
            cx = by_idx[near] + (k - near) * step
        out.append((k, cx))
    return out


def slot_states(a):
    """返回 N_SLOT 个槽位的状态列表

    每项 dict：kind / slot / cx / count / conf / tint / green
    依据用户确认**排列固定**，故不做图标身份识别，
    只判「是否存在该插件（灰色=不存在）」与「库存数量」。

    灰色不可用槽在 `b−r` 判据下与地图底同色、无法直接检出，
    靠「可见槽等间距外推」补齐位置，再由亮像素 `b−r` 判为 `none`。
    """
    band = bar_rows(a)
    if band is None:
        return []
    y0, y1 = band
    runs = _cols_in_band(_not_map(a), y0, y1, a.shape[1])
    if not runs:
        return []
    centers = [(s + e) // 2 for s, e in runs]
    step = _step_of(centers)
    idx = _slot_index(centers, step)
    full = _expand_slots(a, y0, y1, centers, step, idx)

    out = []
    for k, cx in full:
        if cx is None:
            out.append({'slot': k, 'kind': 'none', 'cx': None,
                        'tint': None, 'count': None, 'conf': False,
                        'green': 0})
            continue
        cell = _cell_at(a, y0, y1, cx)
        kind, tint, green, frac = _classify(cell)
        count, conf = read_badge(cell)
        out.append({
            'kind': kind, 'slot': k, 'cx': cx, 'tint': tint,
            'count': count, 'conf': conf, 'green': green,
        })
    return out


def parse(path):
    """解析一张截图的底部工具栏 → 变项库存

    返回：
      file          文件名
      has_bar       是否检出工具栏
      n_slots       检出的槽位数
      std_slots     灰底（标准变项）槽位序号
      special_slots 绿底（特殊变项）槽位序号
      none_slots    灰色（不存在该插件）槽位序号
      inventory     {槽位序号: 库存数量}（仅含 count 非 None 的槽）
      slots         全部槽位明细
    """
    a = load_screen(path)      # 入口尺度归一化（槽间距 84~88 等常量按 1920 标定）
    slots = slot_states(a)
    n = len(slots)
    idx = lambda pred: [i for i, s in enumerate(slots) if pred(s)]
    return {
        'file': os.path.basename(path),
        'has_bar': n > 0,
        'n_slots': n,
        'std_slots': idx(lambda s: s['kind'] == 'std'),
        'special_slots': idx(lambda s: s['kind'] == 'special'),
        'none_slots': idx(lambda s: s['kind'] == 'none'),
        'inventory': {i: s['count'] for i, s in enumerate(slots)
                      if s['count'] is not None},
        'slots': slots,
    }


def apply_disables(res, disabled=None):
    """把用户填写的「禁用变项」并入工具栏解析结果

    背景
    ────
    变项的**身份**由用户按槽位序号填写（本模块只判「有哪些可用、各有多少个」，
    见模块 docstring），因此「哪些槽被禁用」同样只能由用户告知 ——
    禁用状态在截图上与普通可用槽**视觉上完全一致**，程序无法自行判断。

    以**全歼**为目标时，禁用变项通常不是约束（多一种可打总比少一种好），
    故默认 `disabled=None` 即「不考虑禁用」，由用户按需开启。

    参数
    ────
    disabled : 槽位序号集合，或 {序号: 名称} 映射，或 None
               None /空集合 → 不考虑禁用（全歼模式默认）

    返回
    ────
    原res 的副本，额外给出：
      disabled_slots  被禁用的槽位序号
      enabled_slots   实际可用的槽位序号（排除禁用与无插件）
    """
    res = dict(res)
    slots = [dict(s) for s in res['slots']]
    dis = set(disabled or ())
    for i, s in enumerate(slots):
        s['disabled'] = i in dis
        s['usable'] = (s['kind'] != 'none') and not s['disabled']
    res['slots'] = slots
    res['disabled_slots'] = sorted(i for i, s in enumerate(slots) if s['disabled'])
    res['enabled_slots'] = [i for i, s in enumerate(slots) if s['usable']]
    return res


def prompt_disables(n_slots, interactive=True):
    """交互式询问用户哪些槽位被禁用

    非交互环境（无 tty / 批量处理）下直接返回空集合，
    即按「不考虑禁用」处理 —— 与全歼模式的默认语义一致。"""
    if not interactive or n_slots <= 0:
        return set()
    print('\n当前有 %d 个变项槽位。若某些变项已被禁用，请输入其序号。' % n_slots)
    print('直接回车 = 不考虑禁用（全歼模式默认）')
    try:
        ans = input('禁用序号（逗号分隔）> ').strip()
    except (EOFError, OSError):
        return set()
    if not ans:
        return set()
    out = set()
    for tok in ans.replace('，', ',').split(','):
        tok = tok.strip()
        if tok.isdigit() and 0 <= int(tok) < n_slots:
            out.add(int(tok))
    return out


if __name__ == '__main__':
    import sys
    DATA = str(ROOT / 'data' / 'stage')
    FILES = ['kp1', 'kp2', 'kp3', 'boss', 'ex_kp1', 'ex_kp2', 'ex_kp3',
             'n1', 'n2', 'n3', 'n4', 'n5', 'n6', 'n7']

    # 无工具栏的样本：n4 为「带入」阶段界面，底部只有我方卡组栏，
    # 不存在变项工具栏 → 属正常形态，不是识别失败。
    NO_BAR = {'n4'}

    print('%-7s %-4s %-9s %-16s %-17s %-12s %s'
          % ('文件', '槽数', '间距', '标准(灰底)', '特殊(绿底)', '灰(无插件)', '库存'))
    paths = [(f, DATA + '/%s.jpg' % f) for f in FILES]
    if '--real' in sys.argv:
        rdir = ROOT / 'data' / 'stage_real'
        if rdir.is_dir():
            for rp in sorted(rdir.glob('*.jpg')):
                paths.append((rp.stem, str(rp)))
    # 禁用变项由用户填写：非交互或未加 --ask 时一律按「不考虑禁用」处理
    ask = '--ask' in sys.argv
    for f, p in paths:
        r = parse(p)
        if ask:
            r = apply_disables(r, prompt_disables(r['n_slots']))
        cx = [s['cx'] for s in r['slots'] if s.get('cx') is not None]
        pit = [cx[i + 1] - cx[i] for i in range(len(cx) - 1)]
        ps = '%d~%d' % (min(pit), max(pit)) if pit else '-'
        inv = r['inventory']
        invs = ' '.join('%d:%s' % (k, v) for k, v in sorted(inv.items()))
        note = '  (无工具栏: 带入界面)' if f in NO_BAR else ''
        if r.get('disabled_slots'):
            note += '  [禁用 %s → 可用 %s]' % (r['disabled_slots'], r['enabled_slots'])
        print('%-7s %-4d %-9s %-16s %-17s %-12s %s%s'
              % (f, r['n_slots'], ps, r['std_slots'], r['special_slots'],
                 r['none_slots'], invs, note))