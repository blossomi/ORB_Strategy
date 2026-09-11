# -*- coding: utf-8 -*-
"""
orb_backtest_v4.py
==================
NQ 期货 5 分钟 ORB 日内策略 —— NautilusTrader 版本 (v4)

v4: 对齐论文"进一步研究"的最优配置, 做苹果对苹果对比
  1) 时段: 2016-01-01 ~ 2023-02-17 (论文样本窗口)
  2) 止损: 5% × 14日ATR (Wilder, 用前一日 ATR, 无未来函数)
  3) 止盈: 无 (持有至收盘平仓) —— 论文发现"紧止损 + 不封顶"最优
  4) 仓位: floor(equity × 1% / (止损点数 × $20)), 复利当前权益

不变部分:
  - 时段 9:30-16:00 ET (RTH), K1=9:30, K2=9:35
  - 方向: K1 收阳只做多, 收阴只做空, 十字星不交易
  - 入场: K1 收盘价 (≈ K2 开盘), 市价单
  - 收盘平仓, 手续费 $0.5/手/边, 无滑点, 每日最多 1 笔

用法: cd ORB_strategy && ../.venv/bin/python orb_backtes_v4.py
"""
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
DATA_PATH = "nq_5min_rth.parquet"
INSTRUMENT_ID = "NQ.GLBX"                        # 合成"连续 NQ"合约
VENUE = "GLBX"
MULTIPLIER = 20.0                                # NQ 点值 $20/点
TICK = 0.25
PRICE_PRECISION = 2

STARTING_CAPITAL = 50_000                        # 起始资金(美元)
RISK_PER_TRADE = 0.01                            # 每笔风险 = 权益的 1% (复利, 对齐论文)
ATR_PERIOD = 14                                  # ATR 周期
ATR_STOP_FRACTION = 0.05                         # 止损 = 5% × 14日ATR
COMMISSION_PER_CONTRACT = 0.5                    # 手续费 $/手/边
SLIPPAGE = 0                                     # 滑点(0=无)

START_DATE = "2016-01-01"                        # 论文样本窗口起
END_DATE = "2023-02-17"                          # 论文样本窗口止

ET = zoneinfo.ZoneInfo("America/New_York")
T_K1 = dtime(9, 30)                              # 当日第一根 5 分钟 K 线
T_EOD = dtime(15, 55)                            # 当日最后一根 5 分钟 K 线(收盘 16:00)


def tick_round(px: float) -> float:
    """把价格/点数取整到最小变动价位 0.25 的倍数。"""
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


# ---------------------------------------------------------------------------
# ATR: 前一日 14 日 ATR (Wilder), 返回 {ET 交易日 date: atr 点数}
# (在完整数据上计算, 保证 2016 年初有足够的 warmup)
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


class OrbStrategy(Strategy):
    def __init__(self, config: OrbStrategyConfig, atr_map: dict[ddate, float]):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.risk_per_trade = config.risk_per_trade
        self.multiplier = config.multiplier
        self.atr_stop_fraction = config.atr_stop_fraction
        self.atr_map = atr_map
        self.k1 = {}                          # 当日 K1 的 OHLC
        self.pending_entry = {}               # 入场单 cid -> 止损距离(点)
        self.n_entries = 0                    # 统计入场次数
        self.n_skipped = 0                    # 因开不了仓(qty<1)跳过的天数

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

        if t == T_K1:
            self.k1 = {
                "open": bar.open.as_double(),
                "high": bar.high.as_double(),
                "low": bar.low.as_double(),
                "close": bar.close.as_double(),
            }
            self._enter_on_k1(bar)

        elif t == T_EOD:
            # 收盘强制平仓, 并撤销未成交的止损单(避免隔夜挂单)
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)

    def _enter_on_k1(self, bar: Bar):
        k1o, k1c = self.k1["open"], self.k1["close"]

        # 方向判断 (十字星 = 不交易)
        if k1c > k1o:
            side = OrderSide.BUY
        elif k1c < k1o:
            side = OrderSide.SELL
        else:
            return

        # 止损距离 = 5% × 前一日 14 日 ATR (至少 1 个 tick)
        atr = self.atr_map.get(self._et_date(bar.ts_event))
        if atr is None or atr <= 0:
            return
        stop_dist = max(TICK, tick_round(self.atr_stop_fraction * atr))
        entry = k1c
        stop_price = tick_round(entry - stop_dist) if side == OrderSide.BUY \
            else tick_round(entry + stop_dist)
        actual_dist = abs(entry - stop_price)
        if actual_dist <= 0:
            return

        # 复利仓位: floor(equity × 1% / (止损点数 × $20))
        equity = self._equity()
        qty = int(floor(equity * self.risk_per_trade / (actual_dist * self.multiplier)))
        if qty < 1:
            self.n_skipped += 1
            return

        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=Quantity.from_str(str(qty)),
        )
        self.pending_entry[order.client_order_id] = actual_dist
        self.submit_order(order)

    def on_order_filled(self, event):
        cid = event.client_order_id

        # 入场单成交 → 只挂止损 (无止盈, 持有至收盘)
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
            )
            self.submit_order(sl)
            self.log.info(
                f"入场 {side.name} qty={qty} px={entry_px:.2f} "
                f"stop={stop:.2f} ({actual_dist:.2f}pt) 持有至收盘"
            )
            return

        # 止损成交: 仓位已平, 无需额外动作


# ---------------------------------------------------------------------------
# 数据: 读 5 分钟 parquet (过滤到论文窗口) → Nautilus Bar + 合成连续合约
# ---------------------------------------------------------------------------
def build_bars_and_instrument() -> tuple[list[Bar], FuturesContract, BarType]:
    df = pd.read_parquet(DATA_PATH)
    df = df.tz_convert(ET)                    # 先转 ET 过滤日期
    start = pd.Timestamp(START_DATE, tz=ET)
    end = pd.Timestamp(END_DATE, tz=ET) + pd.Timedelta(days=1)  # 含 2023-02-17 全天
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
        expiration_ns=last_ns + 3_652_000_000_000_000,  # 连续合约, 无真实到期
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
# 主流程
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("[1/4] 构建前一日 14 日 ATR 映射 (全数据 warmup) ...", flush=True)
    atr_map = build_atr_map()
    print(f"      ATR 覆盖 {len(atr_map):,} 个交易日")

    print("[2/4] 加载 5 分钟数据...", flush=True)
    bars, instrument, bar_type = build_bars_and_instrument()
    print(f"      {len(bars):,} 根 Bar, {instrument.id}, "
          f"乘数={instrument.multiplier}, 最小变动={instrument.price_increment}")

    print("[3/4] 配置回测引擎 ...", flush=True)
    venue = Venue(VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("ORB-BT-004")))
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
    )
    strategy = OrbStrategy(config, atr_map)
    engine.add_strategy(strategy)

    print("[4/4] 运行回测 ...", flush=True)
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
    print(f"===== 因开不了仓(qty<1)跳过: {strategy.n_skipped:,} 天 =====")
    print("===== 账户报告(首/尾) =====")
    print(acct.iloc[[0, -1]].to_string())
    print(f"\n最终权益: ${final_total:,.2f}  "
          f"总盈亏: ${final_total - STARTING_CAPITAL:,.2f}  "
          f"收益率: {(final_total / STARTING_CAPITAL - 1) * 100:,.1f}%")
    print(f"回测年限: {years:.2f} 年   年化收益率: {annual * 100:,.1f}%")
    print(f"峰值权益: ${daily.max():,.0f}   最大回撤: {mdd * 100:.1f}%")

    # 生成可视化报告
    create_tearsheet(engine, output_path="html_output/orb_report_v4.html", title="NQ 5min ORB v4 回测报告 (2016-2023)")
    print("\n可视化报告已生成: orb_report_v4.html")
