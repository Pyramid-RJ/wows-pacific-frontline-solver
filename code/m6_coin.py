"""代币类型判别：进度代币（金）vs 其余掉落物

**只有进度代币能被「效率提升」加成**，因此在识别层必须把它和其他掉落物分开。

判别只用一条判据：**圆牌是否为「金色」（琥珀色相）**。
⚠ **不能用数量级**（用户确认 2026-10-10）：经验值、硬币等掉落物同样在
1k~2k 区间，数量级无法区分；实测 `11.png` 的「硬币柱 1600/2000/2400」
会被旧的数量级判据误判成进度代币。

实测「琥珀色像素占亮像素的比例」（`goldness`，采样窗按黑牌高度定，见下）：

| 类别 | 范围 |
| --- | --- |
| **进度代币（金色圆牌，同心环 + 三角徽记）** | **0.384 ~ 0.561** |
| 次级代币（银色圆牌）/ 硬币 / 灰箱 / 卡面 | 0.028 ~ 0.101 |
| 绿色掉落图标 | 0.035 ~ 0.153（色相 ~120° → 被色相窗口排除） |

阈值 `GOLD_MIN = 0.20` 落在两者几何中点，两侧余量各 **1.95×**。

> 早先的方案及为何废弃：
> 1. 「暖度」(R+B/2−G) —— 银牌的米黄底色本身偏暖，反而比金牌得分更高。
> 2. 饱和度**中位数**（阈值 0.22）—— 采样窗半径按黑底数字牌宽度算，
>    而牌宽随**位数**变化：4 位数的牌更宽 → 采样窗更大、混入更多非牌面像素
>    → 中位数被稀释。实测 `41.png` 的「1800」那枚中位饱和度只有 **0.161**，
>    比非金样本还低 —— 两类完全不分离（0.76×）。
> 3. 饱和度 **p70 / p75** —— 对采样面积不敏感，但**绿色图标同样是高饱和**
>    （色相 ~120°，饱和度 ~0.67），p75 会把它们误判成金色
>    （实测 3 处误报：`21` 卡4、`32` 卡2、`s3_kp6` 卡2）。
>    换句话说：**「饱和」不等于「金」，「色相」才是。**
"""
import numpy as np
from pathlib import Path
from m2_layout import load, find_card_columns, to_gray
from m3_token import find_plate

ROOT = Path(__file__).resolve().parent.parent
DATA = str(ROOT / 'data' / 'stage')

# 进度代币数量下限（次级代币实测范围 1~15）
PROGRESS_MIN = 100


def coin_region(a, col, blk, sub_box):
    """代币圆牌区域（绝对坐标）

    实测版式：黑方块在右下，圆牌在左上，两者部分重叠。
    圆牌中心 ≈ 黑方块左上角 + (−0.65·bh, −0.40·bh)，牌径 ≈ bh。

    ⚠ 窗口必须**只按黑牌高度 `bh` 定，不能用宽度 `bw`** ——
    `bw` 随数字位数变化（4 位数的牌更宽），用它当半径会让位数多的卡
    采到更大一片背景，金色度被稀释（历史误判的根源，见模块 docstring）。
    高度 `bh` 只反映字号，与位数无关。
    """
    ax0 = sub_box[0] + blk[0]
    ay0 = sub_box[1] + blk[1]
    bh = blk[3] - blk[1]
    cx = ax0 - int(bh * 0.65)
    cy = ay0 - int(bh * 0.40)
    R = int(bh)
    return (cx - R, cy - R, cx + R, cy + R)


def goldness(a, box):
    """圆牌「金色度」= 亮像素中**琥珀色**（金色）像素的占比

    金色圆牌：色相 ≈ 35~60°、饱和度高、R 为最大分量。
    绿色掉落图标（色相 ≈ 100~140°）、银色圆牌 / 卡面（低饱和）均不满足。

    实测（12 枚金色 / 60 枚非金，跨 18 张截图）：金色 0.384~0.561，
    非金 ≤ 0.101，阈值 0.20 两侧余量各约 1.95×。
    """
    x0, y0, x1, y1 = box
    H, W = a.shape[:2]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(W, x1), min(H, y1)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    reg = a[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    bright = reg.mean(axis=1) > 90      # 排除深色背景与黑方块
    if bright.sum() < 20:
        return None
    px = reg[bright]
    mx = px.max(axis=1)
    mn = px.min(axis=1)
    sat = (mx - mn) / np.maximum(mx, 1.0)
    r, g, b = px[:, 0], px[:, 1], px[:, 2]
    # 色相（度）；只有 R 为最大分量的像素才可能是金色
    hue = 60.0 * (((g - b) / np.maximum(mx - mn, 1e-6)) % 6.0)
    amber = ((sat >= 0.30) & (r >= mx - 1e-6) & (g >= b)
             & (hue >= 20.0) & (hue <= 70.0))
    return float(amber.mean())


GOLD_MIN = 0.20


def read_token_type(a, col, blk=None, sub_box=None, value=None):
    """返回 (type, evidence)
    type: 'progress'（金色进度代币）| 'secondary'（其余掉落物）| 'unknown'

    **只看金色度，不看数量级** —— 数量级仅作为 evidence 记录，
    因为它无法区分进度代币与经验值/硬币（都会落在 1k~2k）。
    """
    votes = {}
    if sub_box is not None:
        if blk is None:
            b, sub = find_plate(a, col)
        else:
            b = blk
        if b is not None:
            reg = coin_region(a, col, b, sub_box)
            g = goldness(a, reg)
            if g is not None:
                votes['sat'] = round(g, 3)
                votes['gold'] = g >= GOLD_MIN
    if value is not None:
        votes['magnitude'] = value >= PROGRESS_MIN     # 仅记录，不参与判定
    if 'gold' not in votes:
        return 'unknown', votes
    return ('progress' if votes['gold'] else 'secondary'), votes


if __name__ == '__main__':
    FILES = ['ex_kp1', 'ex_kp2', 'ex_kp3', 'kp1', 'kp2', 'kp3', 'boss']
    from m2_layout import card_vertical, token_plate_box
    total = ok = 0
    for f in FILES:
        a = load(DATA + '/%s.jpg' % f)
        row = []
        for col in find_card_columns(a):
            yt, yb = card_vertical(a, col[0], col[1])
            tb = token_plate_box({'bbox': (col[0], yt, col[1], yb)})
            b, sub = find_plate(a, col)
            if b is None:
                row.append(('?', None))
                continue
            t, v = read_token_type(a, col, b, tb)
            row.append((t[:4], v.get('gold')))
        print(f, row)