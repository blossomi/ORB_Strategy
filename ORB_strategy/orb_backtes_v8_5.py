# -*- coding: utf-8 -*-
"""
orb_backtest_v8_5.py
====================
NQ 期货 5 分钟 ORB 日内策略 —— NautilusTrader 版本 (v8.5)

v8.5 改动 (相对 v8.4):
  - 区间改成「开盘后 20 分钟」: 9:30-9:50 ET 的前 4 根 5min K 线高低价 (纯 RTH)
  - 入场窗口顺延: 9:50-10:10, 逐根 K 线收盘价判断突破
      收盘价涨破区间高点 → 做多
      收盘价跌破区间低点 → 做空
      区间内则等下一根; 10:10 前这 4 根 5min K 线无突破 → 当日放弃交易
  - 止损: 5% × 14日ATR (前一日, 无未来函数, 论文方案)
  - 止盈: 无 (持有至收盘平仓, 不封顶, 保留长尾大赢家)
  - 保本: 浮盈达 10R 时把止损拉到「保本 + 2 tick 缓冲」
      (缓冲覆盖点差 0.25 + 手续费 ~0.05, 对齐 v6 的保本定义)
  - 仓位: 以损定仓 floor(equity × 1% / (止损点数 × $20)), 单笔最大 4000 手

保本机制说明 (5分钟K线粒度, 对齐 v6):
  - 「浮盈达 10R」用触及判断 (bar.high / bar.low), 达到后在 K 线收盘时移动止损,
    移动后从下一根 K 线生效; 若同一根 K 线先触 10R 又回落到初始止损,
    按初始止损平仓 (偏保守)。
  - 拉到保本后不继续跟踪 (非 trailing), 之后要么保本止损、要么持有到收盘。

★ 所有可调参数都集中在下方「配置 / 参数开关区」, 改数值即可, 无需动策略代码。

本脚本末尾直接生成两张图表:
  1) html_output/orb_report_v8_5.html  统计报告 (tearsheet)
  2) html_output/orb_chart_v8_5.html   K 线图 (Lightweight Charts)

用法: cd ORB_strategy && ../.venv/bin/python orb_backtes_v8_5.py
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

# ===========================================================================
# ★ 参数开关区 —— 想调策略直接改这里, 无需动下方策略代码 ★
# ===========================================================================

# ---- 数据源 ----
DATA_PATH = "nq_5min_rth.parquet"                  # 回测数据 (RTH 9:30-16:00)
RANGE_DATA_PATH = "nq_5min_rth.parquet"            # 区间数据源 (v8.5 区间 9:30-9:50 在 RTH 内)
START_DATE = "2016-01-01"                          # 样本窗口起
END_DATE = "2026-08-30"                            # 样本窗口止

# ---- 合约 / 基础设施 (一般不用改) ----
INSTRUMENT_ID = "NQ.GLBX"                          # 合成"连续 NQ"合约
VENUE = "GLBX"
MULTIPLIER = 20.0                                  # NQ 点值 $20/点
TICK = 0.25
PRICE_PRECISION = 2

# ---- 区间 & 入场窗口 ----
ET = zoneinfo.ZoneInfo("America/New_York")
T_RANGE_START = dtime(9, 30)                       # 区间开始 9:30 (开盘)
T_RANGE_END   = dtime(9, 50)                       # 区间结束 9:50 (开盘后 20 分钟, 前 4 根 5min K 线)
T_WIN_START   = dtime(9, 50)                       # 入场窗口开始 9:50
T_WIN_END     = dtime(11, 10)                      # 入场窗口结束 10:10 (后 4 根 5min K 线, 无突破则放弃)
T_EOD         = dtime(15, 55)                      # 收盘平仓

# ---- 止损 (论文方案) ----
ATR_PERIOD = 14                                    # ATR 周期
ATR_STOP_FRACTION = 0.05                           # 止损 = 5% × 14日ATR

# ---- 仓位 ----
STARTING_CAPITAL = 50_000                          # 起始资金(美元)
RISK_PER_TRADE = 0.01                              # 每笔风险 = 权益的 1% (复利)
MAX_QTY = 4000                                     # 单笔最大手数上限

# ---- 止盈 / 保本 ----
BE_R_MULTIPLE = 10.0                               # 浮盈达 10R → 拉止损到保本
BE_BUFFER_TICKS = 2                                # 保本缓冲 = 2 tick = 0.50pt (点差 0.25 + 手续费 ~0.05)

# ---- 成本 ----
COMMISSION_PER_CONTRACT = 0.5                      # 手续费 $/手/边
SLIPPAGE = 0                                       # 滑点(0=无)

LWC_CDN = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"

# 策略简要描述 (末尾打印, f-string 自动引用顶部参数)
STRATEGY_DESC = f"""\
版本       : v8.5
标的       : NQ 期货 (5分钟 RTH)
时段       : {START_DATE} ~ {END_DATE}
区间       : {T_RANGE_START:%H:%M}-{T_RANGE_END:%H:%M} 开盘后 20 分钟 (前 4 根 5min K 线高低价)
入场       : 窗口 {T_WIN_START:%H:%M}-{T_WIN_END:%H:%M}, 逐根 K 线收盘价判断突破
             收盘 > 区间高点 → 做多
             收盘 < 区间低点 → 做空
             区间内等下一根; {T_WIN_END:%H:%M} 前 4 根无突破 → 当日放弃交易
止损       : {ATR_STOP_FRACTION:.0%} × {ATR_PERIOD}日ATR (前一日, 无未来函数)
止盈       : 无 (持有至收盘平仓, 不封顶)
保本       : 浮盈达 {BE_R_MULTIPLE:.0f}R → 拉止损到「保本 + {BE_BUFFER_TICKS} tick」, 之后持有到收盘
仓位       : floor(equity × {RISK_PER_TRADE:.0%} / (止损点数 × $20)), 单笔最大 {MAX_QTY} 手
手续费     : ${COMMISSION_PER_CONTRACT}/手/边, 无滑点"""

def tick_round(px: float) -> float:
    """把价格/点数取整到最小变动价位 0.25 的倍数。"""
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


# ---------------------------------------------------------------------------
# 区间: 每日 (high, low), 从 RANGE_DATA_PATH 计算 (v8.5: 9:30-9:50)
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
    atr_stop_fraction: float = 0.05
    max_qty: int = 4000
    be_r_multiple: float = 10.0               # 浮盈达 10R → 拉保本
    be_buffer_ticks: int = 2                  # 保本缓冲 tick 数


class OrbStrategy(Strategy):
    def __init__(self, config: OrbStrategyConfig, atr_map: dict[ddate, float],
                 range_map: dict[ddate, tuple[float, float]]):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.risk_per_trade = config.risk_per_trade
        self.multiplier = config.multiplier
        self.atr_stop_fraction = config.atr_stop_fraction
        self.max_qty = config.max_qty
        self.be_r_multiple = config.be_r_multiple
        self.be_buffer_ticks = config.be_buffer_ticks
        self.atr_map = atr_map
        self.range_map = range_map
        self.pending_entry = {}               # 入场单 cid -> 止损距离(点)
        self._trade = None                    # 当前持仓状态 dict (side/qty/r/entry_px/stop_order/stop_moved)
        self._cur_date = None                 # 当前交易日
        self.entered_today = False            # 当日是否已入场
        self.n_entries = 0                    # 统计入场次数
        self.n_no_trade = 0                   # 当日无交易的天数
        self.n_capped = 0                     # 被最大手数上限压制(qty被砍)的天数
        self.n_be_moves = 0                   # 浮盈达 10R → 拉到保本 的次数
        self.n_stopped = 0                    # 初始止损出场次数
        self.n_be_exits = 0                   # 保本止损出场次数
        self.n_eod = 0                        # 收盘平仓次数

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

        # 入场窗口内 (9:50-10:10): 逐根 K 线收盘价判断突破
        if T_WIN_START <= t < T_WIN_END and not self.entered_today:
            rng = self.range_map.get(d)
            if rng is not None:
                rng_high, rng_low = rng
                close = bar.close.as_double()
                if close > rng_high:
                    self._enter(OrderSide.BUY, bar, d)
                elif close < rng_low:
                    self._enter(OrderSide.SELL, bar, d)
                # 收盘在区间内 → 等下一根, 10:10 前无方向则不交易

        # 持仓中: 浮盈达 10R → 拉止损到保本 (收盘平仓那根 K 线之前)
        if t < T_EOD:
            self._check_be(bar)

        # 收盘: 平仓 + 统计未交易日
        if t == T_EOD:
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)
            if self._trade is not None:
                self.n_eod += 1
                self._trade = None
            if not self.entered_today:
                self.n_no_trade += 1

    def _enter(self, side: OrderSide, bar: Bar, d: ddate):
        # 止损距离 = 5% × 前一日 14 日 ATR (至少 1 个 tick)
        atr = self.atr_map.get(d)
        if atr is None or atr <= 0:
            return
        stop_dist = max(TICK, tick_round(self.atr_stop_fraction * atr))

        # 入场价 = 第一根 K 线收盘价
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

    def _check_be(self, bar: Bar):
        """浮盈达 10R (触及判断) → 把止损拉到保本 + 缓冲。"""
        trade = self._trade
        if trade is None or trade["stop_moved"]:
            return
        if trade["stop_order"] is None or not trade["stop_order"].is_open:
            return

        r = trade["r"]
        entry_px = trade["entry_px"]
        if trade["side"] == OrderSide.BUY:
            if bar.high.as_double() >= entry_px + self.be_r_multiple * r:
                self._move_stop_to_be()
        else:
            if bar.low.as_double() <= entry_px - self.be_r_multiple * r:
                self._move_stop_to_be()

    def _move_stop_to_be(self):
        trade = self._trade
        buffer_pts = self.be_buffer_ticks * TICK
        be_px = tick_round(trade["entry_px"] + buffer_pts) if trade["side"] == OrderSide.BUY \
            else tick_round(trade["entry_px"] - buffer_pts)

        self.modify_order(
            trade["stop_order"],
            trigger_price=Price.from_str(f"{be_px:.2f}"),
        )
        trade["stop_moved"] = True
        self.n_be_moves += 1
        self.log.info(
            f"浮盈达 {self.be_r_multiple:.0f}R → 止损移到 保本+{buffer_pts:.2f}pt = {be_px:.2f}"
        )

    def on_order_filled(self, event):
        cid = event.client_order_id

        # 入场单成交 → 只挂初始止损 (无止盈, 持有至收盘)
        if cid in self.pending_entry:
            actual_dist = self.pending_entry.pop(cid)
            self.n_entries += 1

            entry_px = event.last_px.as_double()
            qty = event.last_qty
            side = event.order_side
            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY

            stop = tick_round(entry_px - actual_dist) if side == OrderSide.BUY \
                else tick_round(entry_px + actual_dist)

            sl = self.order_factory.stop_market(
                instrument_id=self.instrument_id,
                order_side=exit_side,
                quantity=qty,
                trigger_price=Price.from_str(f"{stop:.2f}"),
                reduce_only=True,
            )
            self.submit_order(sl)
            self._trade = {
                "side": side,
                "qty": qty,
                "r": actual_dist,
                "entry_px": entry_px,
                "stop_order": sl,
                "stop_moved": False,
            }
            self.log.info(
                f"入场 {side.name} qty={qty} px={entry_px:.2f} "
                f"止损={stop:.2f} (-{actual_dist:.2f}pt) 持有至收盘, 达 {self.be_r_multiple:.0f}R 拉保本"
            )
            return

        # 出场单成交 (止损 / 保本止损)
        trade = self._trade
        if trade is None:
            return
        if trade["stop_order"] is not None and cid == trade["stop_order"].client_order_id:
            if trade["stop_moved"]:
                self.n_be_exits += 1
            else:
                self.n_stopped += 1
            self._trade = None


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
            "version": "v8.5",
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
<title>NQ ORB 回测图表 (v8.5)</title>
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
  <b>NQ 5m ORB · v8.5</b><br>
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

    print("[1/5] 构建 9:30-9:50 区间映射 ...", flush=True)
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
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("ORB-BT-085")))
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
        atr_stop_fraction=ATR_STOP_FRACTION,
        max_qty=MAX_QTY,
        be_r_multiple=BE_R_MULTIPLE,
        be_buffer_ticks=BE_BUFFER_TICKS,
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
    print(f"===== 浮盈达 10R → 拉保本: {strategy.n_be_moves:,} 次 =====")
    print(f"===== 出场分布: 初始止损 {strategy.n_stopped:,}  |  保本止损 {strategy.n_be_exits:,}  |  收盘平仓 {strategy.n_eod:,} =====")
    print(f"===== 当日无交易(收盘在区间内): {strategy.n_no_trade:,} 天 =====")
    print(f"===== 被最大手数上限压制(qty被砍): {strategy.n_capped:,} 天 =====")
    print("===== 账户报告(首/尾) =====")
    print(acct.iloc[[0, -1]].to_string())
    print(f"\n最终权益: ${final_total:,.2f}  "
          f"总盈亏: ${final_total - STARTING_CAPITAL:,.2f}  "
          f"收益率: {(final_total / STARTING_CAPITAL - 1) * 100:,.1f}%")
    print(f"回测年限: {years:.2f} 年   年化收益率: {annual * 100:,.1f}%")
    print(f"峰值权益: ${daily.max():,.0f}   最大回撤: {mdd * 100:.1f}%")

    # ---- 生成两张图表 ----
    report_path = "html_output/orb_report_v8_5.html"
    create_tearsheet(engine, output_path=report_path, title="NQ 5min ORB v8.5 回测报告 (2016-2026, 区间9:30-9:50 + 10R拉保本)")

    trades = extract_trades(engine)
    bar_list = extract_bars(bars)
    chart_path = "html_output/orb_chart_v8_5.html"
    with open(chart_path, "w", encoding="utf-8") as f:
        f.write(gen_chart_html(bar_list, trades))

    print(f"\n已生成两张图表:")
    print(f"  {report_path}  ({os.path.getsize(report_path)/1e6:.1f} MB)  ← 统计报告")
    print(f"  {chart_path}  ({os.path.getsize(chart_path)/1e6:.1f} MB)  ← K线图")

    # ---- 策略简要描述 ----
    print(f"\n===== 策略描述 =====\n{STRATEGY_DESC}")
