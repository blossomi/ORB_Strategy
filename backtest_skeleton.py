# -*- coding: utf-8 -*-
"""
NautilusTrader 最小回测骨架
============================
流程: 读取本地 DBN (GLBX.MDP3, ohlcv-1m) → 抽取单个 NQ 合约 NQU0 的一天数据
      → 构建期货合约 + Bar 对象 → 跑一个最简单的策略 → 输出结果。

目的: 让用户先感受 NautilusTrader 的完整管道(数据→合约→引擎→策略→结果),
      之后再决定是否把 ORB 策略完整移植过来。

用法: .venv/bin/python backtest_skeleton.py
"""
from decimal import Decimal

import pandas as pd
from databento import DBNStore

from nautilus_trader.analysis import create_tearsheet
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
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
# 1) 从 DBN 抽取一个 NQ 合约(NQU0)的 1 分钟 K 线
# ---------------------------------------------------------------------------
DBN_PATH = "GLBX-20260831-A8UNFK5CD7/glbx-mdp3-20100606-20260830.ohlcv-1m.dbn.zst"
SYMBOL = "NQU0"          # NQ 2010年9月合约
TRADE_DATE = "2010-06-07"  # 只取这一天

store = DBNStore.from_file(DBN_PATH)
# to_df(count=...) 返回迭代器, 只解码文件开头一部分(无需全量加载 16 年)
df = next(store.to_df(count=300_000))

nq = df[df["symbol"] == SYMBOL]
# ts_event 在索引上(UTC)
day = nq[nq.index.normalize() == pd.Timestamp(TRADE_DATE, tz="UTC")]
day = day.sort_index()
print(f"[数据] {SYMBOL} 在 {TRADE_DATE} 共 {len(day)} 根 1 分钟 K 线")
print(f"[数据] 价格范围: {day['low'].min():.2f} ~ {day['high'].max():.2f}, "
      f"成交总量: {int(day['volume'].sum())}")
assert len(day) > 0, "当日无数据"

# ---------------------------------------------------------------------------
# 2) 构建 NQ 期货合约 (NQU0)
# ---------------------------------------------------------------------------
venue = Venue("GLBX")                        # CME Globex → GLBX
instrument_id = InstrumentId.from_str(f"{SYMBOL}.GLBX")
first_ts_ns = dt_to_unix_nanos(day.index[0])
expiry_ns = dt_to_unix_nanos(pd.Timestamp("2010-09-17 13:00", tz="America/New_York"))

instrument = FuturesContract(
    instrument_id=instrument_id,
    raw_symbol=Symbol(SYMBOL),
    asset_class=AssetClass.INDEX,             # 纳斯达克100指数期货
    currency=USD,
    price_precision=2,                        # 0.25 跳动 → 2 位小数
    price_increment=Price.from_str("0.25"),
    multiplier=Quantity.from_str("20"),       # NQ 点值 $20/点
    lot_size=Quantity.from_str("1"),
    underlying="NQ",
    activation_ns=first_ts_ns - 86_400_000_000_000,  # 前一天(示意)
    expiration_ns=expiry_ns,
    ts_event=first_ts_ns,
    ts_init=first_ts_ns,
)
print(f"[合约] {instrument_id}  乘数={instrument.multiplier}  "
      f"最小变动={instrument.price_increment}")

# ---------------------------------------------------------------------------
# 3) 把 DataFrame 转成 Nautilus Bar 对象
# ---------------------------------------------------------------------------
bar_type = BarType.from_str(f"{SYMBOL}.GLBX-1-MINUTE-LAST-EXTERNAL")
bars: list[Bar] = []
for ts, row in day.iterrows():
    ts_ns = dt_to_unix_nanos(ts)
    bars.append(Bar(
        bar_type=bar_type,
        open=Price.from_str(f"{row['open']:.2f}"),
        high=Price.from_str(f"{row['high']:.2f}"),
        low=Price.from_str(f"{row['low']:.2f}"),
        close=Price.from_str(f"{row['close']:.2f}"),
        volume=Quantity.from_str(str(int(row["volume"]))),
        ts_event=ts_ns,
        ts_init=ts_ns,
    ))
print(f"[Bar] 构建 {len(bars)} 根 Bar")

# ---------------------------------------------------------------------------
# 4) 最简单的策略: 首根 K 线买入 1 手, 持有 60 根(1小时)后平仓
# ---------------------------------------------------------------------------
class SimpleConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    quantity: str = "1"
    hold_bars: int = 60


class SimpleStrategy(Strategy):
    def __init__(self, config: SimpleConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.quantity = Quantity.from_str(config.quantity)
        self.hold_bars = config.hold_bars
        self.entered = False
        self.exited = False
        self.bars_since_entry = 0

    def on_start(self):
        self.subscribe_bars(self.bar_type)     # 订阅 1 分钟 K 线

    def on_bar(self, bar: Bar):
        if not self.entered:
            # 首根 K 线市价买入 1 手
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=self.quantity,
            )
            self.submit_order(order)
            self.entered = True
            self.log.info(f"入场: {order.client_order_id} @ bar {bar.ts_event}")
        else:
            self.bars_since_entry += 1
            if not self.exited and self.bars_since_entry >= self.hold_bars:
                # 到达持有目标后全部平仓(会平掉多头)
                self.close_all_positions(self.instrument_id)
                self.exited = True
                self.log.info(f"平仓信号 @ bar {bar.ts_event}")

    def on_stop(self):
        pass


# ---------------------------------------------------------------------------
# 5) 回测引擎
# ---------------------------------------------------------------------------
engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("BACKTEST-001")))

engine.add_venue(
    venue=venue,
    oms_type=OmsType.NETTING,
    account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(1_000_000, USD)],
)
engine.add_instrument(instrument)
engine.add_data(bars)

config = SimpleConfig(
    instrument_id=str(instrument_id),
    bar_type=str(bar_type),
    quantity="1",
    hold_bars=60,
)
engine.add_strategy(SimpleStrategy(config))

engine.run()

# ---------------------------------------------------------------------------
# 6) 结果
# ---------------------------------------------------------------------------
print("\n===== 账户报告 =====")
print(engine.trader.generate_account_report(venue))
print("\n===== 成交报告 =====")
print(engine.trader.generate_order_fills_report())
print("\n===== 持仓报告 =====")
print(engine.trader.generate_positions_report())

create_tearsheet(
    engine=engine,
    output_path="backtest_report.html"
)