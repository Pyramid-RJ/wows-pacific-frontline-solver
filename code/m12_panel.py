"""M6-3 图形化编排建议面板：plan/v1 JSON → 自包含 HTML

运行：
  python code/m12_panel.py                  # data/plan.json → data/panel.html，浏览器打开即用
  python code/m12_panel.py --serve          # 本地服务，浏览器刷新即最新数据
  python code/m12_panel.py --plan X.json --out Y.html

设计约定：
  - 零外部依赖：数据内嵌进 HTML、样式内联，离线双击打开也可用
  - 数据源是 plan/v1（m11_plan.plan_to_json），字段口径与文本输出逐一对应，
    本模块不做任何数值计算，只做展示
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = ROOT / 'data' / 'plan.json'
DEFAULT_OUT = ROOT / 'data' / 'panel.html'

WEAK_ZH = {'rapid': '速射', 'smoke': '烟幕', 'largecal': '大口径',
           'atkair': '攻击机', 'detect': '被侦察', 'antiair': '防空'}

_CSS = """
:root { --bg:#12161d; --card:#1b212b; --line:#2a3240; --fg:#dde4ee;
        --dim:#8b96a8; --ok:#3fb96a; --bad:#e05656; --warn:#e0a03c;
        --acc:#4a9eff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--fg);
       font:14px/1.5 "Microsoft YaHei", "PingFang SC", sans-serif;
       padding:20px 24px 60px; }
h1 { font-size:19px; margin-bottom:4px; }
.sub { color:var(--dim); font-size:12.5px; margin-bottom:18px; }
.sub b { color:var(--fg); }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(430px,1fr));
        gap:14px; }
.node { background:var(--card); border:1px solid var(--line);
        border-radius:10px; padding:14px 16px; }
.node.fail { border-color:var(--bad); }
.nhead { display:flex; align-items:baseline; gap:10px; margin-bottom:8px; }
.nhead .nm { font-size:16px; font-weight:700; }
.tag { font-size:11px; padding:1px 8px; border-radius:8px;
       background:var(--line); color:var(--dim); white-space:nowrap; }
.tag.ok { background:#173626; color:var(--ok); }
.tag.bad { background:#3a1717; color:var(--bad); }
.tag.warn { background:#3a2c14; color:var(--warn); }
.meta { color:var(--dim); font-size:12px; margin-bottom:8px; }
.meta b { color:var(--fg); font-weight:600; }
.cond { font-size:12.5px; margin:6px 0; padding:6px 10px; border-radius:6px;
        background:var(--line); }
.cond.warn { background:#33270f; color:var(--warn); }
.cond .inc { color:var(--ok); }
table { width:100%; border-collapse:collapse; margin-top:8px; }
th { color:var(--dim); font-size:11px; font-weight:400; text-align:left;
     padding:2px 6px; border-bottom:1px solid var(--line); }
td { padding:5px 6px; border-bottom:1px solid var(--line);
     font-size:12.5px; vertical-align:top; }
tr:last-child td { border-bottom:none; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.wr { display:inline-block; min-width:44px; text-align:center;
      border-radius:6px; font-weight:700; }
.wr.ok { color:var(--ok); } .wr.bad { color:var(--bad); }
.chip { display:inline-block; font-size:11px; padding:0 7px;
        margin:1px 3px 1px 0; border-radius:7px;
        background:var(--line); color:var(--fg); }
.chip.deb { background:#33222a; color:#e08ab0; }
.chip.buf { background:#16303f; color:#6fc3ff; }
.chip.var { background:#173626; color:var(--ok); }
.chip.spc { background:#14321f; color:#7fe0a0; border:1px solid #1f5433; }
/* 卡面等级配色：与游戏内卡面底色一致（白 / 蓝 / 金 / 红），提高卡牌与等级的辨识度 */
.tier-white { color:#e9eef7; }
.tier-blue  { color:#63a9f0; }
.tier-gold  { color:#f0c04a; }
.tier-red   { color:#e0685f; }
.lv { font-size:10.5px; padding:0 5px; border-radius:7px;
      border:1px solid currentColor; opacity:.95; }
.vars { margin-top:3px; line-height:1.35; }
.lbl { color:var(--dim); font-size:10.5px; margin-right:3px; }
.sum { margin-top:18px; background:var(--card); border:1px solid var(--line);
       border-radius:10px; padding:14px 16px; }
.sum h2 { font-size:15px; margin-bottom:8px; }
.sum ul { list-style:none; }
.sum li { font-size:12.5px; padding:2px 0; color:var(--fg); }
.sum li.warn { color:var(--warn); }
.kv { display:flex; flex-wrap:wrap; gap:6px 26px; margin-bottom:8px;
      font-size:12.5px; }
.kv span b { color:var(--acc); }
.foot { color:var(--dim); font-size:11.5px; margin-top:14px; }
"""

_JS = """
function zh(w){ return (%(weak_zh)s)[w] || w; }
const DIR_ZH = { up: '上', left: '左', right: '右' };
function el(t, cls, html){ const e=document.createElement(t);
  if(cls) e.className=cls; if(html!==undefined) e.innerHTML=html; return e; }
function pct(x){ return Math.round(x*100) + '%%'; }
function chips(map, cls, conv){
  return Object.entries(map||{}).map(([k,v]) =>
    '<span class="chip '+cls+'">'+conv(k)+
    (v.length ? '·' + v.map(d=>DIR_ZH[d]||d).join('') : '') + '</span>').join('');
}
// 卡面等级配色（white/blue/gold/red → 与游戏内卡面底色一致的字体色）
function tcls(t){ return t ? 'tier-' + t : ''; }
// 变项挂在**所装之船**上：直接显示在该船名下方，标出标准/特殊（颜色区分）
function vchips(a){
  const e = a.variations || {};
  const out = [].concat(
    e.std ? ['<span class="chip var">' + e.std + '</span>'] : [],
    e.spc ? ['<span class="chip spc">' + e.spc + '</span>'] : []);
  return out.length
    ? '<div class="vars"><span class="lbl">变项</span>' + out.join('') +
      '</div>'
    : '';
}
const STAGES = DATA.schema === 'plan_all/v1' ? DATA.stages : [DATA];
const TOTAL_NODES = STAGES.reduce((x, s) => x + s.nodes.length, 0);
document.getElementById('title').textContent =
  DATA.schema === 'plan_all/v1'
    ? '整局编排 · ' + STAGES.length + ' 个阶段 · ' + TOTAL_NODES + ' 个据点'
    : '阶段 ' + DATA.stage.current + '（共 ' + DATA.stage.total + '）· ' +
      DATA.nodes.length + ' 个据点';
document.getElementById('sub').innerHTML =
  '目标胜率 <b>' + pct(DATA.target_rate || STAGES[0].stage.target_rate) +
  '</b>' +
  ((DATA.target_rate || STAGES[0].stage.target_rate) < 1
    ? '（已调低：非 100%% 仍有翻车概率）' : '') +
  ' · 生成于 ' + DATA.schema;
const root = document.getElementById('stages');
for (const D of STAGES){
  const sec = el('div', '');
  if (STAGES.length > 1){
    const bad = D.nodes.filter(n => !n.meets_target).length;
    sec.appendChild(el('h2', 'stageh',
      '阶段 ' + D.stage.current + '（共 ' + D.stage.total + '）' +
      ' · ' + D.nodes.length + ' 个据点' +
      (bad ? ' · <span class="wr bad">' + bad + ' 个未达标</span>' : '')));
  }
  const grid = el('div', 'grid');
  sec.appendChild(grid);
for (const n of D.nodes){
  const okAll = n.min_win_rate >= D.stage.target_rate - 1e-9;
  const card = el('div', 'node' + (okAll ? '' : ' fail'));
  // 标题行
  const head = el('div', 'nhead');
  head.appendChild(el('span', 'nm', n.name));
  head.appendChild(el('span', 'tag',
    (n.difficulty_label||'—') + ' · 敌方难度 ' + n.difficulty));
  head.appendChild(el('span', 'tag ' + (okAll?'ok':'bad'),
    (okAll ? '✓ ' : '✗ ') + pct(n.min_win_rate)));
  card.appendChild(head);
  // 敌方 + 掉落
  let meta = '敌方战力 <b>' + n.enemy_powers.join(' / ') + '</b>';
  // 新增可选字段（2026-10-13）：面板绿字值（= 含「带入」敌方战力加成）
  // 与战斗基础值的对照，缺这两个键时完全不影响渲染
  if (n.enemy_powers_displayed && n.enemy_power_pct) {
    const dp = Math.round((n.enemy_power_pct.displayed || 0) * 100);
    meta += ' <span class="tag">面板绿字 '
      + n.enemy_powers_displayed.join(' / ') + '（带入条件 '
      + (dp > 0 ? '+' : '') + dp + '%% 已反推为上面的基础值）</span>';
  }
  if (n.is_progress)
    meta += ' · 进度代币 <b>' + n.progress_tokens + '</b>';
  card.appendChild(el('div', 'meta', meta));
  // 弱点
  card.appendChild(el('div', 'meta', '弱点：' + n.weak.map((w,i)=>
    '敌' + (i+1) + ' ' + (w.length ? w.map(zh).join('·')
                                   : '（未识别）')).join('　')));
  // 条件
  const c = n.condition;
  const parts = [];
  if (c.scope) parts.push(c.scope);
  if (c.attr) parts.push(c.attr + ' ' + c.pct + '%%');
  if (c.directive) parts.push('「' + c.directive + '」');
  if (c.ban_item) parts.push('禁用变项' +
    ((c.disabled_variations && c.disabled_variations.length)
      ? '（禁用：' + c.disabled_variations.join('、') + '）'
      : '（未填具体变项，按不禁用处理）'));
  if (parts.length){
    let line = '战斗条件：' + parts.join(' · ');
    if (c.quantified) line += '（<span class="inc">已计入胜率</span>）';
    else if (c.attr && c.pct === null)
      line += '（未读到百分比，未计入 —— 需人工确认）';
    card.appendChild(el('div', 'cond' + (c.quantified?' warn':''), line));
  }
  // 变项不再在据点级平铺 —— 已挂到「装它的那条船」上（编队表「我方」列内），
  // 使用对象一眼可见；标准/特殊由 chip 颜色区分（绿=标准，浅绿带框=特殊）
  // 编队表
  const tb = el('table');
  tb.innerHTML = '<tr><th>位</th><th>我方</th><th>有效参数</th>' +
    '<th>区间 vs 敌方</th><th>胜率</th><th>减益 / 获得增益</th></tr>';
  for (const a of n.assignment){
    const ok = a.win_rate >= D.stage.target_rate - 1e-9;
    const st = a.stats ? a.stats.join('/') + '=' + a.stats.reduce((x,y)=>x+y,0)
                       : '—';
    const tr = el('tr');
    tr.innerHTML =
      '<td class="num">' + a.position + '</td>' +
      '<td class="' + tcls(a.tier) + '"><b>' + a.name + '</b>' +
        (a.level ? ' <span class="lv">' + a.level + '</span>' : '') +
        ' <span class="tag">hp' + a.hp + '</span>' + vchips(a) + '</td>' +
      '<td class="num">' + st + '</td>' +
      '<td class="num">' + a.power_range[0] + '–' + a.power_range[1] +
        ' vs ' + a.enemy_eff + '</td>' +
      '<td><span class="wr ' + (ok?'ok':'bad') + '">' + pct(a.win_rate) +
        '</span></td>' +
      '<td>' + chips(a.debuffs, 'deb', k=>k) +
        a.buffs_received.map(b=>'<span class="chip buf">' + b.attr +
          ' +' + Math.round(b.pct*100) + '%%</span>').join('') + '</td>';
    tb.appendChild(tr);
  }
  card.appendChild(tb);
  // 生效增益
  if (n.active_buffs.length)
    card.appendChild(el('div', 'meta', '生效增益：' + n.active_buffs.map(b=>
      b.from + ' → 位' + b.to_position + ' · ' + b.attr).join('；')));
  grid.appendChild(card);
}
  // 汇总
  const s = D.summary;
  const sum = el('div', 'sum');
  sum.appendChild(el('h2', '', '阶段 ' + D.stage.current + ' 汇总'));
  const kv = el('div', 'kv');
  kv.innerHTML =
    '<span>参战 <b>' + s.cards_used_count + '</b> 张</span>' +
    '<span>次级代币 已用 <b>' + s.secondary_uses.used + '</b> / (' +
      (s.secondary_uses.max === null ? '不限' : s.secondary_uses.max) +
      ') 次</span>' +
    '<span>进度代币 <b>' + s.progress_tokens_total + '</b>' +
      (s.efficiency_nodes ? '（' + s.efficiency_nodes +
        ' 个据点已装效率提升）' : '') + '</span>' +
    '<span>保留高战力 <b>' +
      s.kept_top.map(k=>'<span class="' + tcls(k.tier) + '">' + k.name +
        (k.level ? '（' + k.level + '）' : '') + '</span>').join('、') +
      '</b></span>';
  sum.appendChild(kv);
  const ul = el('ul');
  if (s.notes.length){
    for (const x of s.notes)
      ul.appendChild(el('li', x.indexOf('⚠')>=0 ? 'warn' : '', '· ' + x));
  } else {
    ul.appendChild(el('li', '', '· 本阶段全部据点达标，无告警'));
  }
  sum.appendChild(ul);
  sec.appendChild(sum);
  root.appendChild(sec);
}
"""

_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>太平洋战线 · 编排建议</title>
<style>
%(css)s
h2.stageh {{ font-size:16px; margin:22px 0 10px; }}
</style>
</head>
<body>
<h1 id="title">编排建议</h1>
<div class="sub" id="sub"></div>
<div id="stages"></div>
<div class="foot" style="margin-top:18px">
  卡名颜色 = 卡面等级：<span class="tier-white">白 · 1 级</span> ·
  <span class="tier-blue">蓝 · 2 级</span> ·
  <span class="tier-gold">金 · 3 级</span> ·
  <span class="tier-red">红 · 3 级</span>　|　
  变项显示在<b>装它的那条船</b>名下（绿=标准变项，浅绿带框=特殊变项）；
  「效率提升」不占次数上限、库存视为无限<br>
  数据源：plan/v1 · plan_all/v1（m11_plan）· 面板由 m12_panel 生成，只做展示不做计算
</div>
<script>const DATA = %(data)s;</script>
<script>%(js)s</script>
</body>
</html>
"""


def build_html(doc):
    """plan/v1 或 plan_all/v1 dict → 自包含 HTML 字符串（数据内嵌，离线可用）"""
    if doc.get('schema') not in ('plan/v1', 'plan_all/v1'):
        raise ValueError('期望 plan/v1 / plan_all/v1，得到 %r'
                         % doc.get('schema'))
    data = json.dumps(doc, ensure_ascii=False).replace('</', '<\\/')
    return _HTML % {'css': _CSS, 'data': data,
                    'js': _JS % {'weak_zh': repr(WEAK_ZH)}}


class _Handler(BaseHTTPRequestHandler):
    plan_path = DEFAULT_PLAN
    out_path = DEFAULT_OUT

    def do_GET(self):
        if self.path not in ('/', '/index.html'):
            self.send_error(404)
            return
        doc = json.loads(Path(self.plan_path).read_text(encoding='utf-8'))
        html = build_html(doc)
        body = html.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print('  [面板] ' + fmt % args)


def main(argv=None):
    ap = argparse.ArgumentParser(description='M6-3 编排建议面板')
    ap.add_argument('--plan', default=str(DEFAULT_PLAN),
                    help='plan/v1 JSON（m11_plan 的 --out 产物）')
    ap.add_argument('--out', default=str(DEFAULT_OUT),
                    help='生成的 HTML 路径')
    ap.add_argument('--serve', action='store_true',
                    help='启动本地服务（浏览器刷新即最新数据），不写文件')
    ap.add_argument('--port', type=int, default=8765)
    args = ap.parse_args(argv)

    if args.serve:
        _Handler.plan_path = Path(args.plan)
        print('【面板服务】http://127.0.0.1:%d  （数据源 %s，Ctrl+C 退出）'
              % (args.port, args.plan))
        try:
            HTTPServer(('127.0.0.1', args.port), _Handler).serve_forever()
        except KeyboardInterrupt:
            print('\n已退出')
        return 0

    doc = json.loads(Path(args.plan).read_text(encoding='utf-8'))
    html = build_html(doc)
    Path(args.out).write_text(html, encoding='utf-8')
    print('【面板】%s —— 浏览器打开即用；实时模式：--serve' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
