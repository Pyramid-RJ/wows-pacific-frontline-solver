"""求解核心 · 独立运行示例（不依赖识别层）

只用「据点描述」JSON 求解 —— 不导入任何识别模块（OCR / opencv / 截图都不需要），
只需要 numpy，用于对接、复现与排查。

运行：
    .venv/Scripts/python examples/solve_demo.py

期望最后一行：
    整局汇总：5 阶段 · 13 据点 · 未达标 0 个

据点 JSON 由 `code/m11_plan.parse_node` 对截图目录产出（示例见
`examples/nodes_新行动_5阶段13据点.json`），本次配套的期望输出见
`examples/期望输出.txt`。
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / 'code'))

import m11_plan as P                                            # noqa: E402

NODES_JSON = HERE / 'nodes_新行动_5阶段13据点.json'


def load_nodes(path):
    """读入识别层导出的据点 JSON，恢复 set 型字段与 int 键"""
    doc = json.loads(Path(path).read_text(encoding='utf-8'))
    nodes = []
    for n in doc['nodes']:
        n['weak'] = [set(w) for w in (n.get('weak') or [])]
        n['inventory'] = {int(k): int(v)
                          for k, v in (n.get('inventory') or {}).items()}
        nodes.append(n)
    return nodes


def main():
    nodes = load_nodes(NODES_JSON)
    fleet = P.load_fleet()                       # data/our_fleet.json
    inp = {'current': 1, 'total': 5, 'uses_max': None,   # None = 次数不限
           'missing': [], 'damaged': {}, 'banned': {},
           'disabled': {}, 'variations': None}

    print('读入 %d 个据点；卡池 %d 张' % (len(nodes), len(fleet)))
    results, _ = P.plan_all_stages(fleet, inp, {}, nodes,
                                   target=1.0, uses_max=None,
                                   no_local=False, save_gold=True)
    for st in sorted(results):
        r = results[st]
        nodes_st = [n for n in nodes if n.get('stage') == st]
        inp_st = dict(inp, current=st)
        print('\n' + '=' * 60)
        print('【阶段 %d】' % st)
        print(P.format_plan(r['plan'], r['pool'], r['remaining'], nodes_st,
                            inp_st, None, r['uses_left'], r['notes'],
                            r['skipped'], r['var_status'], target=1.0))
    n_bad = sum(1 for s in results for p in results[s]['plan']
                if not p['meets_target'])
    print('整局汇总：%d 阶段 · %d 据点 · 未达标 %d 个'
          % (len(results), sum(len(results[s]['plan']) for s in results),
             n_bad))


if __name__ == '__main__':
    main()
