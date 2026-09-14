# -*- coding: utf-8 -*-
"""
orb_backtest_v8_4_1.py
======================
NQ 5 分钟 ORB 策略 —— v8.4.1 (精简 + 滚动测试专用)。

相对 v8.4 的改动 (策略逻辑**零改动**):
  1) 删除无用开关: 标的开关(SYMBOL/ES)、固定止盈(TP_R_MULTIPLE)、移动止盈(TRAIL_*)。
     → 锁死 NQ + 无止盈(持有到收盘) + 无 trailing。
  2) 其余保留 v8.4 逻辑: 9:00-9:29 盘前区间 / 9:30-10:10 收盘价判断突破 /
     5%×14日ATR 止损 / 浮盈达 5R 拉保本 / 部分成交修复 / 节假日收盘。
  3) 指标扩充: 年化 / MDD / Sharpe(252) / Sortino(252) / 胜率 / 盈亏比(PF)。
  4) 优化为滚动测试友好: start/end 参数化 + 内置 6 窗口 Walk-Forward。

用法:
  python orb_backtes_v8_4_1.py                     # 全样本 2016-2026
  python orb_backtes_v8_4_1.py --walkforward       # 6 窗口滚动 (训练5年→测试1年)
  python orb_backtes_v8_4_1.py --start 2023-01-01 --end 2024-01-01
"""
from datetime import time as dtime
from datetime import date as ddate
from math import floor, sqrt

import numpy as np
import pandas as pd
import zoneinfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

# ===========================================================================
# 锁死参数 (NQ, 结论: 5% ATR + 5R 保本)
# ===========================================================================
DATA_PATH = "nq_5min_rth.parquet"
RANGE_DATA_PATH = "nq_5min_eth.parquet"
INSTRUMENT_ID = "NQ.GLBX"
VENUE = "GLBX"
MULTIPLIER = 20.0
TICK = 0.25
PRICE_PRECISION = 2

ET = zoneinfo.ZoneInfo("America/New_York")
T_RANGE_START = dtime(9, 0)
T_RANGE_END = dtime(9, 29)
T_WIN_START = dtime(9, 30)
T_WIN_END = dtime(10, 10)

ATR_PERIOD = 14
ATR_STOP_FRACTION = 0.075          # 5% × 14日ATR
BE_R_MULTIPLE = 4.0               # 浮盈达 5R 拉保本
BE_BUFFER_TICKS = 2               # 保本缓冲 2 tick = 0.50pt

STARTING_CAPITAL = 25000          # 对齐 notebook 的 $5万
RISK_PER_TRADE = 0.005
MAX_QTY = 400
COMMISSION_PER_CONTRACT = 0.5

# 6 个滚动窗口 (训练 5 年 → 测试 1 年, 末段测试只到 2026-08-30)
WINDOWS = [
    ("2016-01-01", "2020-12-31", "2021-01-01", "2021-12-31"),
    ("2017-01-01", "2021-12-31", "2022-01-01", "2022-12-31"),
    ("2018-01-01", "2022-12-31", "2023-01-01", "2023-12-31"),
    ("2019-01-01", "2023-12-31", "2024-01-01", "2024-12-31"),
    ("2020-01-01", "2024-12-31", "2025-01-01", "2025-12-31"),
    ("2021-01-01", "2025-12-31", "2026-01-01", "2026-08-30"),
]


def tick_round(px: float) -> float:
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


def money_float(x) -> float:
    """'1,234.56 USD' / Money → float。"""
    s = str(x).replace(",", "")
    for tok in s.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 0.0


# ---------------------------------------------------------------------------
# 每日区间 / ATR / 收盘时间 (读全量, ATR 从 2010 预热)
# ---------------------------------------------------------------------------
def build_range_map() -> dict[ddate, tuple[float, float]]:
    df = pd.read_parquet(RANGE_DATA_PATH).tz_convert(ET)
    t = df.index.time
    df = df[(t >= T_RANGE_START) & (t < T_RANGE_END)]
    out = {}
    for d, grp in df.groupby(df.index.normalize()):
        out[d.date()] = (float(grp["high"].max()), float(grp["low"].min()))
    return out


def build_atr_map() -> dict[ddate, float]:
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    day = df.resample("1D").agg(high=("high", "max"), low=("low", "min"),
                                close=("close", "last")).dropna()
    prev_close = day["close"].shift(1)
    tr = pd.concat([day["high"] - day["low"], (day["high"] - prev_close).abs(),
                    (day["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()
    atr_use = atr.shift(1)
    return {d.date(): float(v) for d, v in atr_use.dropna().items()}


def build_day_last_bar_map() -> dict[ddate, dtime]:
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    ts = df.index.to_series()
    last = ts.groupby(ts.dt.normalize()).max()
    return {ts_et.date(): ts_et.time() for ts_et in last}


def build_bars_and_instrument(start: str, end: str):
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    s = pd.Timestamp(start, tz=ET)
    e = pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)
    df = df[(df.index >= s) & (df.index < e)]
    df = df.tz_convert("UTC").sort_index()

    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    first_ns = dt_to_unix_nanos(df.index[0])
    last_ns = dt_to_unix_nanos(df.index[-1])
    instrument = FuturesContract(
        instrument_id=instrument_id, raw_symbol=Symbol("NQ"), asset_class=AssetClass.INDEX,
        currency=USD, price_precision=PRICE_PRECISION,
        price_increment=Price.from_str(f"{TICK:.2f}"),
        multiplier=Quantity.from_str(f"{MULTIPLIER:.2f}"), lot_size=Quantity.from_str("1"),
        underlying="NQ", activation_ns=first_ns - 86_400_000_000_000,
        expiration_ns=last_ns + 3_652_000_000_000_000, ts_event=first_ns, ts_init=first_ns,
    )
    bar_type = BarType.from_str(f"{INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL")
    bars = []
    for ts, row in df.iterrows():
        ns = dt_to_unix_nanos(ts)
        bars.append(Bar(bar_type=bar_type, open=Price.from_str(f"{row['open']:.2f}"),
                        high=Price.from_str(f"{row['high']:.2f}"),
                        low=Price.from_str(f"{row['low']:.2f}"),
                        close=Price.from_str(f"{row['close']:.2f}"),
                        volume=Quantity.from_str(str(int(row["volume"]))),
                        ts_event=ns, ts_init=ns))
    return bars, instrument, bar_type


def build_data(start: str, end: str) -> dict:
    atr_map = build_atr_map()
    range_map = build_range_map()
    day_last_bar = build_day_last_bar_map()
    bars, instrument, bar_type = build_bars_and_instrument(start, end)
    return dict(start=start, end=end, atr_map=atr_map, range_map=range_map,
                day_last_bar=day_last_bar, bars=bars, instrument=instrument, bar_type=bar_type)


# ---------------------------------------------------------------------------
# 策略 (v8.4 逻辑, 删 TP/TRAIL)
# ---------------------------------------------------------------------------
class OrbStrategyConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    risk_per_trade: float = 0.01
    multiplier: float = 20.0
    atr_stop_fraction: float = 0.05
    max_qty: int = 4000
    be_r_multiple: float = 5.0
    be_buffer_ticks: int = 2


class OrbStrategy(Strategy):
    def __init__(self, config, atr_map, range_map, day_last_bar):
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
        self.day_last_bar = day_last_bar
        self.pending_entry = {}
        self._entry_filled = {}
        self._trade = None
        self._cur_date = None
        self.entered_today = False
        self.n_entries = 0
        self.n_be_moves = 0
        self.n_stopped = 0
        self.n_be_exits = 0
        self.n_eod = 0

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def _et_time(self, ts_ns):
        return pd.Timestamp(ts_ns, unit="ns", tz="UTC").tz_convert(ET).time()

    def _et_date(self, ts_ns):
        return pd.Timestamp(ts_ns, unit="ns", tz="UTC").tz_convert(ET).date()

    def _equity(self) -> float:
        return self.portfolio.equity(venue=self.instrument_id.venue)[USD].as_double()

    def on_bar(self, bar: Bar):
        t = self._et_time(bar.ts_event)
        d = self._et_date(bar.ts_event)

        if d != self._cur_date:
            self._cur_date = d
            self.entered_today = False

        # 入场窗口: 逐根 K 线收盘价判断突破
        if T_WIN_START <= t < T_WIN_END and not self.entered_today:
            rng = self.range_map.get(d)
            if rng is not None:
                rng_high, rng_low = rng
                close = bar.close.as_double()
                if close > rng_high:
                    self._enter(OrderSide.BUY, bar, d)
                elif close < rng_low:
                    self._enter(OrderSide.SELL, bar, d)

        # 持仓中: 浮盈达 5R 拉保本
        last = self.day_last_bar.get(d)
        if last is not None and t < last:
            self._check_be(bar)

        # 收盘: 当日实际最后一根 K 线平仓
        if last is not None and t == last:
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)
            if self._trade is not None:
                self.n_eod += 1
                self._trade = None

    def _enter(self, side: OrderSide, bar: Bar, d: ddate):
        atr = self.atr_map.get(d)
        if atr is None or atr <= 0:
            return
        stop_dist = max(TICK, tick_round(self.atr_stop_fraction * atr))
        entry = bar.close.as_double()
        stop_price = tick_round(entry - stop_dist) if side == OrderSide.BUY \
            else tick_round(entry + stop_dist)
        actual_dist = abs(entry - stop_price)
        if actual_dist <= 0:
            return

        equity = self._equity()
        risk_qty = equity * self.risk_per_trade / (actual_dist * self.multiplier)
        qty = int(floor(min(risk_qty, self.max_qty)))
        if qty < 1:
            return

        order = self.order_factory.market(instrument_id=self.instrument_id, order_side=side,
                                          quantity=Quantity.from_str(str(qty)))
        self.pending_entry[order.client_order_id] = actual_dist
        self.submit_order(order)
        self.entered_today = True

    def _check_be(self, bar: Bar):
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
        self.modify_order(trade["stop_order"], trigger_price=Price.from_str(f"{be_px:.2f}"))
        trade["stop_moved"] = True
        self.n_be_moves += 1

    def on_order_filled(self, event):
        cid = event.client_order_id

        # 入场单成交 → 按累计已成交数量挂/调止损 (部分成交修复)
        if cid in self.pending_entry or cid in self._entry_filled:
            side = event.order_side
            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            is_first = cid in self.pending_entry
            if is_first:
                actual_dist = self.pending_entry.pop(cid)
                self.n_entries += 1
                self._entry_filled[cid] = 0
                self._trade = {"side": side, "qty": 0, "r": actual_dist,
                               "entry_px": event.last_px.as_double(),
                               "stop_order": None, "stop_moved": False}
            else:
                actual_dist = self._trade["r"]

            self._entry_filled[cid] += int(event.last_qty.as_double())
            total_qty = self._entry_filled[cid]
            trade = self._trade
            trade["qty"] = total_qty

            if trade["stop_order"] is None:
                stop = tick_round(trade["entry_px"] - actual_dist) if side == OrderSide.BUY \
                    else tick_round(trade["entry_px"] + actual_dist)
                sl = self.order_factory.stop_market(
                    instrument_id=self.instrument_id, order_side=exit_side,
                    quantity=Quantity.from_str(str(total_qty)),
                    trigger_price=Price.from_str(f"{stop:.2f}"), reduce_only=True)
                self.submit_order(sl)
                trade["stop_order"] = sl
            else:
                self.modify_order(trade["stop_order"], quantity=Quantity.from_str(str(total_qty)))
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
# 指标
# ---------------------------------------------------------------------------
def compute_metrics(engine, venue, capital, start, end) -> dict:
    # 日权益 → 年化 / MDD / Sharpe / Sortino
    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    ret = daily.pct_change().dropna()

    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())
    final = float(eq.iloc[-1])
    years = (pd.Timestamp(end, tz=ET) - pd.Timestamp(start, tz=ET)).days / 365.25
    annual = (final / capital) ** (1.0 / years) - 1.0 if years > 0 and final > 0 else 0.0

    r = ret.to_numpy()
    sharpe = float(r.mean() / r.std() * sqrt(252)) if r.std() > 0 else 0.0
    downside = np.minimum(r, 0.0)
    dstd = float(np.sqrt(np.mean(downside ** 2)))
    sortino = float(r.mean() / dstd * sqrt(252)) if dstd > 0 else 0.0

    # 每笔盈亏 → 胜率 / PF
    pos = engine.trader.generate_positions_report()
    pnls = np.array([money_float(p["realized_pnl"]) for _, p in pos.iterrows()
                     if p["ts_closed"] is not None])
    n = len(pnls)
    winrate = float((pnls > 0).sum() / n) if n else 0.0
    wins = pnls[pnls > 0].sum()
    losses = abs(pnls[pnls <= 0].sum())
    pf = float(wins / losses) if losses > 0 else float("inf")

    return dict(final_equity=final, peak_equity=float(daily.max()), annual=annual, mdd=mdd,
                sharpe=sharpe, sortino=sortino, winrate=winrate, profit_factor=pf,
                n_entries=n, years=years)


def run_backtest(start: str, end: str, capital: float = None, report_path: str = None) -> dict:
    capital = capital or STARTING_CAPITAL
    data = build_data(start, end)
    venue = Venue(VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-BT-841"), logging=LoggingConfig(log_level="WARNING")))
    engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(capital, USD)],
                     fee_model=PerContractFeeModel(Money(COMMISSION_PER_CONTRACT, USD)))
    engine.add_instrument(data["instrument"])
    engine.add_data(data["bars"])
    cfg = OrbStrategyConfig(instrument_id=INSTRUMENT_ID, bar_type=str(data["bar_type"]))
    strat = OrbStrategy(cfg, data["atr_map"], data["range_map"], data["day_last_bar"])
    engine.add_strategy(strat)
    engine.run()
    m = compute_metrics(engine, venue, capital, start, end)
    m["start"] = start
    m["end"] = end
    m["n_be_moves"] = strat.n_be_moves
    m["n_stopped"] = strat.n_stopped
    m["n_be_exits"] = strat.n_be_exits
    m["n_eod"] = strat.n_eod
    if report_path:
        make_report(engine, venue, capital, m, report_path)
    return m


def make_report(engine, venue, capital, m, out_path):
    """生成可视化报告 PNG: 大权益曲线 + 回撤 + 日收益分布 + 月度热力图。"""
    import os
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.sans-serif"] = ["PingFang SC", "Hiragino Sans GB", "Arial Unicode MS",
                                       "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    ret = daily.pct_change().dropna()
    peak = daily.cummax()
    dd = daily / peak - 1.0

    fig = plt.figure(figsize=(16, 11), dpi=100)
    gs = fig.add_gridspec(3, 2, height_ratios=[3.4, 1.6, 1.9], hspace=0.35, wspace=0.20)

    # ---- 1. 大图: 权益曲线 (占满整行, 高度最高) ----
    ax = fig.add_subplot(gs[0, :])
    ax.plot(daily.index, daily.values / 1e6, color="#26a69a", lw=1.8, label="Equity")
    ax.plot(peak.index, peak.values / 1e6, color="#ffb300", lw=1.0, ls="--", alpha=0.8,
            label="Peak")
    ax.fill_between(daily.index, capital / 1e6, daily.values / 1e6,
                    color="#26a69a", alpha=0.12)
    ax.set_title("Equity Curve (总收益曲线)", fontsize=16, fontweight="bold", pad=10)
    ax.set_ylabel("Equity ($M)")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", frameon=False, fontsize=10)
    box = (f"Final  ${m['final_equity']:,.0f}\n"
           f"Annual {m['annual']*100:.1f}%   MDD {m['mdd']*100:.1f}%\n"
           f"Sharpe {m['sharpe']:.2f}   Sortino {m['sortino']:.2f}")
    ax.text(0.02, 0.97, box, transform=ax.transAxes, va="top", fontsize=10,
            bbox=dict(boxstyle="round,pad=0.4", fc="white", ec="#cccccc", alpha=0.9))

    # ---- 2. 回撤曲线 ----
    ax = fig.add_subplot(gs[1, :])
    ax.fill_between(dd.index, dd.values * 100, 0, color="#ef5350", alpha=0.55)
    ax.plot(dd.index, dd.values * 100, color="#c62828", lw=1.0)
    ax.set_title("Drawdown (回撤)", fontsize=13, fontweight="bold")
    ax.set_ylabel("Drawdown (%)")
    ax.grid(True, alpha=0.25)

    # ---- 3. 日收益分布 ----
    ax = fig.add_subplot(gs[2, 0])
    ax.hist(ret.values * 100, bins=100, color="#42a5f5", alpha=0.75, edgecolor="white",
            linewidth=0.3)
    ax.set_title("Daily Return Distribution (日收益分布)", fontsize=13, fontweight="bold")
    ax.set_xlabel("Daily Return (%)")
    ax.set_ylabel("Count")
    ax.grid(True, alpha=0.25, axis="y")

    # ---- 4. 月度收益热力图 ----
    ax = fig.add_subplot(gs[2, 1])
    monthly = (1 + ret).resample("1ME").apply(lambda x: (1 + x).prod() - 1) * 100
    df_m = pd.DataFrame({"y": monthly.index.year, "m": monthly.index.month, "r": monthly.values})
    pivot = df_m.pivot(index="y", columns="m", values="r")
    im = ax.imshow(pivot.values, cmap="RdYlGn", aspect="auto", vmin=-12, vmax=12)
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    ax.set_xticks(range(12))
    ax.set_xticklabels(months, fontsize=8)
    ax.set_yticks(range(len(pivot.index)))
    ax.set_yticklabels(pivot.index, fontsize=9)
    ax.set_title("Monthly Returns % (月度收益)", fontsize=13, fontweight="bold")
    for i in range(pivot.shape[0]):
        for j in range(pivot.shape[1]):
            v = pivot.values[i, j]
            if not np.isnan(v):
                ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=6.5,
                        color="black" if -6 < v < 6 else "white")
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"\n可视化报告已生成: {out_path}")


def _print_full(m):
    """全样本输出 (参照 v8.4 风格 + 指标扩充 + 美化对齐)。"""
    pf = "∞" if np.isinf(m["profit_factor"]) else f"{m['profit_factor']:.2f}"
    line = "=" * 66
    print()
    print(line)
    print(f"  NQ 5min ORB · v8.4.1    样本 {m['start']} ~ {m['end']}")
    print(line)
    print(f"  入场次数   : {m['n_entries']:,} 笔")
    print(f"  出场分布   : 初始止损 {m['n_stopped']:,}  |  保本止损 {m['n_be_exits']:,}  |  收盘平仓 {m['n_eod']:,}")
    print(f"  拉保本     : 浮盈达 {BE_R_MULTIPLE:.0f}R → {m['n_be_moves']:,} 次")
    print("-" * 66)
    print(f"  最终权益   : ${m['final_equity']:,.0f}")
    print(f"  总盈亏     : ${m['final_equity'] - STARTING_CAPITAL:,.0f}"
          f"    收益率 {(m['final_equity'] / STARTING_CAPITAL - 1) * 100:,.1f}%")
    print(f"  回测年限   : {m['years']:.2f} 年")
    print("-" * 66)
    print(f"  年化收益率 : {m['annual']*100:>8.1f}%    Sharpe(252)  {m['sharpe']:>5.2f}    Sortino(252)  {m['sortino']:>5.2f}")
    print(f"  最大回撤   : {m['mdd']*100:>8.1f}%    胜率 {m['winrate']*100:>5.1f}%          Profit Factor {pf}")
    print(f"  峰值权益   : ${m['peak_equity']:,.0f}")
    print(line)


def run_walkforward():
    print("\n" + "=" * 100)
    print("v8.4.1 滚动 Walk-Forward  (固定 5% ATR + 5R 保本, 训练 5 年 → 测试 1 年)")
    print("=" * 100)
    print(f"{'窗口':>3} | {'训练段':<22} {'测试段':<22} | {'训练年化':>8} {'测试年化':>8} "
          f"{'测试MDD':>8} {'测试Sharpe':>9} {'测试Sortino':>9} {'测试胜率':>8} {'测试PF':>7}")
    print("-" * 100)

    rows = []
    for i, (tr_s, tr_e, te_s, te_e) in enumerate(WINDOWS, 1):
        m_tr = run_backtest(tr_s, tr_e)
        m_te = run_backtest(te_s, te_e)
        rows.append((i, tr_s, te_s, m_tr, m_te))
        pf = "∞" if np.isinf(m_te["profit_factor"]) else f"{m_te['profit_factor']:.2f}"
        print(f"{i:>3} | {tr_s}~{tr_e}  {te_s}~{te_e} | {m_tr['annual']*100:>7.1f}% "
              f"{m_te['annual']*100:>7.1f}% {m_te['mdd']*100:>7.1f}% "
              f"{m_te['sharpe']:>9.2f} {m_te['sortino']:>9.2f} "
              f"{m_te['winrate']*100:>7.1f}% {pf:>7}")

    # 样本外汇总
    ann = [m_te["annual"] for _, _, _, _, m_te in rows]
    mdd = [m_te["mdd"] for _, _, _, _, m_te in rows]
    shp = [m_te["sharpe"] for _, _, _, _, m_te in rows]
    sor = [m_te["sortino"] for _, _, _, _, m_te in rows]
    n_pos = sum(1 for a in ann if a > 0)
    print("\n===== 样本外汇总 (6 段测试) =====")
    print(f"年化: 中位 {np.median(ann)*100:.1f}%  最差 {min(ann)*100:.1f}%  最好 {max(ann)*100:.1f}%  "
          f"| {n_pos}/{len(ann)} 段为正")
    print(f"MDD : 最差 {min(mdd)*100:.1f}%  中位 {np.median(mdd)*100:.1f}%")
    print(f"Sharpe 中位 {np.median(shp):.2f}  |  Sortino 中位 {np.median(sor):.2f}")
    print("\n===== 结论 =====")
    if n_pos == len(ann) and np.median(shp) > 0.5:
        print("✓ 每段样本外都盈利且 Sharpe 中位 > 0.5 → edge 跨窗口稳定, 5%+5R 可外推。")
    elif n_pos >= 4:
        print("△ 多数窗口盈利, 但个别窗口亏损 → edge 存在, 但受市场状态影响, 需风控兜底。")
    else:
        print("✗ 样本外不稳定 → 5%+5R 的全样本表现可能过拟合, 不宜直接实盘。")


def main():
    import argparse
    p = argparse.ArgumentParser(description="ORB v8.4.1")
    p.add_argument("--walkforward", action="store_true")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default="2026-08-30")
    p.add_argument("--report", action="store_true", help="生成可视化报告 PNG")
    p.add_argument("--report-out", default="html_output/v8_4_1_report.png",
                   help="报告输出路径 (默认 html_output/v8_4_1_report.png)")
    args = p.parse_args()

    print(f"起始 ${STARTING_CAPITAL:,}  |  {ATR_STOP_FRACTION*100:.1f}% ATR 止损 + "
          f"{BE_R_MULTIPLE:.0f}R 保本  |  MAX_QTY={MAX_QTY}  |  手续费 ${COMMISSION_PER_CONTRACT}/手/边")

    if args.walkforward:
        run_walkforward()
    else:
        m = run_backtest(args.start, args.end,
                         report_path=args.report_out if args.report else None)
        _print_full(m)


if __name__ == "__main__":
    main()
