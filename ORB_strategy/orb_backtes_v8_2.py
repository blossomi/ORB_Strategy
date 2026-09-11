# -*- coding: utf-8 -*-
"""
orb_backtest_v8_2.py
====================
NQ 期货 5 分钟 ORB 日内策略 —— NautilusTrader 版本 (v8.2)

v8.2: 在 v8.1 基础上, 时段扩展到 2016-01 ~ 2026-08-30, 并加 10R 止盈
  - 区间: 9:00-9:30 ET 的高低价 (盘前 30 分钟, 从 ETH 数据计算)
  - 入场窗口: 9:30-10:00 ET, 首根 K 线收盘即突破则以收盘价立即入场
  - 突破: 向上突破区间高点 → 做多; 向下突破区间低点 → 做空
  - 未突破 → 当日不交易
  - 止损: 5% × 14日ATR (前一日, 无未来函数)
  - 止盈: 10R (限价单), 未触及则 EOD 清仓
  - 仓位: 以损定仓 floor(equity × 1% / (止损点数 × $20)), 单笔最大 40 手

本脚本末尾直接生成两张图表:
  1) html_output/orb_report_v8_2.html  统计报告 (tearsheet)
  2) html_output/orb_chart_v8_2.html   K 线图 (Lightweight Charts)

用法: cd ORB_strategy && ../.venv/bin/python orb_backtes_v8_2.py
"""
import json
from datetime import time as dtime
from datetime import date as ddate
from math import floor

import pandas as pd
import zoneinfo

from nautilus_trader.analysis import create_tearsheet
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import StrategyConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
DATA_PATH = "nq_5min_rth.parquet"                  # 回测数据 (RTH 9:30-16:00)
RANGE_DATA_PATH = "nq_5min_eth.parquet"            # 计算 9:00-9:30 区间 (ETH 全时段)
INSTRUMENT_ID = "NQ.GLBX"                          # 合成"连续 NQ"合约
VENUE = "GLBX"
MULTIPLIER = 20.0                                  # NQ 点值 $20/点
TICK = 0.25
PRICE_PRECISION = 2

STARTING_CAPITAL = 50_000                          # 起始资金(美元)
RISK_PER_TRADE = 0.01                              # 每笔风险 = 权益的 1% (复利)
RISK_REWARD = 10.0                                 # 止盈倍数 R
ATR_PERIOD = 14                                    # ATR 周期
ATR_STOP_FRACTION = 0.05                           # 止损 = 5% × 14日ATR
COMMISSION_PER_CONTRACT = 0.5                      # 手续费 $/手/边
SLIPPAGE = 0                                       # 滑点(0=无)

MAX_QTY = 4000                                      # 单笔最大手数上限

START_DATE = "2016-01-01"                          # 样本窗口起
END_DATE = "2026-08-30"                            # 样本窗口止

ET = zoneinfo.ZoneInfo("America/New_York")
T_RANGE_START = dtime(9, 0)                        # 区间开始 9:00
T_RANGE_END = dtime(9, 30)                         # 区间结束 9:30
T_WIN_START = dtime(9, 30)                         # 入场窗口开始 (开盘即开始, 含首根 K 线)
T_WIN_END = dtime(10, 0)                           # 入场窗口结束 (开盘 30 分钟)
T_EOD = dtime(15, 55)                              # 收盘平仓

LWC_CDN = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"

# 策略简要描述 (末尾打印)
STRATEGY_DESC = """\
版本       : v8.2
标的       : NQ 期货 (5分钟 RTH)
时段       : 2016-01-01 ~ 2026-08-30
入场       : 9:00-9:30 区间突破, 9:30-10:00 窗口内, 突破当根K线收盘价入场
             (向上突破做多, 向下突破做空, 未突破不交易)
止损       : 5% × 14日ATR (前一日, 无未来函数)
止盈       : 10R (限价单), 未触及则 EOD 清仓
仓位       : floor(equity × 1% / (止损点数 × $20)), 单笔最大 40 手
手续费     : $0.5/手/边, 无滑点"""


def tick_round(px: float) -> float:
    """把价格/点数取整到最小变动价位 0.25 的倍数。"""
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


# ---------------------------------------------------------------------------
# 9:00-9:30 区间: 每日 (high, low), 从 ETH 数据计算
# ---------------------------------------------------------------------------
def build_range_map() -> dict[ddate, tuple[float, float]]:
    df = pd.read_parquet(RANGE_DATA_PATH).tz_convert(ET)
    t = df.index.time
    df = df[(t >= T_RANGE_START) & (t < T_RANGE_END)]
    out = {}
    for d, grp in df.groupby(df.index.normalize()):
        out[d.date()] = (float(grp["high"].max()), float(grp["low"].min()))
    return out


# ---------------------------------------------------------------------------
# ATR: 前一日 14 日 ATR (Wilder), 返回 {ET 交易日 date: atr 点数}
# ---------------------------------------------------------------------------
def build_atr_map() -> dict[ddate, float]:
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    day = (
        df.resample("1D")
        .agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
        .dropna()
    )
    prev_close = day["close"].shift(1)
    tr = pd.concat(
        [
            day["high"] - day["low"],
            (day["high"] - prev_close).abs(),
            (day["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()  # Wilder 平滑
    atr_use = atr.shift(1)                                     # 用前一日 ATR, 无未来函数
    return {d.date(): float(v) for d, v in atr_use.dropna().items()}


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
class OrbStrategyConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    risk_per_trade: float = 0.01
    multiplier: float = 20.0
    risk_reward: float = 10.0
    atr_stop_fraction: float = 0.05
    max_qty: int = 40


class OrbStrategy(Strategy):
    def __init__(self, config: OrbStrategyConfig, atr_map: dict[ddate, float],
                 range_map: dict[ddate, tuple[float, float]]):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.risk_per_trade = config.risk_per_trade
        self.multiplier = config.multiplier
        self.risk_reward = config.risk_reward
        self.atr_stop_fraction = config.atr_stop_fraction
        self.max_qty = config.max_qty
        self.atr_map = atr_map
        self.range_map = range_map
        self.pending_entry = {}               # 入场单 cid -> 止损距离(点)
        self.exit_sibling = {}                # 止损/止盈订单 cid -> 另一方的 Order 对象
        self._cur_date = None                 # 当前交易日
        self.entered_today = False            # 当日是否已入场
        self.n_entries = 0                    # 统计入场次数
        self.n_no_trade = 0                   # 当日无交易(未突破)的天数
        self.n_capped = 0                     # 被最大手数上限压制(qty被砍)的天数

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def _et_time(self, ts_ns: int):
        return pd.Timestamp(ts_ns, unit="ns", tz="UTC").tz_convert(ET).time()

    def _et_date(self, ts_ns: int) -> ddate:
        return pd.Timestamp(ts_ns, unit="ns", tz="UTC").tz_convert(ET).date()

    def _equity(self) -> float:
        eq = self.portfolio.equity(venue=self.instrument_id.venue)
        return eq[USD].as_double()

    def on_bar(self, bar: Bar):
        t = self._et_time(bar.ts_event)
        d = self._et_date(bar.ts_event)

        # 新的一天: 重置当日入场标记
        if d != self._cur_date:
            self._cur_date = d
            self.entered_today = False

        # 入场窗口内: 检查区间突破 (含首根 K 线 9:30)
        if T_WIN_START <= t < T_WIN_END and not self.entered_today:
            rng = self.range_map.get(d)
            if rng is not None:
                rng_high, rng_low = rng
                if bar.high.as_double() > rng_high:
                    self._enter(OrderSide.BUY, bar, d)
                elif bar.low.as_double() < rng_low:
                    self._enter(OrderSide.SELL, bar, d)

        # 收盘: 平仓 + 统计未交易日
        elif t == T_EOD:
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)
            if not self.entered_today:
                self.n_no_trade += 1

    def _enter(self, side: OrderSide, bar: Bar, d: ddate):
        # 止损距离 = 5% × 前一日 14 日 ATR (至少 1 个 tick)
        atr = self.atr_map.get(d)
        if atr is None or atr <= 0:
            return
        stop_dist = max(TICK, tick_round(self.atr_stop_fraction * atr))

        # 入场价 = 突破当根 K 线收盘价
        entry = bar.close.as_double()
        stop_price = tick_round(entry - stop_dist) if side == OrderSide.BUY \
            else tick_round(entry + stop_dist)
        actual_dist = abs(entry - stop_price)
        if actual_dist <= 0:
            return

        # 以损定仓 + 单笔最大手数上限
        equity = self._equity()
        risk_qty = equity * self.risk_per_trade / (actual_dist * self.multiplier)
        if risk_qty > self.max_qty:
            self.n_capped += 1
        qty = int(floor(min(risk_qty, self.max_qty)))
        if qty < 1:
            return

        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=Quantity.from_str(str(qty)),
        )
        self.pending_entry[order.client_order_id] = actual_dist
        self.submit_order(order)
        self.entered_today = True

    def on_order_filled(self, event):
        cid = event.client_order_id

        # 入场单成交 → 挂止损 + 止盈
        if cid in self.pending_entry:
            actual_dist = self.pending_entry.pop(cid)
            self.n_entries += 1

            entry_px = event.last_px.as_double()
            qty = event.last_qty
            side = event.order_side
            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

            stop = tick_round(entry_px - actual_dist) if side == OrderSide.BUY \
                else tick_round(entry_px + actual_dist)
            tp = tick_round(entry_px + self.risk_reward * actual_dist) if side == OrderSide.BUY \
                else tick_round(entry_px - self.risk_reward * actual_dist)

            sl = self.order_factory.stop_market(
                instrument_id=self.instrument_id,
                order_side=exit_side,
                quantity=qty,
                trigger_price=Price.from_str(f"{stop:.2f}"),
            )
            tp_order = self.order_factory.limit(
                instrument_id=self.instrument_id,
                order_side=exit_side,
                quantity=qty,
                price=Price.from_str(f"{tp:.2f}"),
            )
            self.submit_order(sl)
            self.submit_order(tp_order)
            # 记录互为"另一方", 一方成交即撤销另一方 (OUO)
            self.exit_sibling[sl.client_order_id] = tp_order
            self.exit_sibling[tp_order.client_order_id] = sl
            self.log.info(
                f"入场 {side.name} qty={qty} px={entry_px:.2f} "
                f"stop={stop:.2f} tp={tp:.2f} ({actual_dist:.2f}pt)"
            )
            return

        # 止损/止盈成交 → 撤销另一方 (OUO: 避免残留挂单)
        sibling = self.exit_sibling.pop(cid, None)
        if sibling is not None:
            self.cancel_order(sibling)


# ---------------------------------------------------------------------------
# 数据: 读 5 分钟 RTH parquet (过滤到样本窗口) → Nautilus Bar + 合成连续合约
# ---------------------------------------------------------------------------
def build_bars_and_instrument() -> tuple[list[Bar], FuturesContract, BarType]:
    df = pd.read_parquet(DATA_PATH)
    df = df.tz_convert(ET)
    start = pd.Timestamp(START_DATE, tz=ET)
    end = pd.Timestamp(END_DATE, tz=ET) + pd.Timedelta(days=1)
    df = df[(df.index >= start) & (df.index < end)]
    df = df.tz_convert("UTC")
    df = df.sort_index()

    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    first_ns = dt_to_unix_nanos(df.index[0])
    last_ns = dt_to_unix_nanos(df.index[-1])

    instrument = FuturesContract(
        instrument_id=instrument_id,
        raw_symbol=Symbol("NQ"),
        asset_class=AssetClass.INDEX,
        currency=USD,
        price_precision=PRICE_PRECISION,
        price_increment=Price.from_str(f"{TICK:.2f}"),
        multiplier=Quantity.from_str(f"{MULTIPLIER:.2f}"),
        lot_size=Quantity.from_str("1"),
        underlying="NQ",
        activation_ns=first_ns - 86_400_000_000_000,
        expiration_ns=last_ns + 3_652_000_000_000_000,
        ts_event=first_ns,
        ts_init=first_ns,
    )

    bar_type = BarType.from_str(f"{INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL")
    bars = []
    for ts, row in df.iterrows():
        ns = dt_to_unix_nanos(ts)
        bars.append(Bar(
            bar_type=bar_type,
            open=Price.from_str(f"{row['open']:.2f}"),
            high=Price.from_str(f"{row['high']:.2f}"),
            low=Price.from_str(f"{row['low']:.2f}"),
            close=Price.from_str(f"{row['close']:.2f}"),
            volume=Quantity.from_str(str(int(row["volume"]))),
            ts_event=ns,
            ts_init=ns,
        ))
    return bars, instrument, bar_type


# ---------------------------------------------------------------------------
# 图表数据: 提取 K 线 + 交易明细
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


def extract_trades(engine):
    pos = engine.trader.generate_positions_report()
    ordr = engine.trader.generate_orders_report()

    closing_type = {}
    for idx, row in ordr.iterrows():
        closing_type[idx] = str(row["type"])

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
        lo = t_open_ns
        hi = t_open_ns + 10 * 60_000_000_000
        cand = [px for (t, px) in orders if lo <= t <= hi]
        return cand[0] if cand else None

    trades = []
    for _, p in pos.iterrows():
        if p["ts_closed"] is None:
            continue
        side = 1 if str(p["entry"]) == "BUY" else -1
        entry_t = to_sec(p["ts_opened"])
        exit_t = to_sec(p["ts_closed"])
        entry_px = float(p["avg_px_open"])
        exit_px = float(p["avg_px_close"])
        qty = int(p["peak_qty"])
        pnl = money_float(p["realized_pnl"])

        ctype = closing_type.get(p["closing_order_id"], "MARKET")
        reason = "stop" if "STOP" in ctype else ("tp" if "LIMIT" in ctype else "eod")

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


def gen_chart_html(bars, trades):
    """生成 Lightweight Charts K 线图 (内嵌数据)。"""
    n_win = sum(1 for t in trades if t["p"] > 0)
    n = len(trades)
    win_rate = (n_win / n * 100) if n else 0.0
    pnl_total = sum(t["p"] for t in trades)
    data = {
        "bars": bars,
        "trades": trades,
        "stats": {
            "version": "v8.2",
            "bars": len(bars),
            "trades": n,
            "win_rate": round(win_rate, 1),
            "pnl_total": round(pnl_total, 2),
        },
    }
    data_json = json.dumps(data, separators=(",", ":"))

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>NQ ORB 回测图表 (v8.2)</title>
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
  <b>NQ 5m ORB · v8.2</b><br>
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

const sample = DATA.trades.find(t => t.sl != null);
if (sample && sample.sl != null) candle.createPriceLine({{ price:sample.sl, color:'#ef5350', lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'止损示例' }});
if (sample && sample.tp != null) candle.createPriceLine({{ price:sample.tp, color:'#26a69a', lineWidth:1, lineStyle:LightweightCharts.LineStyle.Dashed, axisLabelVisible:true, title:'止盈示例' }});

const N = DATA.bars.length;
chart.timeScale().setVisibleLogicalRange({{ from: Math.max(0, N-1600), to: N+5 }});
window.addEventListener('resize', () => chart.applyOptions({{ width: window.innerWidth, height: window.innerHeight }}));
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os

    print("[1/5] 构建 9:00-9:30 区间映射 ...", flush=True)
    range_map = build_range_map()
    print(f"      区间覆盖 {len(range_map):,} 个交易日")

    print("[2/5] 构建前一日 14 日 ATR 映射 ...", flush=True)
    atr_map = build_atr_map()
    print(f"      ATR 覆盖 {len(atr_map):,} 个交易日")

    print("[3/5] 加载 5 分钟数据...", flush=True)
    bars, instrument, bar_type = build_bars_and_instrument()
    print(f"      {len(bars):,} 根 Bar, {instrument.id}, "
          f"乘数={instrument.multiplier}, 最小变动={instrument.price_increment}")

    print("[4/5] 配置回测引擎 ...", flush=True)
    venue = Venue(VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("ORB-BT-082")))
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(STARTING_CAPITAL, USD)],
        fee_model=PerContractFeeModel(Money(COMMISSION_PER_CONTRACT, USD)),
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)

    config = OrbStrategyConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=str(bar_type),
        risk_per_trade=RISK_PER_TRADE,
        multiplier=MULTIPLIER,
        risk_reward=RISK_REWARD,
        atr_stop_fraction=ATR_STOP_FRACTION,
        max_qty=MAX_QTY,
    )
    strategy = OrbStrategy(config, atr_map, range_map)
    engine.add_strategy(strategy)

    print("[5/5] 运行回测 ...", flush=True)
    engine.run()

    # ---- 结果 ----
    acct = engine.trader.generate_account_report(venue)
    eq = acct['total'].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample('1D').last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())

    final_total = float(eq.iloc[-1])
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    start = pd.Timestamp(START_DATE, tz=ET)
    end = pd.Timestamp(END_DATE, tz=ET) + pd.Timedelta(days=1)
    df = df[(df.index >= start) & (df.index < end)]
    years = (df.index[-1] - df.index[0]).days / 365.25
    annual = (final_total / STARTING_CAPITAL) ** (1.0 / years) - 1.0

    print(f"\n===== 入场次数: {strategy.n_entries:,} =====")
    print(f"===== 当日无交易(未突破): {strategy.n_no_trade:,} 天 =====")
    print(f"===== 被最大手数上限压制(qty被砍): {strategy.n_capped:,} 天 =====")
    print("===== 账户报告(首/尾) =====")
    print(acct.iloc[[0, -1]].to_string())
    print(f"\n最终权益: ${final_total:,.2f}  "
          f"总盈亏: ${final_total - STARTING_CAPITAL:,.2f}  "
          f"收益率: {(final_total / STARTING_CAPITAL - 1) * 100:,.1f}%")
    print(f"回测年限: {years:.2f} 年   年化收益率: {annual * 100:,.1f}%")
    print(f"峰值权益: ${daily.max():,.0f}   最大回撤: {mdd * 100:.1f}%")

    # ---- 生成两张图表 ----
    report_path = "html_output/orb_report_v8_2.html"
    create_tearsheet(engine, output_path=report_path, title="NQ 5min ORB v8.2 回测报告 (2016-2026)")

    trades = extract_trades(engine)
    bar_list = extract_bars(bars)
    chart_path = "html_output/orb_chart_v8_2.html"
    with open(chart_path, "w", encoding="utf-8") as f:
        f.write(gen_chart_html(bar_list, trades))

    print(f"\n已生成两张图表:")
    print(f"  {report_path}  ({os.path.getsize(report_path)/1e6:.1f} MB)  ← 统计报告")
    print(f"  {chart_path}  ({os.path.getsize(chart_path)/1e6:.1f} MB)  ← K线图")

    # ---- 策略简要描述 ----
    print(f"\n===== 策略描述 =====\n{STRATEGY_DESC}")
