# -*- coding: utf-8 -*-
"""
orb_backtest.py
===============
NQ 期货 5 分钟 ORB 日内策略 —— NautilusTrader 版本 (v1)

依据:
  - 论文 "Can Day Trading Really Be Profitable?" (Aziz & Zarattini, 2025) 的策略规则
  - 用户定制: 以损定仓, 固定亏损 $1000, 向下取整, 最少 1 手, 不足 1 手不开仓
  - 数据: NQ 16 年「连续 + 差值后复权」5 分钟 RTH K 线 (nq_5min_rth.parquet)

规则速览:
  - 时段: 9:30-16:00 ET (RTH)
  - K1 = 当日 9:30 的 5 分钟 K 线; K2 = 9:35 的 5 分钟 K 线
  - 方向: K1 收阳 → 只做多; 收阴 → 只做空; 十字星 → 不交易
  - 入场: K2 开盘价 (在 K1 收盘时下市价单, 成交于 K1 收盘 ≈ K2 开盘)
  - 止损: 多 = K1 最低价; 空 = K1 最高价 (距离 = 1R)
  - 止盈: 10R (限价单)
  - 以损定仓: 手数 = floor($1000 / (止损点数 × $20)); 手数 < 1 → 不开仓
  - 收盘平仓: 16:00 ET 全部平仓 + 撤销未成交的止损/止盈单; 每日最多 1 笔, 不加减仓
  - 手续费: $0.5/手/边; 无滑点

用法: cd ORB_strategy && ../.venv/bin/python orb_backtest.py
"""
from math import floor
from datetime import time as dtime

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
MAX_LOSS_USD = 1000.0                            # 单笔固定亏损
RISK_REWARD = 10.0                               # 止盈倍数 R
COMMISSION_PER_CONTRACT = 0.5                    # 手续费 $/手/边
SLIPPAGE = 0                                     # 滑点(0=无)

ET = zoneinfo.ZoneInfo("America/New_York")
T_K1 = dtime(9, 30)                              # 当日第一根 5 分钟 K 线
T_EOD = dtime(15, 55)                            # 当日最后一根 5 分钟 K 线(收盘 16:00)


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------
class OrbStrategyConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    max_loss_usd: float = 1000.0
    multiplier: float = 20.0
    risk_reward: float = 10.0


class OrbStrategy(Strategy):
    def __init__(self, config: OrbStrategyConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.max_loss_usd = config.max_loss_usd
        self.multiplier = config.multiplier
        self.risk_reward = config.risk_reward
        self.k1 = {}                          # 当日 K1 的 OHLC
        self.pending_entry = set()             # 已提交、待成交的入场单 client_order_id
        self.exit_sibling = {}                 # 止损/止盈订单 cid -> 另一方的 Order 对象
        self.n_entries = 0                     # 统计入场次数

    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def _et_time(self, ts_ns: int):
        return pd.Timestamp(ts_ns, unit="ns", tz="UTC").tz_convert(ET).time()

    def on_bar(self, bar: Bar):
        t = self._et_time(bar.ts_event)

        if t == T_K1:
            self.k1 = {
                "open": bar.open.as_double(),
                "high": bar.high.as_double(),
                "low": bar.low.as_double(),
                "close": bar.close.as_double(),
            }
            self._enter_on_k1()

        elif t == T_EOD:
            # 收盘强制平仓, 并撤销未成交的止损/止盈单(避免隔夜挂单)
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)

    def _enter_on_k1(self):
        k1o, k1h, k1l, k1c = self.k1["open"], self.k1["high"], self.k1["low"], self.k1["close"]

        # 方向判断 (十字星 = 不交易)
        if k1c > k1o:
            side = OrderSide.BUY
            stop = k1l
        elif k1c < k1o:
            side = OrderSide.SELL
            stop = k1h
        else:
            return

        # 入场价 = K1 收盘 (≈ K2 开盘); 止损点数 = |入场 - 止损|
        entry = k1c
        stop_points = abs(entry - stop)
        if stop_points <= 0:
            return

        # 以损定仓: 固定亏损 $1000, 向下取整, 最少 1 手, 不足 1 手不开仓
        per_contract_loss = stop_points * self.multiplier
        qty = int(floor(self.max_loss_usd / per_contract_loss))
        if qty < 1:
            return

        # 只提交市价入场单, 止损/止盈在成交回调中再挂(避免 reduce_only 提前校验)
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=Quantity.from_str(str(qty)),
        )
        self.pending_entry.add(order.client_order_id)
        self.submit_order(order)

    def on_order_filled(self, event):
        # 入场单成交 → 挂止损 + 止盈
        cid = event.client_order_id
        if cid in self.pending_entry:
            self.pending_entry.discard(cid)
            self.n_entries += 1

            entry_px = event.last_px.as_double()
            qty = event.last_qty
            side = event.order_side
            exit_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            stop = self.k1["low"] if side == OrderSide.BUY else self.k1["high"]
            stop_points = abs(entry_px - stop)
            tp = entry_px + self.risk_reward * stop_points if side == OrderSide.BUY \
                else entry_px - self.risk_reward * stop_points

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
            # 记录互为"另一方", 一方成交即撤销另一方
            self.exit_sibling[sl.client_order_id] = tp_order
            self.exit_sibling[tp_order.client_order_id] = sl
            self.log.info(
                f"入场 {side.name} qty={qty} px={entry_px:.2f} (K1close={self.k1['close']:.2f}) "
                f"stop={stop:.2f} tp={tp:.2f} ({stop_points:.2f}pt)"
            )
            return

        # 止损/止盈成交 → 撤销另一方(OUO: 避免残留挂单)
        sibling = self.exit_sibling.pop(event.client_order_id, None)
        if sibling is not None:
            self.cancel_order(sibling)


# ---------------------------------------------------------------------------
# 数据: 读 5 分钟 parquet → Nautilus Bar + 合成连续合约
# ---------------------------------------------------------------------------
def build_bars_and_instrument() -> tuple[list[Bar], FuturesContract, BarType]:
    df = pd.read_parquet(DATA_PATH)
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
    print("[1/3] 加载 5 分钟数据并构建 Bar ...", flush=True)
    bars, instrument, bar_type = build_bars_and_instrument()
    print(f"      {len(bars):,} 根 Bar, {instrument.id}, "
          f"乘数={instrument.multiplier}, 最小变动={instrument.price_increment}")

    print("[2/3] 配置回测引擎 ...", flush=True)
    venue = Venue(VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("ORB-BT-001")))
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
        max_loss_usd=MAX_LOSS_USD,
        multiplier=MULTIPLIER,
        risk_reward=RISK_REWARD,
    )
    strategy = OrbStrategy(config)
    engine.add_strategy(strategy)

    print("[3/3] 运行回测 ...", flush=True)
    engine.run()

    # ---- 结果 ----
    acct = engine.trader.generate_account_report(venue)
    final_total = float(str(acct['total'].iloc[-1]).replace(',', ''))
    print(f"\n===== 入场次数: {strategy.n_entries:,} =====")
    print("===== 账户报告(首/尾) =====")
    print(acct.iloc[[0, -1]].to_string())
    print(f"\n最终权益: ${final_total:,.2f}  "
          f"总盈亏: ${final_total - STARTING_CAPITAL:,.2f}  "
          f"收益率: {(final_total / STARTING_CAPITAL - 1) * 100:,.1f}%")

    # 生成可视化报告
    create_tearsheet(engine, output_path="orb_report.html", title="NQ 5min ORB 回测报告")
    print("\n可视化报告已生成: orb_report.html")
