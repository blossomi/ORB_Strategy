# -*- coding: utf-8 -*-
"""
orb_core_v84.py  (GLM_working)
==============================
NQ 5 分钟 ORB 策略 —— 参数搜索核心模块 (v8.4 逻辑, 三参数化)。

与 ORB_strategy/orb_backtes_v8_6.py 的关系:
  - 策略逻辑零改动复制自 v8_6 (即 v8.4 逻辑: 收盘价判突破 / ATR止损 / 浮盈达 N R 拉保本 /
    部分成交按累计数量挂止损 / 节假日按当日实际最后一根K线收盘), 仅做以下完善:
    1) 止损分数 atr_stop_fraction 从「锁死 0.075」改为 run_backtest() 的搜索变量;
    2) 成本补上滑点: 每手每边 = $0.5 手续费 + SLIPPAGE_TICKS × tick × 乘数 (线性折算,
       与 v8_4 主脚本同一口径; v8_6 搜索版只算了手续费, 违反 .hermes.md 硬规则 6);
    3) 补 n_cant_afford 统计 (硬规则 2: 任何回测先检查每笔能否买得起 1 手);
    4) 指标补 Sortino / 胜率 / PF / Calmar (右偏长尾策略降权 Sharpe, 看 Sortino/PF);
    5) 去掉 TP / trailing 代码路径 (v8.4 当前配置本就锁死关闭)。
  - 路径自适应: 数据从 ../ORB_strategy 读, 不依赖 cwd。

搜索变量 (Stage A/B/C 由外部脚本驱动):
    atr_stop_fraction : 止损 = frac × 14日ATR (前一日, Wilder, 无未来函数)
    be_r_multiple     : 浮盈达 N R 拉保本 (None → 不拉保本, 纯持有到收盘)
    risk_per_trade    : 每笔风险占权益比例 (复利)

锁死参数 (v8.4 标准):
    NQ $20/点, tick 0.25, 区间 9:00-9:29(ETH), 入场 9:30-10:10 收盘价判断,
    BE 缓冲 0 tick, 无止盈, MAX_QTY 99999 (仅防极端, 记录 n_capped)。
"""
from datetime import time as dtime
from datetime import date as ddate
from math import floor, sqrt
from pathlib import Path

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

# ---------------------------------------------------------------------------
# 路径与锁死参数
# ---------------------------------------------------------------------------
_ORB_DIR = Path(__file__).resolve().parent.parent / "ORB_strategy"
DATA_PATH = str(_ORB_DIR / "nq_5min_rth.parquet")
RANGE_DATA_PATH = str(_ORB_DIR / "nq_5min_eth.parquet")

INSTRUMENT_ID = "NQ.GLBX"
VENUE = "GLBX"
MULTIPLIER = 20.0          # NQ $20/点
TICK = 0.25
PRICE_PRECISION = 2

ET = zoneinfo.ZoneInfo("America/New_York")
T_RANGE_START = dtime(9, 0)     # 盘前区间 9:00-9:29 (ETH 数据)
T_RANGE_END = dtime(9, 29)
T_WIN_START = dtime(9, 30)      # 入场窗口 9:30-10:10
T_WIN_END = dtime(10, 10)

ATR_PERIOD = 14
BE_BUFFER_TICKS = 0
MAX_QTY = 99999
COMMISSION_PER_CONTRACT = 0.5
SLIPPAGE_TICKS = 2              # 每手每边滑点 (tick), 折算进成本; NQ: 2×0.25×$20 = $10/手/边
FEE_PER_SIDE = COMMISSION_PER_CONTRACT + SLIPPAGE_TICKS * TICK * MULTIPLIER

DEFAULT_CAPITAL = 250_000       # 搜索用本金: 保证最宽止损(7.5%ATR×2026波动)×最低风险(0.3%)也买得起 1 手


def configure(multiplier: float = 20.0, slippage_ticks: float = 2.0,
              capital: float = 250_000) -> None:
    """切换成本/合约口径 (worker 进程内在 build_data 之前调用)。

    小本金场景应配 MNQ 乘数 2.0: NQ 标准合约在高波动年 (止损 15~35pt × $20) 每手风险
    $300~700, $25k × 0.7% = $175 买不起 1 手, 样本出现大面积缺口 (硬规则 2)。
    注意 R 口径下的摩擦近似等价: MNQ 1 tick ≈ NQ 2 tick
    (每边摩擦/R = [佣金 + 滑点tick×0.25×乘数] / (止损pt×乘数):
      MNQ 1tick = 1.0/(2×stop) = 0.50/stop;  NQ 2tick = 10.5/(20×stop) = 0.525/stop)。
    """
    global MULTIPLIER, SLIPPAGE_TICKS, FEE_PER_SIDE, DEFAULT_CAPITAL
    MULTIPLIER = float(multiplier)
    SLIPPAGE_TICKS = float(slippage_ticks)
    FEE_PER_SIDE = COMMISSION_PER_CONTRACT + SLIPPAGE_TICKS * TICK * MULTIPLIER
    DEFAULT_CAPITAL = float(capital)


def tick_round(px: float) -> float:
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


# ---------------------------------------------------------------------------
# 映射构建 (与 v8_6 相同: 读全量数据, ATR 从 2010 预热)
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
        [day["high"] - day["low"],
         (day["high"] - prev_close).abs(),
         (day["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()   # Wilder
    atr_use = atr.shift(1)                                      # 前一日, 无未来函数
    return {d.date(): float(v) for d, v in atr_use.dropna().items()}


def build_day_last_bar_map() -> dict[ddate, dtime]:
    df = pd.read_parquet(DATA_PATH).tz_convert(ET)
    ts = df.index.to_series()
    last = ts.groupby(ts.dt.normalize()).max()
    return {ts_et.date(): ts_et.time() for ts_et in last}


def build_bars_and_instrument(start: str, end: str):
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


def build_data(start: str, end: str) -> dict:
    """一个窗口的完整数据包 (maps + bars), 同窗口多次 run_backtest 复用。"""
    return dict(
        start=start, end=end,
        atr_map=build_atr_map(),
        range_map=build_range_map(),
        day_last_bar=build_day_last_bar_map(),
        bars=None, instrument=None, bar_type=None,   # bars 惰性构建 (见 ensure_bars)
    )


def ensure_bars(data: dict) -> dict:
    """惰性构建 bars + instrument (构建耗时, 只做一次)。"""
    if data["bars"] is None:
        bars, instrument, bar_type = build_bars_and_instrument(data["start"], data["end"])
        data["bars"], data["instrument"], data["bar_type"] = bars, instrument, bar_type
    return data


# ---------------------------------------------------------------------------
# 策略 (v8.4 逻辑, 止损/保本/风险 参数化)
# ---------------------------------------------------------------------------
class OrbStrategyConfig(StrategyConfig):
    # 全部必填 (对齐 6f64902 的修复: 不留默认值, 漏传直接报错)
    instrument_id: str
    bar_type: str
    risk_per_trade: float
    multiplier: float
    atr_stop_fraction: float
    max_qty: int
    be_r_multiple: float
    be_buffer_ticks: int


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
        self.cant_afford_today = False
        # 统计
        self.n_entries = 0
        self.n_no_trade = 0
        self.n_cant_afford = 0
        self.n_capped = 0
        self.n_be_moves = 0
        self.n_stopped = 0
        self.n_be_exits = 0
        self.n_eod = 0

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
            self.cant_afford_today = False

        # 入场窗口内: 逐根 K 线收盘价判断突破 (v8.3 修复: 不用 high/low 触及)
        if T_WIN_START <= t < T_WIN_END and not self.entered_today:
            rng = self.range_map.get(d)
            if rng is not None:
                rng_high, rng_low = rng
                close = bar.close.as_double()
                if close > rng_high:
                    self._enter(OrderSide.BUY, bar, d)
                elif close < rng_low:
                    self._enter(OrderSide.SELL, bar, d)

        # 持仓中: 浮盈达 N R 拉保本
        last = self.day_last_bar.get(d)
        if last is not None and t < last:
            self._check_be(bar)

        # 收盘: 按当日实际最后一根 K 线平仓 (半日市防跨夜)
        if last is not None and t == last:
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)
            if self._trade is not None:
                self.n_eod += 1
                self._trade = None
            if not self.entered_today:
                if self.cant_afford_today:
                    self.n_cant_afford += 1
                else:
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
            self.cant_afford_today = True
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

    def on_order_filled(self, event):
        cid = event.client_order_id

        # 入场单成交 (含部分成交): 按「累计已成交数量」挂/调止损 —— v8.4 2026-09-03 修复
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
                    "stop_order": None, "stop_moved": False,
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
            else:
                self.modify_order(trade["stop_order"],
                                  quantity=Quantity.from_str(str(total_qty)))
            return

        # 出场单成交 (初始止损 / 保本止损)
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
def money_float(x) -> float:
    s = str(x).replace(",", "")
    for tok in s.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 0.0


def compute_metrics(engine, venue, capital: float, start: str, end: str) -> dict:
    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())
    final = float(daily.iloc[-1])

    ret = daily.pct_change().dropna()
    r = ret.to_numpy()
    sharpe = float(r.mean() / r.std() * sqrt(252)) if len(r) > 1 and r.std() > 0 else 0.0
    downside = np_min0(r)
    dstd = float(np_sqrt(np_mean(downside ** 2))) if len(r) > 1 else 0.0
    sortino = float(r.mean() / dstd * sqrt(252)) if dstd > 0 else 0.0

    years = (pd.Timestamp(end, tz=ET) - pd.Timestamp(start, tz=ET)).days / 365.25
    annual = (final / capital) ** (1.0 / years) - 1.0 if years > 0 and final > 0 else 0.0
    calmar = annual / abs(mdd) if mdd < 0 else 0.0
    return dict(final_equity=final, annual=annual, mdd=mdd, sharpe=sharpe,
                sortino=sortino, calmar=calmar, years=years)


def trade_stats(engine) -> dict:
    """胜率 / PF / 每笔均盈亏 (金额口径)。"""
    pos = engine.trader.generate_positions_report()
    pnls = [money_float(p["realized_pnl"])
            for _, p in pos.iterrows() if p["ts_closed"] is not None]
    if not pnls:
        return dict(n_trades=0, winrate=0.0, pf=0.0)
    import numpy as _np
    a = _np.array(pnls)
    wins = a[a > 0].sum()
    losses = abs(a[a <= 0].sum())
    pf = float(wins / losses) if losses > 0 else float("inf")
    return dict(n_trades=int(len(a)), winrate=float((a > 0).mean()), pf=pf)


# 小工具 (避免模块顶部 import numpy 之外的耦合)
def np_min0(a):
    import numpy as _np
    return _np.minimum(a, 0.0)


def np_mean(a):
    import numpy as _np
    return _np.mean(a)


def np_sqrt(x):
    import numpy as _np
    return _np.sqrt(x)


# ---------------------------------------------------------------------------
# 回测执行
# ---------------------------------------------------------------------------
def run_backtest(stop_frac: float, be_r, risk_per_trade: float, data: dict,
                 capital: float = None) -> dict:
    """在 data 窗口上跑一次回测。

    stop_frac      : 止损 = frac × 前一日 14日ATR
    be_r           : 浮盈达 N R 拉保本; None → 不拉保本 (纯持有到收盘)
    risk_per_trade : 每笔风险占权益比
    成本: $0.5/手/边 手续费 + SLIPPAGE_TICKS×tick×乘数 滑点 (线性折算)。
    """
    ensure_bars(data)
    capital = capital or DEFAULT_CAPITAL
    be_r_eff = 1e9 if be_r is None else float(be_r)

    venue = Venue(VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-SRCH"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(capital, USD)],
                     fee_model=PerContractFeeModel(Money(FEE_PER_SIDE, USD)))
    engine.add_instrument(data["instrument"])
    engine.add_data(data["bars"])

    cfg = OrbStrategyConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=str(data["bar_type"]),
        risk_per_trade=risk_per_trade,
        multiplier=MULTIPLIER,
        atr_stop_fraction=stop_frac,
        max_qty=MAX_QTY,
        be_r_multiple=be_r_eff,
        be_buffer_ticks=BE_BUFFER_TICKS,
    )
    strat = OrbStrategy(cfg, data["atr_map"], data["range_map"], data["day_last_bar"])
    engine.add_strategy(strat)
    engine.run()

    m = compute_metrics(engine, venue, capital, data["start"], data["end"])
    m.update(trade_stats(engine))
    m.update(
        stop_frac=stop_frac, be_r=(None if be_r is None else float(be_r)),
        risk_per_trade=risk_per_trade, capital=capital,
        n_entries=strat.n_entries, n_stopped=strat.n_stopped,
        n_be_exits=strat.n_be_exits, n_eod=strat.n_eod,
        n_be_moves=strat.n_be_moves, n_capped=strat.n_capped,
        n_cant_afford=strat.n_cant_afford, n_no_trade=strat.n_no_trade,
    )
    engine.reset()
    return m


# ---------------------------------------------------------------------------
# 命令行: 单组合校准/调试  (python orb_core_v84.py --stop 0.075 --be 5 --risk 0.007)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import json
    import time as _time

    p = argparse.ArgumentParser(description="ORB v8.4 三参数单组合回测")
    p.add_argument("--stop", type=float, default=0.075, help="止损分数 (0.075=7.5%%×14日ATR)")
    p.add_argument("--be", type=float, default=None, help="保本倍数 R (不填=不拉保本)")
    p.add_argument("--risk", type=float, default=0.007, help="每笔风险 (0.007=0.7%%)")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end", default="2026-08-30")
    p.add_argument("--capital", type=float, default=DEFAULT_CAPITAL)
    args = p.parse_args()

    t0 = _time.time()
    data = build_data(args.start, args.end)
    t1 = _time.time()
    ensure_bars(data)
    t2 = _time.time()
    r = run_backtest(args.stop, args.be, args.risk, data, args.capital)
    t3 = _time.time()

    print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                      for k, v in r.items()}, ensure_ascii=False, indent=2))
    print(f"\n[计时] 数据映射 {t1-t0:.1f}s | bars构建 {t2-t1:.1f}s ({len(data['bars']):,} 根) "
          f"| 回测 {t3-t2:.1f}s | 总计 {t3-t0:.1f}s")
    print(f"[成本口径] ${FEE_PER_SIDE}/手/边 = 手续费 ${COMMISSION_PER_CONTRACT} "
          f"+ 滑点 {SLIPPAGE_TICKS} tick")
