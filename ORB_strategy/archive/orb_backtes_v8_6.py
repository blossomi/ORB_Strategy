# -*- coding: utf-8 -*-
"""
orb_backtest_v8_6.py
====================
NQ 5 分钟 ORB 策略 —— 参数搜索专用版 (v8.6)。

与 v8.4 的关系: 策略逻辑**零改动**地从 v8.4 复制 (入场 9:30-10:10 收盘价判断、
初始止损、浮盈达 N R 拉保本、部分成交修复、节假日收盘), 仅做两件事:
  1) 去掉图表生成 (tearsheet / Lightweight Charts), 只保留「跑回测 → 出指标」。
  2) 止损距离固定 0.075 × 14日ATR 不变; 把两个搜索变量 `RISK_PER_TRADE` (0.3%-0.8%)
     与 `BE_R_MULTIPLE` (1R-5R) 改成 `run_backtest()` 的入参; 其余参数锁死为 v8.4 标准。

本文件不独立跑全量 (但支持 `python orb_backtes_v8_6.py --risk 0.007 --be 3`
单组合调试); 参数搜索由 heatmap_v8_6.py (单窗口) 与 walk_forward.py (滚动) 驱动。

★ 参数网格 (6 x 5 = 30 组合):
   RISK_FRACS = [0.003, 0.004, 0.005, 0.006, 0.007, 0.008]   # 仓位风险 0.3%-0.8%
   BE_RS      = [1, 2, 3, 4, 5]                              # 浮盈达 N R 拉保本

用法:
   cd ORB_strategy && ../.venv/bin/python orb_backtes_v8_6.py --risk 0.007 --be 3
"""
from datetime import time as dtime
from datetime import date as ddate
from math import floor, sqrt

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
# ★ 锁死参数 (v8.4 标准, 与 notebook 记录一致) ★
# ===========================================================================
SYMBOL = "NQ"
DATA_PATH = "nq_5min_rth.parquet"                 # 回测数据 (RTH 9:30-16:00)
RANGE_DATA_PATH = "nq_5min_eth.parquet"           # 区间数据源 (盘前 9:00-9:30)

INSTRUMENT_ID = "NQ.GLBX"
VENUE = "GLBX"
MULTIPLIER = 20.0                                  # NQ $20/点
TICK = 0.25
PRICE_PRECISION = 2

ET = zoneinfo.ZoneInfo("America/New_York")
T_RANGE_START = dtime(9, 0)                        # 区间 9:00-9:29
T_RANGE_END = dtime(9, 29)
T_WIN_START = dtime(9, 30)                         # 入场窗口 9:30-10:10
T_WIN_END = dtime(10, 10)

TP_R_MULTIPLE = 0.0                                # 无止盈 (持有到收盘)
BE_BUFFER_TICKS = 0                                # 保本缓冲 0 tick
TRAIL_ACTIVATE_R = 0.0                             # 移动止盈关闭
TRAIL_DISTANCE_R = 0.0
ATR_PERIOD = 14                                    # ATR 周期
ATR_STOP_FRACTION = 0.075                          # 止损距离固定 7.5% × 14日ATR (不再搜索)
STARTING_CAPITAL = 25000                          # 起始资金 (低风险 0.3%-0.8% 需大本金才能买得起手数)
RISK_PER_TRADE = 0.007                             # 每笔风险默认 0.7% (搜索变量 1)
MAX_QTY = 99999                                     # 单笔最大手数
COMMISSION_PER_CONTRACT = 0.5                      # 手续费 $/手/边

# ---- 参数网格 (搜索范围) ----
RISK_FRACS = [0.003, 0.004, 0.005, 0.006, 0.007, 0.008]   # 仓位风险 0.3%-0.8%
BE_RS = [1, 2, 3, 4, 5, 6]                            # 保本倍数 1R-5R


def tick_round(px: float) -> float:
    """把价格/点数取整到最小变动价位 0.25 的倍数。"""
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


# ---------------------------------------------------------------------------
# 每日区间 / ATR / 收盘时间映射 (读全量数据, 不按窗口过滤; ATR 从 2010 预热充分)
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
    atr_use = atr.shift(1)                                     # 前一日 ATR, 无未来函数
    return {d.date(): float(v) for d, v in atr_use.dropna().items()}


def build_day_last_bar_map() -> dict[ddate, dtime]:
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    ts = df.index.to_series()
    last = ts.groupby(ts.dt.normalize()).max()
    return {ts_et.date(): ts_et.time() for ts_et in last}


# ---------------------------------------------------------------------------
# 数据: 按窗口过滤 → Nautilus Bar + 合成连续合约
# ---------------------------------------------------------------------------
def build_bars_and_instrument(start: str, end: str) -> tuple[list[Bar], FuturesContract, BarType]:
    df = pd.read_parquet(DATA_PATH)
    df = df.tz_convert(ET)
    s = pd.Timestamp(start, tz=ET)
    e = pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)
    df = df[(df.index >= s) & (df.index < e)]
    df = df.tz_convert("UTC")
    df = df.sort_index()

    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    first_ns = dt_to_unix_nanos(df.index[0])
    last_ns = dt_to_unix_nanos(df.index[-1])

    instrument = FuturesContract(
        instrument_id=instrument_id,
        raw_symbol=Symbol(SYMBOL.upper()),
        asset_class=AssetClass.INDEX,
        currency=USD,
        price_precision=PRICE_PRECISION,
        price_increment=Price.from_str(f"{TICK:.2f}"),
        multiplier=Quantity.from_str(f"{MULTIPLIER:.2f}"),
        lot_size=Quantity.from_str("1"),
        underlying=SYMBOL.upper(),
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


def build_data(start: str, end: str) -> dict:
    """构建一个窗口的完整数据包 (maps + bars), 供同一窗口内多次 run_backtest 复用。"""
    atr_map = build_atr_map()
    range_map = build_range_map()
    day_last_bar = build_day_last_bar_map()
    bars, instrument, bar_type = build_bars_and_instrument(start, end)
    return dict(
        start=start, end=end,
        atr_map=atr_map, range_map=range_map, day_last_bar=day_last_bar,
        bars=bars, instrument=instrument, bar_type=bar_type,
    )


# ---------------------------------------------------------------------------
# 策略 (逻辑零改动复制自 v8.4)
# ---------------------------------------------------------------------------
class OrbStrategyConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    atr_stop_fraction: float = 0.075           # 固定 7.5% × 14日ATR
    be_r_multiple: float = 5.0                 # ★ 搜索变量 2 (1R-5R)
    risk_per_trade: float = 0.007              # ★ 搜索变量 1 (0.3%-0.8%)
    multiplier: float = 20.0
    max_qty: int = 4000
    tp_r_multiple: float = 0.0
    be_buffer_ticks: int = 2
    trail_activate_r: float = 0.0
    trail_distance_r: float = 0.0


class OrbStrategy(Strategy):
    def __init__(self, config: OrbStrategyConfig, atr_map: dict[ddate, float],
                 range_map: dict[ddate, tuple[float, float]],
                 day_last_bar: dict[ddate, dtime]):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.risk_per_trade = config.risk_per_trade
        self.multiplier = config.multiplier
        self.atr_stop_fraction = config.atr_stop_fraction
        self.max_qty = config.max_qty
        self.tp_r_multiple = config.tp_r_multiple
        self.be_r_multiple = config.be_r_multiple
        self.be_buffer_ticks = config.be_buffer_ticks
        self.trail_activate_r = config.trail_activate_r
        self.trail_distance_r = config.trail_distance_r
        self.atr_map = atr_map
        self.range_map = range_map
        self.day_last_bar = day_last_bar
        self.pending_entry = {}
        self._entry_filled = {}
        self._trade = None
        self._cur_date = None
        self.entered_today = False
        self.n_entries = 0
        self.n_no_trade = 0
        self.n_capped = 0
        self.n_be_moves = 0
        self.n_trail_moves = 0
        self.n_stopped = 0
        self.n_be_exits = 0
        self.n_eod = 0
        self.n_tp_hits = 0

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

        if d != self._cur_date:
            self._cur_date = d
            self.entered_today = False

        # 入场窗口内: 逐根 K 线收盘价判断突破
        if T_WIN_START <= t < T_WIN_END and not self.entered_today:
            rng = self.range_map.get(d)
            if rng is not None:
                rng_high, rng_low = rng
                close = bar.close.as_double()
                if close > rng_high:
                    self._enter(OrderSide.BUY, bar, d)
                elif close < rng_low:
                    self._enter(OrderSide.SELL, bar, d)

        # 持仓中: 出场管理 (移动止盈 / 拉保本)
        last = self.day_last_bar.get(d)
        if last is not None and t < last:
            if self.trail_activate_r > 0:
                self._check_trail(bar)
            else:
                self._check_be(bar)

        # 收盘: 当日实际最后一根 K 线平仓
        if last is not None and t == last:
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)
            if self._trade is not None:
                self.n_eod += 1
                self._trade = None
            if not self.entered_today:
                self.n_no_trade += 1

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

    def _check_trail(self, bar: Bar):
        trade = self._trade
        if trade is None or trade["stop_order"] is None or not trade["stop_order"].is_open:
            return
        r = trade["r"]
        entry_px = trade["entry_px"]
        trail_dist = self.trail_distance_r * r
        if trade["side"] == OrderSide.BUY:
            if not trade["trail_active"]:
                if bar.high.as_double() >= entry_px + self.trail_activate_r * r:
                    trade["trail_active"] = True
                    trade["stop_moved"] = True
            if trade["trail_active"]:
                new_stop = tick_round(bar.high.as_double() - trail_dist)
                if new_stop > trade["stop_px"]:
                    trade["stop_px"] = new_stop
                    self.modify_order(trade["stop_order"], trigger_price=Price.from_str(f"{new_stop:.2f}"))
                    self.n_trail_moves += 1
        else:
            if not trade["trail_active"]:
                if bar.low.as_double() <= entry_px - self.trail_activate_r * r:
                    trade["trail_active"] = True
                    trade["stop_moved"] = True
            if trade["trail_active"]:
                new_stop = tick_round(bar.low.as_double() + trail_dist)
                if new_stop < trade["stop_px"]:
                    trade["stop_px"] = new_stop
                    self.modify_order(trade["stop_order"], trigger_price=Price.from_str(f"{new_stop:.2f}"))
                    self.n_trail_moves += 1

    def _cancel_if_open(self, order):
        if order is not None and order.is_open:
            self.cancel_order(order)

    def on_order_filled(self, event):
        cid = event.client_order_id

        if cid in self.pending_entry or cid in self._entry_filled:
            side = event.order_side
            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            is_first = cid in self.pending_entry
            if is_first:
                actual_dist = self.pending_entry.pop(cid)
                self.n_entries += 1
                self._entry_filled[cid] = 0
                self._trade = {
                    "side": side, "qty": 0, "r": actual_dist,
                    "entry_px": event.last_px.as_double(),
                    "stop_order": None, "tp_order": None,
                    "stop_moved": False, "stop_px": None, "trail_active": False,
                }
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
                    instrument_id=self.instrument_id,
                    order_side=exit_side,
                    quantity=Quantity.from_str(str(total_qty)),
                    trigger_price=Price.from_str(f"{stop:.2f}"),
                    reduce_only=True,
                )
                self.submit_order(sl)
                trade["stop_order"] = sl
                trade["stop_px"] = stop
            else:
                self.modify_order(trade["stop_order"], quantity=Quantity.from_str(str(total_qty)))

            if self.tp_r_multiple > 0:
                if trade["tp_order"] is None:
                    tp_px = tick_round(trade["entry_px"] + self.tp_r_multiple * actual_dist) if side == OrderSide.BUY \
                        else tick_round(trade["entry_px"] - self.tp_r_multiple * actual_dist)
                    tp_order = self.order_factory.limit(
                        instrument_id=self.instrument_id,
                        order_side=exit_side,
                        quantity=Quantity.from_str(str(total_qty)),
                        price=Price.from_str(f"{tp_px:.2f}"),
                        reduce_only=True,
                    )
                    self.submit_order(tp_order)
                    trade["tp_order"] = tp_order
                else:
                    self.modify_order(trade["tp_order"], quantity=Quantity.from_str(str(total_qty)))
            return

        # 出场单成交
        trade = self._trade
        if trade is None:
            return
        if trade["stop_order"] is not None and cid == trade["stop_order"].client_order_id:
            if trade["stop_moved"]:
                self.n_be_exits += 1
            else:
                self.n_stopped += 1
            self._cancel_if_open(trade.get("tp_order"))
            self._trade = None
        elif trade.get("tp_order") is not None and cid == trade["tp_order"].client_order_id:
            self.n_tp_hits += 1
            self._cancel_if_open(trade["stop_order"])
            self._trade = None


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def compute_metrics(engine, venue, capital: float, start: str, end: str) -> dict:
    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())
    ret = daily.pct_change().dropna()
    sharpe = float(ret.mean() / ret.std() * sqrt(252)) if ret.std() > 0 else 0.0
    final = float(eq.iloc[-1])
    years = (pd.Timestamp(end, tz=ET) - pd.Timestamp(start, tz=ET)).days / 365.25
    annual = (final / capital) ** (1.0 / years) - 1.0 if years > 0 and final > 0 else 0.0
    return dict(final_equity=final, annual=annual, mdd=mdd, sharpe=sharpe, years=years)


# ---------------------------------------------------------------------------
# 回测执行
# ---------------------------------------------------------------------------
def run_backtest(risk_per_trade: float, be_r: float, data: dict, capital: float = None) -> dict:
    """对给定 (risk_per_trade, be_r) 在 data 窗口上跑一次回测, 返回指标 dict。

    止损距离固定 ATR_STOP_FRACTION=0.075 (7.5% × 14日ATR)。
    data 由 build_data(start, end) 构建; 同一窗口内多次调用可复用同一 data
    (engine.add_data 会内部拷贝 bars, 不消耗原列表)。
    """
    capital = capital or STARTING_CAPITAL
    venue = Venue(VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-BT-86"), logging=LoggingConfig(log_level="WARNING")))
    engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(capital, USD)],
                     fee_model=PerContractFeeModel(Money(COMMISSION_PER_CONTRACT, USD)))
    engine.add_instrument(data["instrument"])
    engine.add_data(data["bars"])

    cfg = OrbStrategyConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=str(data["bar_type"]),
        atr_stop_fraction=ATR_STOP_FRACTION,
        risk_per_trade=risk_per_trade,
        be_r_multiple=be_r,
        max_qty=MAX_QTY,
        be_buffer_ticks=BE_BUFFER_TICKS,
    )
    strat = OrbStrategy(cfg, data["atr_map"], data["range_map"], data["day_last_bar"])
    engine.add_strategy(strat)
    engine.run()

    m = compute_metrics(engine, venue, capital, data["start"], data["end"])
    m["n_entries"] = strat.n_entries
    return m


def run_single(risk_per_trade: float, be_r: float, start: str, end: str, capital: float = None) -> dict:
    """便捷封装: 构建数据 + 跑一次 (单组合调试用)。"""
    data = build_data(start, end)
    return run_backtest(risk_per_trade, be_r, data, capital)


# ---------------------------------------------------------------------------
# 命令行单跑 (调试)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="ORB v8.6 单组合回测")
    p.add_argument("--risk", type=float, default=0.007, help="仓位风险 (0.007=0.7%%)")
    p.add_argument("--be", type=float, default=3.0, help="保本倍数 (R)")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default="2026-08-30")
    args = p.parse_args()

    r = run_single(args.risk, args.be, args.start, args.end)
    print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()},
                     ensure_ascii=False, indent=2))
