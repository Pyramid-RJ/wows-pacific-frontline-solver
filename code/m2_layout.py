"""分块识别框架（v2 架构）

核心思想：**先按语义分块，再在块内识别**，而不是在全图上做全局阈值分割。
原因：游戏截图存在多套背景（青绿渐变地图底、浅色卡面、深色侧栏），
      全局阈值必然同时误伤不同区域；分块后每块背景单一，判据可大幅简化。

块结构（实测 1920×806~830）
┌──────────────────────────────────────────────┬─────────┐
│ A区 敌方卡牌列×4+外挂代币牌  y 0~300│ 侧栏   │
│      ├ 卡头 y 0~90（弱点图标+战力数字）      │         │
│      ├ 卡身 y 90~290（底色档+舰体图）       │         │
│      └ 代币牌：外挂于卡牌右上（黑底白字）    │         │
├──────────────────────────────────────────────┴─────────┤
│ B区 中部部署区（拖拽槽位）                                │
├─────────────────────────────────────────────────────────┤
│ C区 底部工具栏 y 670~730（变项图标+角标数量）             │
└─────────────────────────────────────────────────────────┘

每块内部再细分到元素级（见各 *_block 函数）。
"""
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA = str(ROOT / 'data' / 'stage')

import numpy as np
from PIL import Image

# ============ 通用工具 ============
BASE_W = 1920          # 基准宽度：识别层全部版式常量按此标定
# 右侧条件面板的左边界占宽比（实测 1482/1920 = 0.772）—— 面板底面也是浅色，
# 列扫描会把它抓成「段」，必须与卡牌列区分开（见 find_card_columns）。
PANEL_X = 0.772

def load(path):
    return np.array(Image.open(path).convert('RGB')).astype(int)


def load_screen(path, base_w=BASE_W):
    """读取整屏游戏截图 → 统一缩放到基准宽度（默认 1920）

    识别层的版式常量（卡牌列宽窗口、卡头/代币牌偏移、图标半径、
    工具栏槽间距等）全部按 **1920 宽**标定。实测玩家的截图可能是
    1.23× 的 UI 缩放（如 2375×1061），直接喂入会让卡牌列合并、代币牌
    窗口落空、图标尺度错位 → 识别结果错乱（战力读成 None）甚至崩溃。

    因此在**入口**做一次尺度归一化，而不是逐个模块改常量。
    已经是基准宽度（±1%）的截图不重采样，返回像素与 `load()` 完全一致。

    注意：仍用 `load()` 的地方是**模板/标注图**（`weak_ref_clean/`、
    `weak_labeled/`、`glyph_tpl.npz`），它们本就按原生尺度标定，不要缩放。
    """
    im = Image.open(path).convert('RGB')
    w, h = im.size
    if abs(w - base_w) <= base_w * 0.01:
        return np.array(im).astype(int)
    im = im.resize((base_w, max(1, int(round(h * base_w / float(w))))),
                   Image.LANCZOS)
    return np.array(im).astype(int)


def to_gray(a):
    return a.mean(axis=2)


def light_mask(a):
    """浅色实体（卡面、白色牌子）：高亮度 + 低饱和"""
    mx = a.max(axis=2).astype(int)
    mn = a.min(axis=2).astype(int)
    return (mx > 185) & ((mx - mn) < 40)


def neutral_mask(a):
    """中性暗色（黑底方块）：r≈g≈b 且 亮度低。
    这是区分「黑方块」与「青绿地图底」的关键 ——
    地图底灰度仅 ~57，但它有明显的 b>g 偏色。"""
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    neutral = (abs(r - b) < 26) & (abs(g - b) < 26)
    return neutral & (to_gray(a) < 78)


def bright_mask(a, thr=150):
    """高亮白字（黑底上的数字）

    thr 固定 150 在早期样本上够用，但抗锯齿会削掉「1」的细笔画
    （实测新样本中「1」只剩 3px 宽，被误识为 6）。
    因此提供 otsu_mask 作为块内自适应方案。"""
    return (a[:, :, 0] > thr) & (a[:, :, 1] > thr) & (a[:, :, 2] > thr)


def otsu_threshold(g):
    """Otsu 双峰阈值：黑底白字块内灰度呈明显双峰
    （实测背景 ~32、文字 ~230，中间几乎无像素），
    用统计量而非固定值取阈值，可同时适配不同压缩程度的截图。"""
    hist = np.bincount(np.clip(g, 0, 255).astype(int).ravel(), minlength=256).astype(float)
    total = hist.sum()
    if total == 0:
        return 128
    w0 = np.cumsum(hist)
    w1 = total - w0
    m0 = np.cumsum(hist * np.arange(256))
    m1 = m0[-1] - m0
    ok = (w0 > 0) & (w1 > 0)
    if not ok.any():
        return 128
    var = np.zeros(256)
    var[ok] = (m1[ok] * w0[ok] - (total - m0[ok]) * w1[ok]) ** 2 / (w0[ok] * w1[ok])
    return int(np.argmax(var))


def otsu_mask(a):
    """块内自适应白字掩膜"""
    t = otsu_threshold(to_gray(a))
    return bright_mask(a, t), t


def connected_boxes(mask, min_px=8):
    """2D 连通域bbox（BFS），返回 [(x0,y0,x1,y1)]"""
    H, W = mask.shape
    seen = np.zeros((H, W), dtype=bool)
    out = []
    for sy in range(H):
        row = mask[sy]
        for sx in range(W):
            if not row[sx] or seen[sy, sx]:
                continue
            stack = [(sy, sx)]
            seen[sy, sx] = True
            xs, ys, n = [], [], 0
            while stack:
                y, x = stack.pop()
                xs.append(x); ys.append(y); n += 1
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < H and 0 <= nx < W and mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if n >= min_px:
                out.append((min(xs), min(ys), max(xs) + 1, max(ys) + 1))
    return out


def row_blocks(boxes):
    """把连通域按「行起点」聚类（同一基线的块归为一行）"""
    if not boxes:
        return []
    from collections import defaultdict
    byrow = defaultdict(list)
    for b in boxes:
        byrow[b[1]].append(b)
    return byrow


def pick_digit_row(boxes, min_ratio=0.55):
    """从连通域中挑出「数字行」
    数字特征：同一基线、等高、等宽（5~14px）。
    图标/文字残块特征：更宽（≥15px）或更矮。"""
    if not boxes:
        return []
    byrow = row_blocks(boxes)
    best, best_key = None, None
    for y, lst in byrow.items():
        hs = [b[3] - b[1] for b in lst]
        ws = [b[2] - b[0] for b in lst]
        hmax = max(hs)
        # 该行内与最高块等高的块数越多、且宽度越窄 → 越像数字行
        same_h = [b for b in lst if (b[3] - b[1]) >= hmax * min_ratio]
        narrow = sum(1 for b in same_h if (b[2] - b[0]) <= 14)
        key = (len(same_h), narrow, hmax)
        if best_key is None or key > best_key:
            best, best_key = lst, key
    return sorted(best, key=lambda b: b[0])


def merge_fragments(boxes, max_w=14, overlap=0.7):
    """合并同一字形被抗锯齿切碎的碎片（x 重叠≥70% 且宽≤max_w）"""
    boxes = sorted(boxes, key=lambda b: (b[0], b[1]))
    out = []
    for b in boxes:
        hit = None
        for i, m in enumerate(out):
            ov = min(m[2], b[2]) - max(m[0], b[0])
            wm, wb = m[2] - m[0], b[2] - b[0]
            if ov > 0 and ov > overlap * min(wm, wb) and max(wm, wb) <= max_w:
                hit = i
                break
        if hit is None:
            out.append(b)
        else:
            m = out[hit]
            out[hit] = (min(m[0], b[0]), min(m[1], b[1]),
                        max(m[2], b[2]), max(m[3], b[3]))
    return out


def glyph_norm(mask, w=20, h=28):
    """字符掩膜 → 归一化向量（去均值 + 单位化）"""
    ys, xs = np.where(mask > 0)
    if len(ys) == 0:
        return None
    g = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    im = Image.fromarray((g * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    v = (np.array(im, dtype=np.float32) / 255.0).ravel()
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def crop_glyphs(mask, spans, y0, rh):
    """从掩膜按 (x0,x1) 切出字符（限定行高，避免竖笔伸入邻行）"""
    out = []
    for (s, e) in spans:
        sub = mask[y0:y0 + rh, s:e]
        if sub.sum() < 6:
            out.append(None)
            continue
        out.append(sub.astype(np.uint8))
    return out


# ============ A 区：卡牌列 ============
def find_card_columns(a):
    """定位 4 张敌方卡的横向范围
    卡面是高亮度浅色实体；地图底为青绿色（饱和度高）

    众数宽度过滤（2026-10-11）：卡间的掉落徽章（金盘/硬币柱，宽 ~40px）
    可能刚好越过 40px 最小宽度阈值混进 runs，把第 4 张卡挤出 runs[:4]
    （实测 PixPin_17-54-24：powers=[141,None,166,188]，卡2=None 即徽章列）。
    卡是画面里**最宽**的浅色结构且宽度一致 —— 按最大宽度过滤
    （< 65% 最大宽度的区段丢弃）。⚠ 不能用众数：杂窄段一多，
    众数本身落进窄段，会把真卡全滤掉（实测 s1_kp3）。
    """
    lm = light_mask(a)
    H, W = lm.shape
    band = lm[:int(H * 0.42)]
    colsum = band.sum(axis=0)
    # 阈值：占该带高度 6% 以上
    th = int(H * 0.42 * 0.06)
    runs, inr = [], False
    for x in range(W):
        if colsum[x] > th and not inr:
            inr, s = True, x
        elif colsum[x] <= th and inr:
            inr = False
            if 40 < x - s < 300:
                runs.append((s, x))
    if inr and 40 < W - s < 300:
        runs.append((s, W))
    # **面板区**（x ≥ 0.772W）的浅色底面也会被列扫描抓成段（实测
    # PixPin_13-56-17 的第 5 段 248px 就是面板纸面、PixPin_22-13-09 的
    # 第 5~7 段 136/117/87px 同理）—— 一律不算卡。不剔掉会污染下面的
    # 「宽度一致性」参考宽度（实测把 136px 的碎段当参考 → 四张卡全被截成
    # 136px、战力读成 2）。
    xp = int(W * PANEL_X)
    runs = [(s, e) for s, e in runs if s < xp]
    if len(runs) > 4:
        wmax = max(e - s for s, e in runs)
        runs = [(s, e) for s, e in runs if e - s >= 0.65 * wmax]
    # 宽度一致性（2026-10-12，4.21 #4）：同一排 4 张卡**同宽**。卡**右侧**的
    # 掉落徽章（金盘/硬币柱，宽 ~67px）也是浅色，与卡之间没有低于阈值的空隙
    # → 连成一段把该列撑宽（实测 13-57-57 列2 = 276px、同排其余 209px；
    # 13-59-31 列3 同理）→ 战力 ROI 右移、首位数字被挤出窗口
    # （147→47、126→26），且**静默**不报低置信。
    # 离群过宽的段按**参考宽度**向右截断（多出的部分一律在卡的右侧）。
    #
    # ⚠ 2026-10-13：参考宽度必须取**最窄**的那一列 —— 徽章只会往列的**右侧**
    # 加宽、不会让它变窄，所以「真实卡宽 = 观测到的最小宽度」。
    # 原先用中位宽：一排 4 张里**两张**带徽章时（玩家实拍 阶段2 据点3：
    # [274, 208, 274, 214]），sorted 后取 `ws[2]`（偶数个取上中位）= **274**
    # → 阈值 315，一个都没截 → 首位「1」仍被挤出窗口（143 读成 43、149 读成 49）。
    if len(runs) >= 3:
        wmin = min(e - s for s, e in runs)
        runs = [(s, s + wmin if e - s > wmin * 1.15 else e) for s, e in runs]
    return runs[:4]


def card_vertical(a, x0, x1):
    """卡牌纵向范围：**按内容检测**，兼容两种界面

    旧版把卡顶硬编码 y=8、底边只在 y∈[230,360) 扫 —— 只适配
    「敌方卡贴着屏幕顶」的战斗界面。地图/部署界面（如 2560×1440
    全屏截图，2026-10-11 用户实测）的卡行整体下移，卡头窗口错位 →
    战力读 None。

    卡内中段的**海面蓝带与地图底同色**，不能按「转青」找卡底
    （旧口径靠「从 230 才开始扫」回避，界面一变即失效）。现口径：
      - 卡顶 = 列内第一段「实心亮行」（frac_light > 0.75 且连续 8 行）；
      - 卡底 = 卡顶 100 行之后，第一个「亮行 + 随后 ≥15 行持续非亮」
        的行 —— 即**卡名条的底边**（海面/船体行不是亮行，不会提前命中）。
    """
    lm = light_mask(a)
    H = lm.shape[0]
    col = lm[:, x0 + 4:x1 - 4]
    frac_light = col.mean(axis=1)

    yt = None
    y = 1
    while y < int(H * 0.55) - 8:
        if frac_light[y] > 0.75 and frac_light[y:y + 8].min() > 0.5:
            yt = y
            break
        y += 1
    if yt is None:
        return 8, 300                    # 兜底：旧行为

    yb = None
    for y2 in range(yt + 100, int(H * 0.62)):
        if frac_light[y2] > 0.45 and frac_light[y2 + 1:y2 + 16].max() < 0.35:
            yb = y2
            break
    return yt, (yb if yb and yb > yt + 150 else yt + 300)


def card_header_box(card):
    """卡头区：弱点图标 + 战力数字（卡内上部 2%~22%）"""
    x0, y0, x1, y1 = card['bbox']
    w, h = x1 - x0, y1 - y0
    return (int(x0 + w * 0.55), int(y0 + h * 0.02),
            int(x1) - 18, int(y0 + h * 0.20))


def card_body_box(card):
    """卡身区：底色档位采样（卡内 27%~64%，避开舰体图上下文字）"""
    x0, y0, x1, y1 = card['bbox']
    w, h = x1 - x0, y1 - y0
    return (int(x0 + w * 0.05), int(y0 + h * 0.27),
            int(x0 + w * 0.95), int(y0 + h * 0.64))


def token_plate_box(card):
    """代币牌区：外挂于卡牌右上侧
    实测（1920 宽）：黑方块位于卡右上角右移 20~100px、下移 30~100px
    """
    x1, y0 = card['bbox'][2], card['bbox'][1]
    return (int(x1) - 4, int(y0), int(x1) + 125, int(y0) + 135)


if __name__ == '__main__':
    import sys
    a = load(DATA + '/boss.jpg')
    H, W, _ = a.shape
    cols = find_card_columns(a)
    print('卡牌列:', cols)
    for i, (x0, x1) in enumerate(cols):
        yt, yb = card_vertical(a, x0, x1)
        card = {'bbox': (x0, yt, x1, yb), 'w': x1 - x0, 'h': yb - yt}
        hb = card_header_box(card)
        bb = card_body_box(card)
        tb = token_plate_box(card)
        # 卡头：暗色笔画（战力数字）
        gm = to_gray(a[hb[1]:hb[3], hb[0]:hb[2]])
        dark = (gm < 118).astype(np.uint8)
        dark[:2, :] = 0
        boxes = connected_boxes(dark, min_px=10)
        merged = merge_fragments(boxes)
        digit_boxes = [b for b in merged if (b[2] - b[0]) <= 13]
        row = pick_digit_row(digit_boxes) if digit_boxes else []
        print('卡%d bbox=%s 卡头=%s 卡身=%s 代币牌=%s' %
              (i + 1, card['bbox'], hb, bb, tb))
        print('     暗块%d 合并后%d 数字行=%s' %
              (len(boxes), len(merged), [(b[0], b[2]) for b in row]))