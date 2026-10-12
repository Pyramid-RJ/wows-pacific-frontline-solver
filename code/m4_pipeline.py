"""统一识别管线 v4（分块架构）

把三类字段串成一条链路：
  1. 卡牌列 / 卡牌 bbox      —— 版式分块
  2. 战力数字               —— 卡头暗色笔画，模板匹配
  3. 代币掉落               —— 代币牌黑方块内白字，模板匹配
  4. 卡面底色档位            —— 卡身主色聚类

字形模板统一库：卡头（浅底深字）与代币（黑底白字）极性相反但形状一致，
裁 bbox → 归一化 → 余弦相似度，因此可互相借用样本，
从而补齐代币牌缺失的 7/8/9 字形。
"""
import os
import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
DATA = str(ROOT / 'data' / 'stage')

import numpy as np
from PIL import Image
from m2_layout import (load, load_screen, to_gray, light_mask, neutral_mask,
                       bright_mask,
                       otsu_mask, otsu_threshold,
                       connected_boxes, merge_fragments, find_card_columns,
                       card_vertical, card_header_box, card_body_box,
                       token_plate_box)
from m3_token import (find_plate, strip_border, digit_band, split_wide,
                      repair_narrow, glyph_vec, digit_boxes)
from m6_coin import read_token_type

TPL = DATA + '/glyph_tpl.npz'

# 含新示例（ex_* 与kp* 编成相同，仅牌面渲染不同，用于验证鲁棒性）
FILES = ['kp1', 'kp2', 'kp3', 'ex_kp1', 'ex_kp2', 'ex_kp3', 'boss']

TRUTH_POWER = {
    'kp1':  [149, 145, 132, 141],
    'kp2':  [83, 112, 114, 112],
    'kp3':  [165, 137, 131, 164],
    'boss': [164, 191, 227, 208],
    'ex_kp1': [149, 145, 132, 141],
    'ex_kp2': [83, 112, 114, 112],
    'ex_kp3': [165, 137, 131, 164],
}
TRUTH_TOKEN = {
    'kp1':  [12, 1, 13, 12],
    'kp2':  [12, 12, 12, 13],
    'kp3':  [12, 11, 13, 13],
    'boss': [600, 650, 2400, 600],
    'ex_kp1': [12, 1, 13, 12],
    'ex_kp2': [12, 12, 12, 13],
    'ex_kp3': [12, 11, 13, 13],
}
TRUTH_TIER = {
    'kp1':  ['blue', 'blue', 'blue', 'blue'],
    'kp2':  ['white', 'white', 'white', 'white'],
    'kp3':  ['blue', 'blue', 'blue', 'blue'],
    'boss': ['gold', 'gold', 'red', 'gold'],
    'ex_kp1': ['blue', 'blue', 'blue', 'blue'],
    'ex_kp2': ['white', 'white', 'white', 'white'],
    'ex_kp3': ['blue', 'blue', 'blue', 'blue'],
}


# ---------- 卡头战力 ----------
def power_boxes(a, card):
    """卡头暗色笔画 → 数字 bbox（块内坐标）"""
    hb = card_header_box(card)
    gm = to_gray(a[hb[1]:hb[3], hb[0]:hb[2]])
    dark = (gm < 118).astype(np.uint8)
    dark[:2, :] = 0
    boxes = connected_boxes(dark, min_px=10)
    merged = merge_fragments(boxes)
    narrow = [b for b in merged if 4 <= (b[2] - b[0]) <= 14]
    if not narrow:
        return None, None
    # 选「块数最多且高度一致」的一行
    from collections import defaultdict
    # 数字行选取：按 y 中心聚类（容差 4px）取最多块的一行。
    # 旧 b[1]//3 固定分桶会被 1px 之差劈开同一行（实测「110」丢首位 1，
    # 2026-10-11 关卡_据点4）。
    cands = sorted(narrow, key=lambda b: (b[1] + b[3]) // 2)
    clusters = []
    for b in cands:
        c = (b[1] + b[3]) // 2
        if clusters and abs(c - clusters[-1][0]) <= 4:
            clusters[-1][1].append(b)
        else:
            clusters.append((c, [b]))
    best = max(clusters, key=lambda cl: len(cl[1]))[1]
    row = sorted(best, key=lambda b: b[0])
    H = dark.shape[0]
    ys = [b[1] for b in row] + [b[3] for b in row]
    y0, y1 = max(0, min(ys) - 1), min(H, max(ys) + 1)
    return dark, [(s, y0, e, y1) for (s, _y, e, _y2) in row]


def power_mask(a, card, upscale=False):
    """返回卡头暗色掩膜与其数字 bbox

    `upscale`（2026-10-11）：卡宽 <240 时按 255 基准放大灰度图再二值化。
    地图/部署界面（2560 全屏）的小字号在阈值 118 下笔画断裂：
    6 断碗成 5、8 断腰成 3（实测 BOSS 图 161→151、187→137，放大后恢复）。
    ⚠ 不默认开启 —— 老样本（同为 ~207px）在原生尺度 28/28，放大反而
    个别数字粘连（191→11）；由 parse 在**原生读数低置信时**重试放大。
    """
    hb = card_header_box(card)
    reg = a[hb[1]:hb[3], hb[0]:hb[2]]
    cw = card.get('w') or (card['bbox'][2] - card['bbox'][0])
    if upscale and cw < 240:
        f = 255.0 / cw
        im = Image.fromarray(reg.astype(np.uint8)).resize(
            (max(1, int(round(reg.shape[1] * f))),
             max(1, int(round(reg.shape[0] * f)))), Image.LANCZOS)
        reg = np.array(im).astype(int)
    gm = to_gray(reg)
    dark = (gm < 118).astype(np.uint8)
    dark[:2, :] = 0
    boxes = connected_boxes(dark, min_px=10)
    merged = merge_fragments(boxes)
    narrow = [b for b in merged if 4 <= (b[2] - b[0]) <= 14]
    if not narrow:
        return None, []
    from collections import defaultdict
    # 数字行选取：按 y 中心聚类（容差 4px）取最多块的一行。
    # 旧 b[1]//3 固定分桶会被 1px 之差劈开同一行（实测「110」丢首位 1，
    # 2026-10-11 关卡_据点4）。
    cands = sorted(narrow, key=lambda b: (b[1] + b[3]) // 2)
    clusters = []
    for b in cands:
        c = (b[1] + b[3]) // 2
        if clusters and abs(c - clusters[-1][0]) <= 4:
            clusters[-1][1].append(b)
        else:
            clusters.append((c, [b]))
    best = max(clusters, key=lambda cl: len(cl[1]))[1]
    row = sorted(best, key=lambda b: b[0])
    H = dark.shape[0]
    ys = [b[1] for b in row] + [b[3] for b in row]
    y0, y1 = max(0, min(ys) - 1), min(H, max(ys) + 1)
    return dark, [(b[0], y0, b[2], y1) for b in row]


# ---------- 卡面底色档位 ----------
def _classify_row(r, g, b):
    """单行主色 → 档位标签（判据来自已验证 16/16 的旧版实现）"""
    r, g, b = float(r), float(g), float(b)
    mx, mn = max(r, g, b), min(r, g, b)
    if mx - mn < 22:
        return 'white'
    if b >= r - 8:
        return 'blue'
    if r > 150 and g > 110 and b < 120 and (r - b) > 55 and (g - b) > 25:
        return 'gold'
    return 'red'


def read_tier(a, card):
    """卡身区逐行取中位色 → 投票得档位。
    白底卡：白行 > 60%；否则取非白档中最多者。"""
    bb = card_body_box(card)
    reg = a[bb[1]:bb[3], bb[0]:bb[2]]
    if reg.size == 0:
        return 'unknown'
    rowmed = np.median(reg.reshape(reg.shape[0], -1, 3), axis=1)
    tags = [_classify_row(*rowmed[i]) for i in range(len(rowmed))]
    from collections import Counter
    cnt = Counter(tags)
    if cnt['white'] / len(tags) > 0.60:
        return 'white'
    nz = [(t, c) for t, c in cnt.items() if t != 'white']
    if not nz:
        return 'white'
    return max(nz, key=lambda z: z[1])[0]


# ---------- 模板库 ----------
def collect_samples():
    """从卡头战力 + 代币牌同时收集字形样本"""
    from m3_token import digit_boxes
    samples = {}
    for f in FILES:
        a = load(DATA + '/%s.jpg' % f)
        for i, col in enumerate(find_card_columns(a)):
            yt, yb = card_vertical(a, col[0], col[1])
            card = {'bbox': (col[0], yt, col[1], yb)}
            # 卡头
            dm, boxes = power_mask(a, card)
            pv = str(TRUTH_POWER[f][i])
            if dm is not None and len(boxes) == len(pv):
                for bx, ch in zip(boxes, pv):
                    v = glyph_vec(bx, dm)
                    if v is not None:
                        samples.setdefault(ch, []).append(v)
            # 代币
            b, sub = find_plate(a, col)
            if b is None:
                continue
            bm, _ = otsu_mask(sub[b[1]:b[3], b[0]:b[2]])
            bm = bm.astype(np.uint8)
            tb = digit_boxes(bm)
            tv = str(TRUTH_TOKEN[f][i])
            if len(tb) == len(tv):
                for bx, ch in zip(tb, tv):
                    v = glyph_vec(bx, bm)
                    if v is not None:
                        samples.setdefault(ch, []).append(v)
    return samples


def _median_tpl(samples):
    out = {}
    for ch, vs in samples.items():
        m = np.median(np.array(vs), axis=0)
        n = np.linalg.norm(m)
        if n > 0:
            out[ch] = m / n
    return out


def build(save=True):
    """按域隔离构建模板：卡头域与代币域的字形笔画粗细/抗锯齿不同，
    混用会互相污染，因此各自独立。对缺失字形（7/8/9 等）跨域借用。"""
    from m3_token import digit_boxes
    head, plate = {}, {}
    for f in FILES:
        a = load(DATA + '/%s.jpg' % f)
        for i, col in enumerate(find_card_columns(a)):
            yt, yb = card_vertical(a, col[0], col[1])
            card = {'bbox': (col[0], yt, col[1], yb)}
            dm, boxes = power_mask(a, card)
            pv = str(TRUTH_POWER[f][i])
            if dm is not None and len(boxes) == len(pv):
                for bx, ch in zip(boxes, pv):
                    v = glyph_vec(bx, dm)
                    if v is not None:
                        head.setdefault(ch, []).append(v)
            b, sub = find_plate(a, col)
            if b is None:
                continue
            bm, _ = otsu_mask(sub[b[1]:b[3], b[0]:b[2]])
            bm = bm.astype(np.uint8)
            tb = digit_boxes(bm)
            tv = str(TRUTH_TOKEN[f][i])
            if len(tb) == len(tv):
                for bx, ch in zip(tb, tv):
                    v = glyph_vec(bx, bm)
                    if v is not None:
                        plate.setdefault(ch, []).append(v)
    th = _median_tpl(head)
    tp = _median_tpl(plate)
    # 代币域缺字形 → 用卡头域补（形状一致，仅笔画渲染略有差异）
    borrowed = []
    for ch in list(th):
        if ch not in tp:
            tp[ch] = th[ch]
            borrowed.append(ch)
    if save:
        np.savez(TPL,
                 **{('h_' + k): v for k, v in th.items()},
                 **{('p_' + k): v for k, v in tp.items()})
    cnt = {k: len(v) for k, v in sorted(head.items())}
    return {'head': th, 'plate': tp}, cnt, borrowed


def load_tpl():
    z = np.load(TPL)
    th = {k[2:]: z[k] for k in z.files if k.startswith('h_')}
    tp = {k[2:]: z[k] for k in z.files if k.startswith('p_')}
    return {'head': th, 'plate': tp}


def digits(mask, boxes, tpl, thr=0.72):
    out, low = '', False
    for bx in boxes:
        v = glyph_vec(bx, mask)
        if v is None:
            return None, True
        best, bs = '?', -2
        for ch, t in tpl.items():
            s = float(v @ t)
            if s > bs:
                bs, best = s, ch
        out += best
        if bs < thr:
            low = True
    return (int(out) if out.isdigit() else None), low


# ---------- 全流程 ----------
def parse(path):
    a = load_screen(path)      # 入口尺度归一化（任意分辨率 → 1920 基准）
    cols = find_card_columns(a)
    tpl = load_tpl()
    out = []
    for col in cols:
        yt, yb = card_vertical(a, col[0], col[1])
        card = {'bbox': (col[0], yt, col[1], yb)}
        dm, pbox = power_mask(a, card)
        power, plow = (digits(dm, pbox, tpl['head']) if dm is not None else (None, True))
        # 低置信重试：放大二值化可修复小字笔画断裂（6→5、8→3），
        # 但对老样本偶发粘连 —— 只在原生读数 None/低置信时采用放大结果
        if (power is None or plow) and dm is not None:
            dm2, pbox2 = power_mask(a, card, upscale=True)
            if dm2 is not None:
                power2, plow2 = digits(dm2, pbox2, tpl['head'])
                if power2 is not None and not plow2:
                    power, plow = power2, plow2
        tb, tsub = find_plate(a, col)
        if tb is None:
            token, tlow, blk = None, True, None
        else:
            bm, othr = otsu_mask(tsub[tb[1]:tb[3], tb[0]:tb[2]])
            bm = bm.astype(np.uint8)
            boxes = digit_boxes(bm)
            token, tlow = digits(bm, boxes, tpl['plate'])
            blk = tb
        # 代币类型：**只看圆牌是否为金色**（金=进度代币；银/其他=非进度）
        # ⚠ sub_box 必须是 token_plate_box() 的**绝对**窗口 ——
        #   而 tb 是 find_plate() 返回的**相对于该窗口**的 bbox，不能当锚点。
        #   旧实现把 tb 当锚点用（坐标被二次偏移），错得很隐蔽：
        #   当时靠「数量级」兜底投票才蒙对，实测 28/28 是假象。
        tbox = token_plate_box(card)
        ttype, tev = read_token_type(a, col, blk, tbox, token)
        out.append({
            'card': card, 'power': power, 'power_low': plow,
            'token': token, 'token_low': tlow, 'plate': blk,
            'token_type': ttype, 'token_type_evidence': tev,
            'tier': read_tier(a, card),
        })
    return out


if __name__ == '__main__':
    tpl, cnt, borrowed = build()
    print('卡头域样本:', cnt)
    print('代币域跨域借用字形:', borrowed)
    okp = okt = oktier = 0
    for f in FILES:
        res = parse(DATA + '/%s.jpg' % f)
        for i, r in enumerate(res):
            gp = r['power'] == TRUTH_POWER[f][i]
            gt = r['token'] == TRUTH_TOKEN[f][i]
            gz = r['tier'] == TRUTH_TIER[f][i]
            okp += gp; okt += gt; oktier += gz
            print('  %s 卡%d 战力=%s%s 代币=%s%s 档位=%s%s'
                  % (f, i + 1, r['power'], '' if gp else '(应%d)' % TRUTH_POWER[f][i],
                     r['token'], '' if gt else '(应%d)' % TRUTH_TOKEN[f][i],
                     r['tier'], '' if gz else '(应%s)' % TRUTH_TIER[f][i]))
    print('\n战力 %d/16  代币 %d/16  档位 %d/16' % (okp, okt, oktier))
