# -*- coding: utf-8 -*-
"""
build_TVchart.py
================
跑回测 → 提取 K 线 + 交易明细 → 一次生成两张图:
  1) orb_report_<version>.html  统计报告 (NautilusTrader tearsheet)
  2) orb_chart_<version>.html   K 线图 (TradingView Lightweight Charts)

用法:
  cd ORB_strategy && ../.venv/bin/python build_TVchart.py [v1|v2|v3|v4|v5|v8]

输出 (在 html_output/ 目录):
  orb_report_<version>.html + orb_chart_<version>.html
"""
import json
import sys
import importlib
import inspect

import pandas as pd

from nautilus_trader.analysis import create_tearsheet
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

VERSION = sys.argv[1] if len(sys.argv) > 1 else "v4"
MOD = importlib.import_module(f"orb_backtes_{VERSION}")

LWC_CDN = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def to_sec(ts) -> int:
    """NautilusTrader 时间戳 (Timestamp 或 ns int) → Unix 秒。"""
    if isinstance(ts, (int, float)):
        return int(ts / 1_000_000_000)  # ns → s
    return int(pd.Timestamp(ts).timestamp())


def money_float(x) -> float:
    """'123.45 USD' / '1,234.56 USD' / Money → float。"""
    s = str(x).replace(",", "")
    for tok in s.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 0.0


# 配置字段名 → 模块常量名 映射 (按需扩展, 兼容 v1-v8 字段差异)
CONFIG_FIELDS = {
    "max_loss_usd": "MAX_LOSS_USD",
    "risk_per_trade": "RISK_PER_TRADE",
    "risk_reward": "RISK_REWARD",
    "atr_stop_fraction": "ATR_STOP_FRACTION",
    "max_qty": "MAX_QTY",
    "use_leverage_cap": "USE_LEVERAGE_CAP",
    "max_leverage": "MAX_LEVERAGE",
}


def build_config(bar_type):
    """按模块暴露的常量构造 OrbStrategyConfig (兼容 v1-v8 字段差异)。"""
    kw = {
        "instrument_id": MOD.INSTRUMENT_ID,
        "bar_type": str(bar_type),
        "multiplier": MOD.MULTIPLIER,
    }
    for field, const in CONFIG_FIELDS.items():
        if hasattr(MOD, const):
            kw[field] = getattr(MOD, const)
    return MOD.OrbStrategyConfig(**kw)


def build_strategy(cfg, atr_map, range_map):
    """按 OrbStrategy.__init__ 的参数名注入依赖 (兼容 v1-v8 构造差异)。"""
    params = list(inspect.signature(MOD.OrbStrategy.__init__).parameters.keys())
    args = []
    for name in params:
        if name == "self":
            continue
        if name == "config":
            args.append(cfg)
        elif name == "atr_map":
            args.append(atr_map)
        elif name == "range_map":
            args.append(range_map)
        else:
            raise ValueError(f"未识别的策略构造参数: {name}")
    return MOD.OrbStrategy(*args)


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------
def run_backtest():
    atr_map = MOD.build_atr_map() if hasattr(MOD, "build_atr_map") else None
    range_map = MOD.build_range_map() if hasattr(MOD, "build_range_map") else None
    bars, instrument, bar_type = MOD.build_bars_and_instrument()

    venue = Venue(MOD.VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId(f"ORB-BT-CHT-{VERSION}")))
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(MOD.STARTING_CAPITAL, USD)],
        fee_model=PerContractFeeModel(Money(MOD.COMMISSION_PER_CONTRACT, USD)),
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)

    cfg = build_config(bar_type)
    strategy = build_strategy(cfg, atr_map, range_map)
    engine.add_strategy(strategy)
    engine.run()
    return engine, bars


# ---------------------------------------------------------------------------
# 提取 K 线
# ---------------------------------------------------------------------------
def extract_bars(bars):
    out = []
    for b in bars:
        t = b.ts_event // 1_000_000_000  # ns → s
        out.append([
            t,
            round(b.open.as_double(), 2),
            round(b.high.as_double(), 2),
            round(b.low.as_double(), 2),
            round(b.close.as_double(), 2),
            int(b.volume.as_double()),
        ])
    return out


# ---------------------------------------------------------------------------
# 提取交易
# ---------------------------------------------------------------------------
def extract_trades(engine):
    pos = engine.trader.generate_positions_report()
    ordr = engine.trader.generate_orders_report()

    # 平仓单类型查找表: client_order_id -> type
    closing_type = {}
    for idx, row in ordr.iterrows():
        closing_type[idx] = str(row["type"])

    # 止损/止盈挂单: (ts_init_ns, type, trigger_price/avg_px)
    stop_orders, limit_orders = [], []
    for idx, row in ordr.iterrows():
        t_init = row["ts_init"]
        t_init = int(t_init) if isinstance(t_init, (int, float)) else pd.Timestamp(t_init).value
        typ = str(row["type"])
        if "STOP" in typ and row["trigger_price"] is not None:
            stop_orders.append((t_init, float(row["trigger_price"])))
        elif "LIMIT" in typ and row["avg_px"] is not None:
            limit_orders.append((t_init, float(row["avg_px"])))
    stop_orders.sort()
    limit_orders.sort()

    def find_near(orders, t_open_ns, t_close_ns):
        """挂单时间在开仓后 10 分钟内, 且不晚于平仓时间。"""
        lo = t_open_ns
        hi = t_open_ns + 10 * 60_000_000_000
        cand = [px for (t, px) in orders if lo <= t <= hi]
        return cand[0] if cand else None

    trades = []
    for _, p in pos.iterrows():
        # 只处理已平仓 (quantity==0 且 ts_closed 存在)
        if p["ts_closed"] is None:
            continue

        side = 1 if str(p["entry"]) == "BUY" else -1  # 1=多, -1=空
        entry_t = to_sec(p["ts_opened"])
        exit_t = to_sec(p["ts_closed"])
        entry_px = float(p["avg_px_open"])
        exit_px = float(p["avg_px_close"])
        qty = int(p["peak_qty"])
        pnl = money_float(p["realized_pnl"])

        ctype = closing_type.get(p["closing_order_id"], "MARKET")
        reason = "stop" if "STOP" in ctype else ("tp" if "LIMIT" in ctype else "eod")

        # 止损/止盈价位
        t_open_ns = pd.Timestamp(p["ts_opened"]).value
        t_close_ns = pd.Timestamp(p["ts_closed"]).value
        sl = find_near(stop_orders, t_open_ns, t_close_ns)
        tp = find_near(limit_orders, t_open_ns, t_close_ns)

        trades.append({
            "et": entry_t, "ep": entry_px,
            "xt": exit_t, "xp": exit_px,
            "s": side, "q": qty, "p": round(pnl, 2), "r": reason,
            "sl": sl, "tp": tp,
        })
    return trades


# ---------------------------------------------------------------------------
# 生成 HTML
# ---------------------------------------------------------------------------
def gen_html(bars, trades):
    n_win = sum(1 for t in trades if t["p"] > 0)
    n = len(trades)
    win_rate = (n_win / n * 100) if n else 0.0
    pnl_total = sum(t["p"] for t in trades)
    data = {
        "bars": bars,
        "trades": trades,
        "stats": {
            "version": VERSION,
            "bars": len(bars),
            "trades": n,
            "win_rate": round(win_rate, 1),
            "pnl_total": round(pnl_total, 2),
        },
    }
    data_json = json.dumps(data, separators=(",", ":"))

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>NQ ORB 回测图表 ({VERSION})</title>
<style>
  body {{ margin:0; background:#131722; color:#d1d4dc; font-family:-apple-system,'Segoe UI',Roboto,sans-serif; }}
  #toolbar {{ position:absolute; top:10px; left:10px; z-index:10; background:rgba(19,23,34,0.9);
    padding:10px 14px; border-radius:6px; border:1px solid #2a2e39; font-size:13px; line-height:1.6; }}
  #toolbar b {{ color:#fff; }}
  #chart {{ position:absolute; inset:0; }}
</style>
</head>
<body>
<div id="chart"></div>
<div id="toolbar">
  <b>NQ 5m ORB · {VERSION}</b><br>
  K线 <span id="st-bars">0</span> 根 · 交易 <span id="st-trades">0</span> 笔<br>
  胜率 <span id="st-win">0</span>% · 总盈亏 $<span id="st-pnl">0</span>
</div>
<script src="{LWC_CDN}"></script>
<script>
const DATA = {data_json};
const STATS = DATA.stats;
document.getElementById('st-bars').textContent = STATS.bars.toLocaleString();
document.getElementById('st-trades').textContent = STATS.trades.toLocaleString();
document.getElementById('st-win').textContent = STATS.win_rate;
document.getElementById('st-pnl').textContent = STATS.pnl_total.toLocaleString();

// 时区: 数据是 UTC 时间戳, Lightweight Charts 默认按浏览器本地时区渲染;
// 这里平移到美东墙钟时间(含 DST), 使图表无论浏览器时区都显示美东时间。
const TZ = 'America/New_York';
const timeToTz = (t, zone) => new Date(new Date(t * 1000).toLocaleString('en-US', {{ timeZone: zone }})).getTime() / 1000;

const chart = LightweightCharts.createChart(document.getElementById('chart'), {{
  layout: {{ background: {{ type:'solid', color:'#131722' }}, textColor:'#d1d4dc' }},
  grid: {{ vertLines:{{ color:'#1e222d' }}, horzLines:{{ color:'#1e222d' }} }},
  rightPriceScale: {{ borderColor:'#2a2e39' }},
  timeScale: {{ borderColor:'#2a2e39', timeVisible:true, secondsVisible:false }},
  crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
}});

const candle = chart.addCandlestickSeries({{
  upColor:'#26a69a', downColor:'#ef5350', borderVisible:false,
  wickUpColor:'#26a69a', wickDownColor:'#ef5350',
}});
candle.setData(DATA.bars.map(b => ({{ time:timeToTz(b[0], TZ), open:b[1], high:b[2], low:b[3], close:b[4] }})));

const vol = chart.addHistogramSeries({{ priceScaleId:'vol', scaleMargins:{{ top:0.85, bottom:0 }} }});
chart.priceScale('vol').applyOptions({{ scaleMargins:{{ top:0.85, bottom:0 }} }});
vol.setData(DATA.bars.map(b => ({{ time:timeToTz(b[0], TZ), value:b[5],
  color: b[4]>=b[1] ? 'rgba(38,166,154,0.35)' : 'rgba(239,83,80,0.35)' }})));

const REASON = {{ stop:{{label:'止损',color:'#ef5350'}}, tp:{{label:'止盈',color:'#26a69a'}}, eod:{{label:'收盘',color:'#787b86'}} }};
const markers = [];
for (const t of DATA.trades) {{
  const long = t.s === 1;
  markers.push({{
    time: timeToTz(t.et, TZ), position: long ? 'belowBar' : 'aboveBar',
    shape: long ? 'arrowUp' : 'arrowDown', color: long ? '#26a69a' : '#ef5350',
    text: (long ? '多 ' : '空 ') + t.q + '手 @' + t.ep.toFixed(2),
  }});
  const rr = REASON[t.r] || REASON.eod;
  markers.push({{
    time: timeToTz(t.xt, TZ), position: long ? 'aboveBar' : 'belowBar',
    shape: 'circle', color: rr.color,
    text: rr.label + ' @' + t.xp.toFixed(2) + ' · PnL $' + t.p.toLocaleString(),
  }});
}}
markers.sort((a,b) => a.time - b.time);
candle.setMarkers(markers);

// 止损/止盈价格线 (取第一笔有值的交易作示例)
const sample = DATA.trades.find(t => t.sl != null);
if (sample && sample.sl != null) candle.createPriceLine({{ price:sample.sl, color:'#ef5350', lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'止损示例' }});
if (sample && sample.tp != null) candle.createPriceLine({{ price:sample.tp, color:'#26a69a', lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'止盈示例' }});

// 初始视图: 末尾约 1 个月
const N = DATA.bars.length;
chart.timeScale().setVisibleLogicalRange({{ from: Math.max(0, N-1600), to: N+5 }});
window.addEventListener('resize', () => chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight }}));
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    print(f"[1/4] 运行 {VERSION} 回测 ...", flush=True)
    engine, bars = run_backtest()
    print(f"      {len(bars):,} 根 Bar")

    print("[2/4] 提取交易明细 ...", flush=True)
    trades = extract_trades(engine)
    print(f"      {len(trades):,} 笔交易")

    print("[3/4] 生成统计报告 (tearsheet) ...", flush=True)
    report_path = f"html_output/orb_report_{VERSION}.html"
    create_tearsheet(engine, output_path=report_path, title=f"NQ 5min ORB {VERSION} 回测报告")

    print("[4/4] 生成 K 线图 (Lightweight Charts) ...", flush=True)
    bar_list = extract_bars(bars)
    html = gen_html(bar_list, trades)
    chart_path = f"html_output/orb_chart_{VERSION}.html"
    with open(chart_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n已生成两张图:")
    print(f"  {report_path}  ({os.path.getsize(report_path)/1e6:.1f} MB)  ← 统计报告")
    print(f"  {chart_path}  ({os.path.getsize(chart_path)/1e6:.1f} MB)  ← K线图")
    print("K线图需联网加载 Lightweight Charts CDN; 统计报告离线可开。")
