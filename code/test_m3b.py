"""M3b 回归测试：弱点图标定位 + 分类（标注集 5 图 / 20 卡 / 39 枚）

运行：python code/test_m3b.py
期望：定位 20/20、检出 39/39、分类 39/39，且参数平台上全部 39/39

与 test_all.py / test_m5.py 的分工：
  test_all.py —— 识别层战力/代币/档位/币种（14 张 stage 截图）
  test_m5.py  —— 求解层 M5
  test_m3b.py —— 弱点图标（人工标注集，唯一有弱点真值的样本）
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from m8b_weak import (LABELED_IMAGES, LIM, THR, NORM_PX, SAT_MAX,
                      card_boxes, classify, icon_crop, load_templates,
                      load_truth, locate_icons, norm_symbol, symbol_mask)

FAIL = []


def ck(cond, msg):
    print('  %s %s' % ('OK  ' if cond else 'FAIL', msg))
    if not cond:
        FAIL.append(msg)


def main():
    images, _ = load_truth()

    print('== 1. 定位（含「只有 1 枚弱点」的卡） ==')
    cache = []          # [(真值, crop)]
    card_ok = card_n = 0
    for name in sorted(images):
        from m2_layout import load
        a = load(str(LABELED_IMAGES / name))
        cards = card_boxes(a)
        if len(cards) != 4:
            ck(False, '%s 卡片数应为 4，实为 %d' % (name, len(cards)))
        recs = locate_icons(a)
        for ci, lab in enumerate(images[name]):
            card_n += 1
            n = len(recs[ci]['icons'])
            card_ok += (n == len(lab))
            if n != len(lab):
                print('     ! %s 卡%d 真值 %d 枚 → 检出 %d 枚'
                      % (name, ci + 1, len(lab), n))
            for k in range(min(n, len(lab))):
                cx, cy, R, _ = recs[ci]['icons'][k]
                cache.append((lab[k], icon_crop(a, cx, cy, R)))
    ck(card_ok == card_n, '卡片级定位 %d/%d' % (card_ok, card_n))
    truth_n = sum(len(c) for v in images.values() for c in v)
    ck(len(cache) == truth_n, '检出枚数 %d / 真值 %d' % (len(cache), truth_n))

    print('== 2. 分类（默认参数 lim=%.2f thr=%.0f） ==')
    tpl = load_templates()
    ok = 0
    for lab, crop in cache:
        cand = classify(norm_symbol(symbol_mask(crop)), tpl, top=1)
        ok += bool(cand and cand[0][0] == lab)
    ck(ok == len(cache), '分类 %d/%d (%.1f%%)'
       % (ok, len(cache), 100.0 * ok / max(1, len(cache))))

    print('== 3. 参数平台（应全部 39/39，说明不是刀尖上的参数） ==')
    allok = True
    for lim in (0.55, 0.58, 0.60, 0.63):
        for thr in (140.0, 160.0, 175.0, 200.0):
            t = load_templates(lim, thr, SAT_MAX, NORM_PX)
            hit = 0
            for lab, crop in cache:
                cand = classify(norm_symbol(symbol_mask(crop, lim, thr)), t, 1)
                hit += bool(cand and cand[0][0] == lab)
            if hit != len(cache):
                allok = False
                print('     ! lim=%.2f thr=%.0f → %d/%d' % (lim, thr, hit,
                                                            len(cache)))
    ck(allok, 'lim 0.55~0.63 × thr 140~200 全部 %d/%d' % (len(cache), len(cache)))

    print('== 4. 越界参数应退化（记录失效边界，防止误以为处处可用） ==')
    t = load_templates(0.85, 140.0, SAT_MAX, NORM_PX)
    hit = 0
    for lab, crop in cache:
        cand = classify(norm_symbol(symbol_mask(crop, 0.85, 140.0)), t, 1)
        hit += bool(cand and cand[0][0] == lab)
    ck(hit < len(cache), 'lim=0.85 thr=140 退化到 %d/%d（徽章外环漏入掩膜）'
       % (hit, len(cache)))

    print('== 5. 整屏截图入口 read_screen（合成整屏：卡牌行 + 地图底） ==')
    import tempfile
    import numpy as np
    from PIL import Image
    from m2_layout import load as _load, load_screen
    import m8b_weak as M

    src = Image.open(str(LABELED_IMAGES / '2.png')).convert('RGB')
    canvas = Image.new('RGB', (2378, 1066), (12, 110, 124))
    canvas.paste(src, (0, 0))
    with tempfile.TemporaryDirectory() as td:
        p = str(Path(td) / 'screen.png')
        canvas.save(p)
        got = [[it['type'] for it in c['icons']] for c in M.read_screen(p)]
        exp = images['2.png']
        hit = sum(1 for g, t in zip(got, exp) for a, b in zip(g, t) if a == b)
        ck(len(got) == 4 and hit == 8,
           '整屏入口：卡数 %d/4、分类 %d/8' % (len(got), hit))

        print('== 6. 入口尺度归一化 load_screen（任意分辨率 → 1920 基准） ==')
        a = load_screen(p)                      # 2378 宽 → 1920
        ck(a.shape[1] == 1920, '2378 宽 → %d 宽' % a.shape[1])
        same = str(Path(td) / 's.png')
        Image.open(p).resize((1920, 862), Image.LANCZOS).save(same)
        ck(np.array_equal(load_screen(same), _load(same)),
           '已是基准宽度的图不重采样（像素完全一致）')

    print()
    if FAIL:
        print('失败 %d 项：' % len(FAIL))
        for x in FAIL:
            print('  - ' + x)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
