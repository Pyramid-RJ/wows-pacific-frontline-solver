"""M6-4 玩家输入编辑页：our_fleet.json → 自包含 HTML（交互式生成 player_input.json）

用法：
  python code/m13_input.py           # data/input_editor.html（双击打开，用「下载」保存 JSON）
  python code/m13_input.py --serve   # http://127.0.0.1:8766 —— 页面「保存」直接写入
                                     # data/player_input.json（旧文件自动备份为 .bak）

设计约定：
  - 零外部依赖：卡表与样式内嵌进 HTML，离线双击打开也能用
  - 卡表来自 `data/our_fleet.json`（唯一权威来源），等级配色与面板一致
  - 页面只负责**生成输入契约**（见开发文档 §八）；
    字段口径与 `m11_plan.load_player_input` 严格对应，写出前用
    `validate_input_doc()` 校验一遍，避免手写出错
"""
import argparse
import json
import shutil
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
DEFAULT_OUT = DATA / 'input_editor.html'
DEFAULT_INPUT = DATA / 'player_input.json'

TIER_COLOR = {'white': '#e9eef7', 'blue': '#63a9f0',
              'gold': '#f0c04a', 'red': '#e0685f'}
LEVEL_ZH = {'white': '1级', 'blue': '2级', 'gold': '3级', 'red': '3级'}
SHIP_TYPES = ['飞机', '战列舰', '巡洋舰', '驱逐舰']
HP_MAX = 3


def load_roster(path=None):
    """卡表（唯一权威来源 data/our_fleet.json）→ 供页面渲染的轻量列表"""
    p = Path(path) if path else DATA / 'our_fleet.json'
    cards = json.loads(Path(p).read_text(encoding='utf-8'))['cards']
    return [{'id': c['id'], 'name': c['name'],
             'power': list(c['power']),
             'tier': c.get('tier'), 'type': c.get('type')}
            for c in sorted(cards, key=lambda c: c['id'])]


def load_var_slots(path=None):
    """变项工具栏槽位 → 变项名（供页面「变项库存」表头）"""
    p = Path(path) if path else DATA / 'var_config.json'
    if not Path(p).exists():
        return {}
    names = (json.loads(Path(p).read_text(encoding='utf-8'))
             .get('slot_names') or {})
    return {int(k): v for k, v in names.items() if v}


# ============ 输入契约校验（与 m11_plan.load_player_input 对齐） ============
def validate_input_doc(doc):
    """校验页面写出的 player_input.json，返回规范化后的 dict

    只认这几个键（其余键直接忽略，避免手写文件里塞进无意义的字段）；
    `_meta` 若存在则原样保留（写文件的人自己看的说明，求解器不读）：
      stage{current,total} · secondary_uses_max · missing_cards ·
      damaged_cards · banned_types · disabled_variations · variations
    校验失败抛 ValueError（页面会把它显示成保存失败原因）。
    """
    if not isinstance(doc, dict):
        raise ValueError('顶层必须是 JSON 对象')
    var_names = set(load_var_slots().values())
    stage = doc.get('stage') or {}
    cur, total = stage.get('current'), stage.get('total')
    if not isinstance(cur, int) or cur < 1:
        raise ValueError('stage.current 必须是 ≥1 的整数')
    if not isinstance(total, int) or total < 1:
        raise ValueError('stage.total 必须是 ≥1 的整数')
    if cur > total:
        raise ValueError('stage.current 不能大于 stage.total')

    uses = doc.get('secondary_uses_max')
    if uses is not None and (not isinstance(uses, int) or uses < 0):
        raise ValueError('secondary_uses_max 必须是 ≥0 的整数，或 null（不设上限）')

    missing = doc.get('missing_cards') or []
    if not isinstance(missing, list) or \
            any(not isinstance(x, int) for x in missing):
        raise ValueError('missing_cards 必须是整数（卡 id）列表')
    if len(set(missing)) != len(missing):
        raise ValueError('missing_cards 里有重复的卡 id')

    damaged = doc.get('damaged_cards') or {}
    if not isinstance(damaged, dict):
        raise ValueError('damaged_cards 必须是 {卡 id: 剩余格数} 的对象')
    for k, v in damaged.items():
        try:
            kid = int(k)
        except (TypeError, ValueError):
            raise ValueError('damaged_cards 的键必须是卡 id：%r' % (k,))
        if not isinstance(v, int) or not 0 <= v < HP_MAX:
            raise ValueError('卡 %s 的剩余格数必须是 0~%d 的整数（满格不必写）'
                             % (kid, HP_MAX - 1))
        if kid in missing:
            raise ValueError('卡 %s 同时被标成「未拥有」和「已损耗」' % kid)

    banned = doc.get('banned_types') or {}
    if not isinstance(banned, dict):
        raise ValueError('banned_types 必须是 {截图名: [舰种]} 的对象')
    for k, v in banned.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError('banned_types 的键必须是非空截图名')
        if not isinstance(v, list) or \
                any(t not in SHIP_TYPES for t in v):
            raise ValueError('截图 %s 的禁用舰种必须是 %s 的子集'
                             % (k, '/'.join(SHIP_TYPES)))

    var = doc.get('variations')
    if var is not None:
        if isinstance(var, list):
            # 元素可为 null（未指定）或 ≥0 的整数（0 = 本轮不可用）
            if len(var) != 11 or any(
                    v is not None and (not isinstance(v, int) or v < 0)
                    for v in var):
                raise ValueError('variations 列表必须是 11 个元素'
                                 '（每项为 null 或 ≥0 的整数，'
                                 '按 var_config.json 的槽位顺序）')
        elif isinstance(var, dict):
            for k, v in var.items():
                key_ok = (isinstance(k, int) or
                          (isinstance(k, str) and k.strip().isdigit()) or
                          k in var_names)
                if not key_ok:
                    raise ValueError('variations 的键只能是槽位号或变项名：%r'
                                     % (k,))
                if v is not None and (not isinstance(v, int) or v < 0):
                    raise ValueError('variations 的数量必须是 null 或 ≥0 的整数')
        else:
            raise ValueError('variations 必须是 null、11 个元素的列表，'
                             '或 {槽位号/变项名: null|数量}')

    dis = doc.get('disabled_variations') or {}
    if not isinstance(dis, dict):
        raise ValueError('disabled_variations 必须是 {截图名: [变项名]} 的对象')
    for k, v in dis.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError('disabled_variations 的键必须是非空截图名')
        if not isinstance(v, list) or any(t not in var_names for t in v):
            raise ValueError('截图 %s 的禁用变项必须是 %s 之一'
                             % (k, '、'.join(sorted(var_names))))

    out = {}
    if isinstance(doc.get('_meta'), dict):      # 说明性字段：原样保留（求解器不读）
        out['_meta'] = doc['_meta']
    out.update({
        'stage': {'current': cur, 'total': total},
        'secondary_uses_max': uses,
        'missing_cards': sorted(int(x) for x in missing),
        'damaged_cards': {str(int(k)): int(v) for k, v in damaged.items()},
        'banned_types': {k: list(v) for k, v in banned.items()},
        'disabled_variations': {k: list(v) for k, v in dis.items()},
        'variations': var,
    })
    return out


def save_input(doc, path=None):
    """校验 → 备份旧文件 → 写入；返回 (路径, 备份名|None, 规范化 dict)"""
    clean = validate_input_doc(doc)
    p = Path(path) if path else DEFAULT_INPUT
    bak = None
    if p.exists():
        bak = p.with_suffix(p.suffix + '.bak')
        shutil.copy2(p, bak)
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + '\n',
                 encoding='utf-8')
    return p, bak, clean


# ============ 页面 ============
_CSS = """
:root { --bg:#12161d; --card:#1b212b; --line:#2a3240; --fg:#dde4ee;
        --dim:#8b96a8; --ok:#3fb96a; --bad:#e05656; --warn:#e0a03c;
        --acc:#4a9eff; }
* { box-sizing:border-box; margin:0; padding:0; }
body { background:var(--bg); color:var(--fg);
       font:14px/1.55 "Microsoft YaHei", "PingFang SC", sans-serif;
       padding:20px 24px 60px; }
h1 { font-size:19px; margin-bottom:4px; }
.sub { color:var(--dim); font-size:12.5px; margin-bottom:16px; }
.sub b { color:var(--fg); }
.wrap { display:grid; grid-template-columns:minmax(560px, 1.35fr) minmax(360px, 1fr);
        gap:16px; align-items:start; }
@media (max-width:1100px){ .wrap { grid-template-columns:1fr; } }
.box { background:var(--card); border:1px solid var(--line);
       border-radius:10px; padding:14px 16px; margin-bottom:14px; }
.box h2 { font-size:14.5px; margin-bottom:10px; }
.box h2 .hint { color:var(--dim); font-size:11.5px; font-weight:400;
                margin-left:6px; }
.row { display:flex; flex-wrap:wrap; gap:10px 18px; align-items:center;
       font-size:13px; }
label.f { color:var(--dim); }
input[type=number], input[type=text], select {
       background:#0e131a; color:var(--fg); border:1px solid var(--line);
       border-radius:6px; padding:4px 7px; font:13px/1.4 inherit; }
input[type=number] { width:74px; }
input[type=text] { width:190px; }
button { background:#22303f; color:var(--fg); border:1px solid var(--line);
         border-radius:7px; padding:4px 11px; font:12.5px/1.4 inherit;
         cursor:pointer; }
button:hover { background:#2b3b4d; }
button.pri { background:#1c4a72; border-color:#2f6ea8; }
button.pri:hover { background:#225d8d; }
button.on { background:#25476a; border-color:#3b7bb5; }
button:disabled { opacity:.45; cursor:default; }
.tools { display:flex; flex-wrap:wrap; gap:8px; align-items:center;
         margin-bottom:10px; }
.tools .sp { flex:1; }
table { width:100%; border-collapse:collapse; }
th { color:var(--dim); font-size:11px; font-weight:400; text-align:left;
     padding:2px 6px; border-bottom:1px solid var(--line); }
td { padding:4px 6px; border-bottom:1px solid var(--line); font-size:12.5px; }
tr.gone td { opacity:.42; }
tr.gone .nm { text-decoration:line-through; }
tr:last-child td { border-bottom:none; }
.nm { font-weight:600; }
.tier-white { color:#e9eef7; } .tier-blue { color:#63a9f0; }
.tier-gold { color:#f0c04a; } .tier-red { color:#e0685f; }
.lv { font-size:10.5px; padding:0 5px; border-radius:7px;
      border:1px solid currentColor; opacity:.9; }
.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.scroll { max-height:430px; overflow:auto; }
pre { background:#0e131a; border:1px solid var(--line); border-radius:8px;
      padding:10px; font-size:12px; line-height:1.5; overflow:auto;
      max-height:520px; white-space:pre; }
.msg { font-size:12.5px; margin-top:8px; }
.msg.ok { color:var(--ok); } .msg.bad { color:var(--bad); }
.msg.warn { color:var(--warn); }
.foot { color:var(--dim); font-size:11.5px; margin-top:14px; }
input[type=checkbox] { width:15px; height:15px; accent-color:#4a9eff; }
.cl { display:inline-flex; align-items:center; gap:4px; font-size:12.5px; }
"""


def build_html(roster, var_slots, serve=False):
    """卡表 + 槽位表 → 自包含 HTML（数据内嵌，离线可用）"""
    order = sorted(var_slots)
    data = json.dumps({'roster': roster,
                       'slots': {str(k): v for k, v in var_slots.items()},
                       'varNames': [var_slots[i] for i in order],
                       'stdCount': sum(1 for i in order if i < 6),
                       'types': SHIP_TYPES,
                       'hpMax': HP_MAX,
                       'serve': bool(serve)},
                      ensure_ascii=False).replace('</', '<\\/')
    return _HTML % {'css': _CSS, 'data': data}


_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>太平洋战线 · 编写 player_input.json</title>
<style>
%(css)s
</style>
</head>
<body>
<h1>编写 player_input.json</h1>
<div class="sub">
  <b>卡牌默认全部拥有</b> —— 只需把<u>自己没有的卡</u>取消勾选；已损耗的卡改「剩余格数」。
  本页只生成输入文件，不做任何计算。<br>
  <span style="color:var(--warn)">截图提示</span>：截图请只框住战斗区
  （敌方卡牌 + 部署区 + <b>底部整条变项工具栏</b> + 右侧面板），
  <b>不要把我方卡组栏框进来</b> —— 它与工具栏紧邻、同为亮色，会干扰工具栏定位。
</div>
<div class="wrap">
  <div>
    <div class="box">
      <h2>基础<span class="hint">阶段用于跨阶段战力留存规划</span></h2>
      <div class="row">
        <label class="f">当前阶段</label>
        <input type="number" id="cur" min="1" step="1" value="1">
        <label class="f">总阶段数</label>
        <input type="number" id="total" min="1" step="1" value="1">
        <label class="f">次数上限</label>
        <input type="number" id="uses" min="0" step="1" placeholder="留空 = 不限">
        <span class="hint" style="color:var(--dim);font-size:11.5px">
          「次级代币购买的变项」本轮最多用几次；留空即不设上限（效率提升不计入）</span>
      </div>
    </div>

    <div class="box">
      <h2>卡牌<span class="hint" id="ownHint"></span></h2>
      <div class="tools">
        <input type="text" id="q" placeholder="搜索卡名…">
        <span id="typeBtns"></span>
        <span class="sp"></span>
        <button id="allOn">全部拥有</button>
        <button id="allOff">全部未拥有</button>
        <span class="cl"><input type="checkbox" id="onlyOff">
          <label for="onlyOff">只看未拥有</label></span>
      </div>
      <div class="scroll">
        <table>
          <thead><tr><th>拥有</th><th>卡名</th><th>战力</th><th>等级</th>
            <th>舰种</th><th>剩余格数</th></tr></thead>
          <tbody id="tb"></tbody>
        </table>
      </div>
    </div>

    <div class="box">
      <h2>禁用舰种（可选）<span class="hint">
        战斗条件「禁用的单位类型」识别不出时人工补录；截图名不含扩展名</span></h2>
      <div id="banned"></div>
      <div class="tools"><button id="addBan">+ 增加一条</button></div>
    </div>

    <div class="box">
      <h2>变项库存（可选）<span class="hint">
        仅当本阶段截图<b>全部没有</b>工具栏时生效；标准变项<b>留空 = 无上限</b></span></h2>
      <div class="tools" id="varRow"></div>
      <div class="tools">
        <button id="varStdClear">标准变项置空（= 无上限）</button>
        <button id="varStdZero">标准变项置零（= 不可用）</button>
        <button id="varStdAll">标准变项置满（= 9）</button>
        <button id="varClear">全部清空</button>
        <span class="hint" style="color:var(--dim);font-size:11.5px">
          标准变项（商店可买）：留空 = 无上限，0 = 本轮不用，N = 最多 N 个；
          特殊变项（关卡掉落）：留空 = 0</span>
      </div>
    </div>

    <div class="box">
      <h2>禁用变项（可选）<span class="hint">
        区域条件「禁用变项」：面板只写这四个字，禁用的是哪几个变项要照游戏里的工具栏勾</span></h2>
      <div id="disVar"></div>
      <div class="tools"><button id="addDis">+ 增加一条</button></div>
    </div>
  </div>

  <div>
    <div class="box">
      <h2>预览 / 保存</h2>
      <div class="tools">
        <button class="pri" id="save">保存到 data\\player_input.json</button>
        <button id="dl">下载 player_input.json</button>
        <button id="cp">复制</button>
        <button id="ld">读取本地文件…</button>
        <button id="reload" style="display:none">重新载入现有输入</button>
        <input type="file" id="file" accept=".json" style="display:none">
      </div>
      <div class="msg" id="msg"></div>
      <pre id="out"></pre>
    </div>
    <div class="box">
      <h2>字段说明</h2>
      <div class="foot">
        · <b>missing_cards</b>：上表里<u>取消勾选</u>的卡（未拥有），其余默认拥有<br>
        · <b>damaged_cards</b>：剩余格数 &lt; 3 的卡（3 格 = 满，不写）<br>
        · <b>secondary_uses_max</b>：留空 = 不设上限；填 0 = 本轮一个都不用<br>
        · <b>variations</b>：标准变项（商店可买）留空 = <b>无上限</b>，
        0 = 本轮不用，N = 最多 N 个；特殊变项（关卡掉落）留空 = 0<br>
        · <b>disabled_variations</b>：区域条件「禁用变项」禁用的具体变项
        （按截图名；不填则该据点按不禁用处理并提示人工确认）<br>
        · 保存后直接用「求解整局.bat」跑；旧文件会被备份成
        <code>player_input.json.bak</code>
      </div>
    </div>
  </div>
</div>

<div class="foot">
  卡表来源：data/our_fleet.json（等级配色与建议面板一致）·
  本页由 code/m13_input.py 生成，只负责生成输入文件，不做计算
</div>

<script>const CFG = %(data)s;</script>
<script>
const R = CFG.roster, HPMAX = CFG.hpMax;
const state = {owned:{}, hp:{}, banned:[], disabled:[], vars:null};
const typeBtns = {};          // 舰种 → 状态刷新函数（工具条按钮）
R.forEach(c => { state.owned[c.id] = true; state.hp[c.id] = HPMAX; });
let filterType = null;

function el(t, cls, html){ const e=document.createElement(t);
  if(cls) e.className=cls; if(html!==undefined) e.innerHTML=html; return e; }
const tcls = t => t ? 'tier-' + t : '';

/* ---------- 卡表 ---------- */
function rowData(){
  return R.map(c => ({...c, owned: state.owned[c.id], hp: state.hp[c.id]}));
}
function renderTable(){
  const q = document.getElementById('q').value.trim().toLowerCase();
  const onlyOff = document.getElementById('onlyOff').checked;
  const tb = document.getElementById('tb');
  tb.innerHTML = '';
  let shown = 0;
  rowData().filter(c =>
      (!filterType || c.type === filterType) &&
      (!q || c.name.toLowerCase().includes(q)) &&
      (!onlyOff || !c.owned)).forEach(c => {
    shown++;
    const tr = el('tr', c.owned ? '' : 'gone');
    const td1 = el('td'); const cb = el('input');
    cb.type = 'checkbox'; cb.checked = c.owned;
    cb.onchange = () => { state.owned[c.id] = cb.checked; refresh(); };
    td1.appendChild(cb); tr.appendChild(td1);
    tr.appendChild(el('td', tcls(c.tier),
      '<span class="nm">' + c.name + '</span>'));
    tr.appendChild(el('td', 'num', c.power[0] + '–' + c.power[1]));
    tr.appendChild(el('td', '', '<span class="lv ' + tcls(c.tier) + '">' +
      (CFG.LEVEL_ZH ? '' : '') +
      ({white:'1级',blue:'2级',gold:'3级',red:'3级'}[c.tier] || '—') + '</span>'));
    tr.appendChild(el('td', '', c.type || '—'));
    const td6 = el('td');
    const sel = el('select');
    [[3,'3 格（满）'],[2,'2 格'],[1,'1 格'],[0,'0 格（不可用）']]
      .forEach(([v, t]) => {
        const o = el('option', '', t); o.value = String(v);
        if (state.hp[c.id] === v) o.selected = true;
        sel.appendChild(o);
      });
    sel.onchange = () => { state.hp[c.id] = parseInt(sel.value, 10);
                           refresh(); };
    td6.appendChild(sel); tr.appendChild(td6);
    tb.appendChild(tr);
  });
  if (!shown) tb.appendChild(el('tr', '', '<td colspan="6" class="foot">' +
    '没有匹配的卡牌</td>'));
  const missing = R.filter(c => !state.owned[c.id]).length;
  document.getElementById('ownHint').textContent =
    '已拥有 ' + (R.length - missing) + ' / ' + R.length +
    '，未拥有 ' + missing + ' 张' + (filterType ? '（筛选中：' + filterType + '）' : '');
}

/* ---------- 禁用舰种 ---------- */
function renderBanned(){
  const box = document.getElementById('banned');
  box.innerHTML = '';
  if (!state.banned.length)
    box.appendChild(el('div', 'foot', '（没有 —— 识别不出禁用舰种时再补）'));
  state.banned.forEach((b, i) => {
    const row = el('div', 'row'); row.style.marginBottom = '6px';
    const t = el('input'); t.type = 'text'; t.placeholder = '截图名（不含扩展名）';
    t.value = b.shot; t.oninput = () => { b.shot = t.value; refresh(); };
    row.appendChild(t);
    CFG.types.forEach(ty => {
      const w = el('span', 'cl');
      const cb = el('input'); cb.type = 'checkbox';
      cb.checked = b.types.includes(ty);
      cb.onchange = () => {
        b.types = cb.checked ? b.types.concat([ty])
                             : b.types.filter(x => x !== ty);
        refresh();
      };
      w.appendChild(cb); w.appendChild(el('span', '', ty));
      row.appendChild(w);
    });
    const del = el('button', '', '删除');
    del.onclick = () => { state.banned.splice(i, 1); renderBanned(); refresh(); };
    row.appendChild(del);
    box.appendChild(row);
  });
}

/* ---------- 禁用变项（按截图） ---------- */
function renderDisVar(){
  const box = document.getElementById('disVar');
  box.innerHTML = '';
  if (!state.disabled.length)
    box.appendChild(el('div', 'foot', '（没有 —— 面板有「禁用变项」条件时再填）'));
  state.disabled.forEach((b, i) => {
    const row = el('div', 'row'); row.style.marginBottom = '6px';
    const t = el('input'); t.type = 'text';
    t.placeholder = '截图名（不含扩展名）';
    t.value = b.shot; t.oninput = () => { b.shot = t.value; refresh(); };
    row.appendChild(t);
    CFG.varNames.forEach(vn => {
      const w = el('span', 'cl');
      const cb = el('input'); cb.type = 'checkbox';
      cb.checked = b.names.includes(vn);
      cb.onchange = () => {
        b.names = cb.checked ? b.names.concat([vn])
                             : b.names.filter(x => x !== vn);
        refresh();
      };
      w.appendChild(cb); w.appendChild(el('span', '', vn));
      row.appendChild(w);
    });
    const del = el('button', '', '删除');
    del.onclick = () => { state.disabled.splice(i, 1); renderDisVar(); refresh(); };
    row.appendChild(del);
    box.appendChild(row);
  });
}

/* ---------- 变项库存 ---------- */
function renderVars(){
  const row = document.getElementById('varRow');
  row.innerHTML = '';
  const arr = state.vars || new Array(11).fill(null);
  for (let i = 0; i < 11; i++){
    const w = el('span', 'cl'); w.style.flexDirection = 'column';
    w.style.alignItems = 'flex-start';
    const name = CFG.slots[String(i)] || ('槽 ' + i);
    const inp = el('input'); inp.type = 'number'; inp.min = '0'; inp.step = '1';
    inp.style.width = '64px';
    inp.placeholder = '—';
    inp.value = arr[i] === null || arr[i] === undefined ? '' : arr[i];
    inp.oninput = () => {
      const cur = state.vars ? state.vars.slice() : new Array(11).fill(null);
      cur[i] = inp.value === '' ? null : Math.max(0, parseInt(inp.value, 10) || 0);
      state.vars = cur;
      refresh(true);
    };
    w.appendChild(inp);
    const isStd = i < CFG.stdCount;
    const lb = el('span', 'foot', name + (isStd ? ' · 标准' : ' · 特殊'));
    lb.style.fontSize = '10.5px';
    if (isStd) lb.style.color = 'var(--ok)';
    w.appendChild(lb);
    row.appendChild(w);
  }
}

/* ---------- 合成 / 预览 ---------- */
function compose(){
  const missing = R.filter(c => !state.owned[c.id]).map(c => c.id)
                   .sort((a, b) => a - b);
  const damaged = {};
  R.forEach(c => { const h = state.hp[c.id];
                   if (h !== null && h < HPMAX) damaged[c.id] = h; });
  const banned = {};
  state.banned.forEach(b => {
    const k = (b.shot || '').trim();
    if (k && b.types.length) banned[k] = b.types.slice();
  });
  const disabled = {};
  state.disabled.forEach(b => {
    const k = (b.shot || '').trim();
    if (k && b.names.length) disabled[k] = b.names.slice();
  });
  let vars = state.vars;
  // 全是 null = 未指定（标准变项按「无上限」）；只要有一个数字就照原样写出
  if (vars && vars.every(v => v === null)) vars = null;
  const doc = {
    _meta: {desc: '由 data/input_editor.html（code/m13_input.py）生成：' +
                  '卡牌默认全部拥有，missing_cards = 手动反选的未拥有卡'},
    stage: {current: parseInt(document.getElementById('cur').value, 10) || 1,
            total: parseInt(document.getElementById('total').value, 10) || 1},
    secondary_uses_max: document.getElementById('uses').value === ''
      ? null : Math.max(0, parseInt(document.getElementById('uses').value, 10) || 0),
    missing_cards: missing,
    damaged_cards: damaged,
    banned_types: banned,
    disabled_variations: disabled,
    variations: vars
  };
  return doc;
}
function refresh(keepVars){
  renderTable();
  if (!keepVars) renderVars();
  Object.values(typeBtns).forEach(f => f());
  document.getElementById('out').textContent =
    JSON.stringify(compose(), null, 2);
}
function setMsg(text, cls){
  const m = document.getElementById('msg');
  m.className = 'msg ' + (cls || ''); m.textContent = text || '';
}

/* ---------- 导入 / 导出 ---------- */
function applyDoc(d){
  if (!d) return;
  const st = d.stage || {};
  document.getElementById('cur').value = st.current || 1;
  document.getElementById('total').value = st.total || 1;
  const u = d.secondary_uses_max;
  document.getElementById('uses').value = (u === null || u === undefined)
    ? '' : u;
  const missing = new Set((d.missing_cards || []).map(Number));
  const dmg = d.damaged_cards || {};
  R.forEach(c => {
    state.owned[c.id] = !missing.has(c.id);
    state.hp[c.id] = (dmg[c.id] !== undefined) ? Number(dmg[c.id]) : HPMAX;
  });
  state.banned = Object.entries(d.banned_types || {})
    .map(([k, v]) => ({shot: k, types: v.slice()}));
  state.disabled = Object.entries(d.disabled_variations || {})
    .map(([k, v]) => ({shot: k, names: v.slice()}));
  const varv = d.variations;
  state.vars = Array.isArray(varv) ? varv.slice()
             : (varv && typeof varv === 'object')
               ? (() => { const a = new Array(11).fill(null);
                          for (const [k, v] of Object.entries(varv)) {
                            const i = /^\\d+$/.test(k)
                              ? parseInt(k, 10)
                              : Object.entries(CFG.slots)
                                  .find(([, nm]) => nm === k)?.[0];
                            if (i !== undefined && i !== null) a[Number(i)] = v;
                          }
                          return a; })()
             : null;
  renderBanned(); renderDisVar(); renderVars(); refresh();
  setMsg('已载入现有输入', 'ok');
}

document.getElementById('q').oninput = renderTable;
document.getElementById('onlyOff').onchange = renderTable;
document.getElementById('allOn').onclick = () => {
  R.forEach(c => { state.owned[c.id] = true; }); refresh(); };
document.getElementById('allOff').onclick = () => {
  R.forEach(c => { state.owned[c.id] = false; }); refresh(); };
document.getElementById('addBan').onclick = () => {
  state.banned.push({shot: '', types: []}); renderBanned(); refresh(); };
document.getElementById('addDis').onclick = () => {
  state.disabled.push({shot: '', names: []}); renderDisVar(); refresh(); };

function setStdVars(v){
  const arr = state.vars ? state.vars.slice() : new Array(11).fill(null);
  for (let i = 0; i < CFG.stdCount; i++) arr[i] = v;
  state.vars = arr;
  renderVars(); refresh(true);
  setMsg(v === null ? '标准变项已置空 —— 视为无上限（商店可买）'
       : v === 0 ? '标准变项已置零 —— 本轮这几个不用'
                 : '标准变项已置 ' + v, 'ok');
}
document.getElementById('varStdClear').onclick = () => setStdVars(null);
document.getElementById('varStdZero').onclick = () => setStdVars(0);
document.getElementById('varStdAll').onclick = () => setStdVars(9);
document.getElementById('varClear').onclick = () => {
  state.vars = null; renderVars(); refresh(true);
  setMsg('变项库存已全部清空（= 未指定，标准变项按无上限）', 'ok'); };
['cur', 'total', 'uses'].forEach(id =>
  document.getElementById(id).oninput = () => refresh(true));

const typeBox = document.getElementById('typeBtns');
CFG.types.forEach(ty => {
  const b = el('button', '', ty);
  b.onclick = () => {
    filterType = (filterType === ty) ? null : ty;
    [...typeBox.querySelectorAll('button.f')].forEach(
      x => x.classList.toggle('on', x === b && !!filterType));
    renderTable();
  };
  b.classList.add('f');
  typeBox.appendChild(b);
  // 本类「拥有 / 未拥有」切换：按钮文字表明**点下去会发生什么**
  const toggle = el('button', '', '');
  typeBox.appendChild(toggle);
  typeBtns[ty] = () => {
    const of = R.filter(c => c.type === ty);
    const allOwned = of.length > 0 && of.every(c => state.owned[c.id]);
    toggle.textContent = allOwned ? '⊘ 全未拥有' : '✓ 全拥有';
    toggle.title = allOwned ? '把本类卡全部标为「未拥有」'
                            : '把本类卡全部标回「拥有」';
    toggle.className = allOwned ? 'bad' : 'ok';
  };
  toggle.onclick = () => {
    const of = R.filter(c => c.type === ty);
    const allOwned = of.length > 0 && of.every(c => state.owned[c.id]);
    of.forEach(c => { state.owned[c.id] = !allOwned; });
    refresh();
  };
});

function doDownload(){
  const blob = new Blob([JSON.stringify(compose(), null, 2) + '\\n'],
                        {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = 'player_input.json';
  a.click(); URL.revokeObjectURL(a.href);
}
document.getElementById('dl').onclick = () => {
  doDownload();
  setMsg('已下载 player_input.json —— 放到 data\\\\ 目录覆盖原文件即可', 'ok');
};
document.getElementById('cp').onclick = async () => {
  try { await navigator.clipboard.writeText(
      JSON.stringify(compose(), null, 2));
    setMsg('已复制到剪贴板', 'ok'); }
  catch (e) { setMsg('复制失败，请手动选择预览框内容', 'bad'); }
};
document.getElementById('ld').onclick = () =>
  document.getElementById('file').click();
document.getElementById('file').onchange = async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try { applyDoc(JSON.parse(await f.text())); }
  catch (err) { setMsg('读取失败：' + err.message, 'bad'); }
};

// 「保存到 data\\player_input.json」= 主按钮：服务模式直接写盘；
// 直接打开文件时浏览器不允许写盘 → 提示后自动改成下载（一次点击不白费）
document.getElementById('save').onclick = async () => {
  if (!CFG.serve){
    setMsg('当前是直接打开的页面，浏览器不允许写盘 —— 已改为下载：把下载的 '
      + 'player_input.json 放进 data\\\\ 目录即可；'
      + '双击「编写输入.bat」打开本页就能一键保存', 'warn');
    doDownload();
    return;
  }
  setMsg('保存中…');
  try {
    const r = await fetch('/save', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(compose())});
    const j = await r.json();
    if (j.ok) setMsg('已保存到 ' + j.path +
      (j.backup ? '（旧文件备份为 ' + j.backup + '）' : ''), 'ok');
    else setMsg('保存失败：' + j.error, 'bad');
  } catch (e) { setMsg('保存失败：' + e.message, 'bad'); }
};

if (CFG.serve){
  const rl = document.getElementById('reload');
  rl.style.display = '';
  rl.onclick = async () => {
    try { const r = await fetch('/current'); const j = await r.json();
      if (j.doc) applyDoc(j.doc); else setMsg('data 下还没有 player_input.json', 'warn');
    } catch (e) { setMsg('读取失败：' + e.message, 'bad'); }
  };
  (async () => { try { const r = await fetch('/current'); const j = await r.json();
    if (j.doc) applyDoc(j.doc); } catch (e) {} })();
}

renderBanned(); renderDisVar(); refresh();
</script>
</body>
</html>
"""


# ============ 本地服务（保存回写） ============
def make_handler(input_path):
    class _H(BaseHTTPRequestHandler):
        html = None

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ('/', '/index.html'):
                body = self.html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type',
                                 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == '/current':
                p = Path(input_path)
                doc = None
                if p.exists():
                    try:
                        doc = json.loads(p.read_text(encoding='utf-8'))
                    except Exception as e:            # 坏文件不该让页面崩
                        self._json({'doc': None, 'error': str(e)})
                        return
                self._json({'doc': doc, 'path': str(p)})
                return
            self.send_error(404)

        def do_POST(self):
            if self.path != '/save':
                self.send_error(404)
                return
            n = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(n)
            try:
                doc = json.loads(raw.decode('utf-8'))
                p, bak, clean = save_input(doc, input_path)
                print('  [保存] %s（未拥有 %d 张 · 损耗 %d 张）'
                      % (p.name, len(clean['missing_cards']),
                         len(clean['damaged_cards'])))
                self._json({'ok': True, 'path': str(p),
                            'backup': bak.name if bak else None})
            except Exception as e:
                print('  [保存失败] %s' % e)
                self._json({'ok': False, 'error': str(e)}, 400)

        def log_message(self, fmt, *args):
            print('  [输入页] ' + fmt % args)

    return _H


def serve(port=8766, input_path=None, roster=None, var_slots=None,
          open_browser=True):
    inp = str(input_path or DEFAULT_INPUT)
    handler = make_handler(inp)
    handler.html = build_html(roster or load_roster(),
                              var_slots if var_slots is not None
                              else load_var_slots(), serve=True)
    url = 'http://127.0.0.1:%d/' % port
    print('【输入编辑页】%s  （保存目标 %s，旧文件自动备份为 .bak）'
          % (url, inp))
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        HTTPServer(('127.0.0.1', port), handler).serve_forever()
    except KeyboardInterrupt:
        print('\n已退出')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='M6-4 玩家输入编辑页')
    ap.add_argument('--out', default=str(DEFAULT_OUT),
                    help='生成的 HTML 路径（非 --serve 模式）')
    ap.add_argument('--serve', action='store_true',
                    help='启动本地服务，页面可直接保存回写输入文件')
    ap.add_argument('--port', type=int, default=8766)
    ap.add_argument('--input-path', default=str(DEFAULT_INPUT),
                    help='保存目标（默认 data/player_input.json）')
    ap.add_argument('--no-open', action='store_true',
                    help='--serve 时不自动打开浏览器')
    args = ap.parse_args(argv)

    roster, var_slots = load_roster(), load_var_slots()
    if args.serve:
        return serve(args.port, args.input_path, roster, var_slots,
                     open_browser=not args.no_open)

    html = build_html(roster, var_slots, serve=False)
    Path(args.out).write_text(html, encoding='utf-8')
    print('【输入编辑页】%s —— 浏览器双击打开；'
          '要直接保存回 data/ 用：--serve' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
