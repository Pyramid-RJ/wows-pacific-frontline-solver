"""代币数字识别（分块架构 v3）

管线：
  卡列 → 卡牌 → 代币牌搜索区 → neutral_mask 定位黑方块
       → bright_mask 提白字 → 去边框 → 行投影取数字行 → 列投影切分 → 粘连拆分
       → 字形归一化 → 余弦相似度模板匹配 → 数值

已验证：黑方块定位 16/16，位数 16/16。
"""
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA = str(ROOT / 'data' / 'stage')

import numpy as np
import json
import os
from m2_layout import (load, bright_mask, neutral_mask, connected_boxes,
                       find_card_columns, card_vertical, token_plate_box)

FILES = ['kp1', 'kp2', 'kp3', 'boss']

# 人工读图确认的代币掉落真值（普通据点=次级货币，boss=进度代币）
TOKEN_TRUTH = {
    'kp1':  [12, 1, 13, 12],
    'kp2':  [12, 12, 12, 13],
    'kp3':  [12, 11, 13, 13],
    'boss': [600, 650, 2400, 600],
}

TPL_PATH = DATA + '/token_tpl.npz'


# ---------- 版式 ----------
def find_plate(a, col):
    """代币牌搜索区内用 neutral_mask 定位黑方块"""
    x0, x1 = col
    yt, yb = card_vertical(a, x0, x1)
    tb = token_plate_box({'bbox': (x0, yt, x1, yb)})
    sub = a[tb[1]:tb[3], tb[0]:tb[2]]
    boxes = connected_boxes(neutral_mask(sub).astype(np.uint8), min_px=120)
    cand = [b for b in boxes
            if 18 <= (b[2] - b[0]) <= 70 and (b[3] - b[1]) >= 18]
    if not cand:
        return None, sub
    cand.sort(key=lambda b: -(b[2] - b[0]))
    return cand[0], sub


def strip_border(bm):
    H, W = bm.shape
    m = bm.copy()
    m[:, :2] = 0
    m[:, max(0, W - 2):] = 0
    m[:2, :] = 0
    m[max(0, H - 3):, :] = 0
    return m


def digit_band(m):
    """行投影取数字行区间：选像素最多的连续行段"""
    rows = m.sum(axis=1)
    best = (0, 0, 0)
    y = 0
    H = len(rows)
    while y < H:
        if rows[y] > 0:
            s = y
            while y < H and rows[y] > 0:
                y += 1
            tot = rows[s:y].sum()
            if tot > best[0]:
                best = (tot, s, y)
        else:
            y += 1
    return best[1], best[2]


def _narrow_width(widths):
    """单字宽度的稳健基准

    ⚠ 用 **min** 而不是中位数：中位数在「只剩 2 段」时会等于较宽的那一段
    （如 [7, 17] → 17），于是既不会切分粘连、又会把正常的窄字误判为「被削窄」
    而去扩张。单字宽度取最小值在有粘连时仍然可靠 —— 至少有一个未粘连的字。
    """
    return min(widths)


def split_wide(m, spans):
    """拆分粘连字形：超宽 span 按列投影谷值再切一刀

    `m` 必须是**数字行带**（`digit_band` 的结果），不能是整块掩膜 ——
    牌底常有一条通宽的白边，它会让列投影处处不为零、谷值彻底消失。

    ⚠ 谷值位置必须换算回**块内绝对坐标**（`s + lo + argmin`），漏了 span
    起点 `s` 时 `s+2 < g < e-2` 恒不成立，拆分逻辑不生效。
    """
    if len(spans) < 2:
        return spans
    h = m.shape[0]
    widths = [e - s for s, e in spans]
    narrow = _narrow_width(widths)
    # 单字宽度上限（2026-10-12，4.25）：数字字形不会比**行高**更宽 ——
    # 同字体实测单字宽 ≈ 0.4~0.7×行高（最长 10px / 行高 14）。
    # ⚠ 缺这个上限时，`_narrow_width`(min) 会被「1」压到 3px → 阈值 6.8，
    #   于是宽度正常的「0」也被当成粘连字，从**内孔(counter)**切一刀
    #   （实测牌面「1100」→ 切成 6 块 → 读成 114313）。
    cap = 0.9 * h
    out = []
    for (s, e) in spans:
        if e - s <= narrow * 1.6 + 2 or e - s <= cap:
            out.append((s, e))
            continue
        cols = m[:, s:e].sum(axis=0)
        lo, hi = int(len(cols) * 0.35), int(len(cols) * 0.65)
        g = s + lo + int(np.argmin(cols[lo:hi]))
        if s + 2 < g < e - 2:
            out += [(s, g), (g, e)]
        else:
            out.append((s, e))
    return out


def repair_narrow(spans, cols):
    """修复被阈值切窄的字形：若某 span 明显窄于同牌中位宽度，
    向两侧扩张到列投影归零处（补回被阈值削掉的笔画）"""
    if len(spans) < 2:
        return spans
    widths = [e - s for s, e in spans]
    med = _narrow_width(widths)
    out = []
    for (s, e) in spans:
        if e - s >= med * 0.75:
            out.append((s, e))
            continue
        s2 = s
        while s2 > 0 and cols[s2 - 1] > 0:
            s2 -= 1
        e2 = e
        W = len(cols)
        while e2 < W and cols[e2] > 0:
            e2 += 1
        out.append((s2, e2))
    return out


def digit_boxes(bm):
    """黑方块白字掩膜 → 字符 bbox 列表（原图坐标系的块内坐标）"""
    m = strip_border(bm)
    W = m.shape[1]
    y0, y1 = digit_band(m)
    if y1 <= y0:
        return []
    band = m[y0:y1]
    cols = band.sum(axis=0)
    thr = max(3, int((y1 - y0) * 0.20))
    spans, inr = [], False
    for x in range(W):
        if cols[x] >= thr and not inr:
            inr, s = True, x
        elif cols[x] < thr and inr:
            inr = False
            if x - s >= 2:
                spans.append((s, x))
    if inr and W - s >= 2:
        spans.append((s, W))
    spans = repair_narrow(split_wide(band, spans), cols)
    return [(s, y0, e, y1) for (s, e) in spans]


# ---------- 字形与模板 ----------
def glyph_vec(box, bm, w=16, h=24):
    s, y0, e, y1 = box
    g = bm[y0:y1, s:e]
    ys, xs = np.where(g > 0)
    if len(ys) == 0:
        return None
    g = g[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.float32)
    from PIL import Image
    im = Image.fromarray((g * 255).astype(np.uint8)).resize((w, h), Image.BILINEAR)
    v = np.array(im, dtype=np.float32).ravel() / 255.0
    v -= v.mean()
    n = np.linalg.norm(v)
    return v / n if n > 0 else None


def build_templates():
    """从 16 张已标注卡牌收集字符样本，按数字取中位数"""
    samples = {}
    for f in FILES:
        a = load(DATA + '/%s.jpg' % f)
        for i, col in enumerate(find_card_columns(a)):
            b, sub = find_plate(a, col)
            if b is None:
                continue
            bm = bright_mask(sub[b[1]:b[3], b[0]:b[2]]).astype(np.uint8)
            boxes = digit_boxes(bm)
            truth = str(TOKEN_TRUTH[f][i])
            if len(boxes) != len(truth):
                print('  !! %s 卡%d 位数不符 %d vs %s' % (f, i + 1, len(boxes), truth))
                continue
            for bx, ch in zip(boxes, truth):
                v = glyph_vec(bx, bm)
                if v is not None:
                    samples.setdefault(ch, []).append(v)
    tpl = {}
    for ch, vs in samples.items():
        A = np.array(vs)
        tpl[ch] = np.median(A, axis=0)
        tpl[ch] /= np.linalg.norm(tpl[ch])
    return tpl, {k: len(v) for k, v in samples.items()}


def save_templates(tpl):
    np.savez(TPL_PATH, **{k: v for k, v in tpl.items()})


def load_templates():
    z = np.load(TPL_PATH)
    return {k: z[k] for k in z.files}


def recognize(bm, boxes, tpl, thr=0.80):
    out, conf = '', True
    for bx in boxes:
        v = glyph_vec(bx, bm)
        if v is None:
            return None, False
        best, bs = '?', -1
        for ch, t in tpl.items():
            s = float(v @ t)
            if s > bs:
                bs, best = s, ch
        out += best
        if bs < thr:
            conf = False
    return (int(out) if out.isdigit() else None), conf


# ---------- 主流程 ----------
def read_all(verbose=True):
    tpl = load_templates() if os.path.exists(TPL_PATH) else build_templates()[0]
    total = ok = 0
    results = {}
    for f in FILES:
        a = load(DATA + '/%s.jpg' % f)
        vals = []
        for i, col in enumerate(find_card_columns(a)):
            b, sub = find_plate(a, col)
            if b is None:
                vals.append(None)
                continue
            bm = bright_mask(sub[b[1]:b[3], b[0]:b[2]]).astype(np.uint8)
            boxes = digit_boxes(bm)
            v, conf = recognize(bm, boxes, tpl)
            vals.append(v)
            truth = TOKEN_TRUTH[f][i]
            total += 1
            good = (v == truth)
            ok += good
            if verbose:
                print('  %s 卡%d 识别=%s 真值=%d %s%s'
                      % (f, i + 1, v, truth, 'OK' if good else 'FAIL',
                         '' if conf else ' (低置信)'))
        results[f] = vals
    if verbose:
        print('\n代币数值 %d/%d' % (ok, total))
    return results, ok, total


if __name__ == '__main__':
    tpl, cnt = build_templates()
    print('模板字符:', {k: v for k, v in sorted(cnt.items())})
    save_templates(tpl)
    read_all()
