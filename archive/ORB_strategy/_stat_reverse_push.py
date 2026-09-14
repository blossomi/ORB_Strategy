# -*- coding: utf-8 -*-
"""统计: 反推把止损距离从名义值撑宽了多少 (按年看, 因为撑宽幅度 ~ 1/手数)。

只跑一次回测(名义 R 配置), 记录每笔的 (名义距离, 实际距离)。
"""
import os
import statistics as st
import sys
from collections import defaultdict

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

range_map = V.build_range_map()
atr_map = V.build_atr_map()
day_last_bar = V.build_day_last_bar_map()
bars, instrument, bar_type = V.build_bars_and_instrument()

REC = []
_orig_enter = V.OrbStrategy._enter


def tapped_enter(self, side, bar, d):
    _orig_enter(self, side, bar, d)
    if self.pending_entry:
        cid = list(self.pending_entry)[-1]
        actual, nominal = self.pending_entry[cid]
        REC.append((d.year, float(nominal), float(actual)))


V.OrbStrategy._enter = tapped_enter          # ← 上一次就是漏了这行

e = BacktestEngine(config=BacktestEngineConfig(
    trader_id=TraderId("PUSH-1"), logging=LoggingConfig(log_level="ERROR")))
e.add_venue(venue=Venue(V.VENUE), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
            base_currency=USD, starting_balances=[Money(V.STARTING_CAPITAL, USD)],
            fee_model=PerContractFeeModel(
                Money(V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER, USD)))
e.add_instrument(instrument)
e.add_data(bars)
cfg = V.OrbStrategyConfig(
    instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
    risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
    atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
    be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS)
e.add_strategy(V.OrbStrategy(cfg, atr_map, range_map, day_last_bar))
print("运行 ...", flush=True)
e.run()

pos_len = sum(1 for _, p in e.trader.generate_positions_report().iterrows() if p["ts_closed"] is not None)
wid = [a / n - 1 for _, n, a in REC if n > 0]
wid.sort()
print(f"\n记录 {len(REC)} 笔 (平仓 {pos_len} 笔)")
print(f"其中反推真正生效(实际>名义, 含 tick 取整后的变化): "
      f"{sum(1 for _, n, a in REC if a > n + 1e-9)} 笔")
print(f"\n撑宽幅度 (实际/名义 - 1):")
print(f"  中位 {st.median(wid):+.3%}  均值 {st.mean(wid):+.3%}  "
      f"10% {wid[int(len(wid)*0.1)]:+.2%}  90% {wid[int(len(wid)*0.9)]:+.2%}  最大 {wid[-1]:+.2%}")
print(f"  -> BE {V.BE_R_MULTIPLE:g}R 触发价被推远的中位幅度: "
      f"{V.BE_R_MULTIPLE * st.median(wid):+.2f}R (即 {V.BE_R_MULTIPLE:.0f}R 名义 ≈ "
      f"{V.BE_R_MULTIPLE/(1+st.median(wid)):.1f}R 实际)")

byyear = defaultdict(list)
for y, n, a in REC:
    if n > 0:
        byyear[y].append(a / n - 1)
print(f"\n按年:")
print(f"{'年':<7}{'笔数':<7}{'撑宽中位':<12}{'撑宽90分位':<13}{'最大':<10}")
for y in sorted(byyear):
    v = sorted(byyear[y])
    print(f"{y:<7}{len(v):<7}{st.median(v):>+9.3%}{'':<3}{v[int(len(v)*0.9)]:>+9.2%}{'':<3}{v[-1]:>+8.2%}")
