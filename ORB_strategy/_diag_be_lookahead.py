# -*- coding: utf-8 -*-
"""诊断: 入场当根 K 线是否立即参与「浮盈达 N R 拉保本」判定 (未来函数嫌疑)。

做法: 复用 orb_backtes_v8_4 的全部构建/主流程, 只 monkey-patch on_bar,
记录「入场成交是否在同一个 on_bar 调用内完成」以及「入场当根 bar 是否已把
止损移到保本」。窗口缩到 2016 全年以加速。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

import orb_backtes_v8_4 as V

V.START_DATE = "2016-01-01"
V.END_DATE = "2016-12-31"

FILLS = []          # 入场成交发生在 on_bar 内部 -> 记录该 bar 信息
orig_on_bar = V.OrbStrategy.on_bar


def patched_on_bar(self, bar):
    pre_entered = self.entered_today
    pre_trade = self._trade
    orig_on_bar(self, bar)
    if (not pre_entered) and self.entered_today and self._trade is not None:
        tr = self._trade
        FILLS.append({
            "bar_time": self._et_time(bar.ts_event),
            "side": tr["side"].name,
            "entry_px": tr["entry_px"],
            "r": tr["r"],
            "moved_in_same_bar": bool(tr["stop_moved"]),   # 关键: 同一根 bar 内已拉保本?
            "bar_high": bar.high.as_double(),
            "bar_low": bar.low.as_double(),
            "bar_close": bar.close.as_double(),
            "entry_was_none": pre_trade is None,
        })


V.OrbStrategy.on_bar = patched_on_bar

print("构建区间/ATR ...", flush=True)
range_map = V.build_range_map()
atr_map = V.build_atr_map()
day_last_bar = V.build_day_last_bar_map()
bars, instrument, bar_type = V.build_bars_and_instrument()
print(f"bars={len(bars)}", flush=True)

engine = BacktestEngine(config=BacktestEngineConfig(
    trader_id=TraderId("DIAG-BE"),
    logging=LoggingConfig(log_level="ERROR"),
))
engine.add_venue(
    venue=Venue(V.VENUE),
    oms_type=OmsType.NETTING,
    account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(V.STARTING_CAPITAL, USD)],
    fee_model=PerContractFeeModel(Money(V.COMMISSION_PER_CONTRACT, USD)),
)
engine.add_instrument(instrument)
engine.add_data(bars)
cfg = V.OrbStrategyConfig(
    instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
    risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
    atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
    be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS,
)
strat = V.OrbStrategy(cfg, atr_map, range_map, day_last_bar)
engine.add_strategy(strat)
print("运行 ...", flush=True)
engine.run()

df = pd.DataFrame(FILLS)
print("\n===== 诊断结果 =====")
print("入场笔数(2016):", len(df))
print("成交在 on_bar 同一调用内完成(同步成交)的笔数:", int(df["entry_was_none"].sum()))
n_same = int(df["moved_in_same_bar"].sum())
print("入场当根 K 线内就把止损拉到保本的笔数:", n_same, f"({n_same/len(df)*100:.1f}%)")
print("策略统计 n_be_moves =", strat.n_be_moves)

if len(df):
    df["up_r"] = (df["bar_high"] - df["entry_px"]) / df["r"]
    df["dn_r"] = (df["entry_px"] - df["bar_low"]) / df["r"]
    longm = df[df["side"] == "BUY"]
    shortm = df[df["side"] == "SELL"]
    print("\n入场当根 K 线相对入场价的 R 幅度 (中位/最大):")
    if len(longm):
        print(f"  做多 {len(longm)} 笔: 向上 reach R 中位 {longm['up_r'].median():.2f}, 最大 {longm['up_r'].max():.2f}")
    if len(shortm):
        print(f"  做空 {len(shortm)} 笔: 向下 reach R 中位 {shortm['dn_r'].median():.2f}, 最大 {shortm['dn_r'].max():.2f}")

pos = engine.trader.generate_positions_report()
print("总交易笔数:", len(pos))
