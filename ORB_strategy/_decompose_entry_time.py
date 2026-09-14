# -*- coding: utf-8 -*-
"""把每笔交易按「入场 bar 时间」摊开, 看入场时点的期望收益曲线。

问题: T_WIN_END 10:10 -> 10:30 多 15 笔却让终值 -13.5%, 说明后段入场是负期望。
本脚本跑当前配置(10:30)并按 bar 时间分组统计: 笔数 / 总盈亏 / 平均净 R / 胜率。
"""
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import numpy as np
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
venue = Venue(V.VENUE)
print(f"配置: {V.START_DATE}~{V.END_DATE} | 窗口 {V.T_WIN_START:%H:%M}-{V.T_WIN_END:%H:%M} | "
      f"BE {V.BE_R_MULTIPLE:g}R | risk {V.RISK_PER_TRADE:.2%} | 滑点 {V.SLIPPAGE_TICKS}tick", flush=True)

fee = V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER
e = BacktestEngine(config=BacktestEngineConfig(
    trader_id=TraderId("DECOMP-1"), logging=LoggingConfig(log_level="ERROR")))
e.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
            base_currency=USD, starting_balances=[Money(V.STARTING_CAPITAL, USD)],
            fee_model=PerContractFeeModel(Money(fee, USD)))
e.add_instrument(instrument)
e.add_data(bars)
cfg = V.OrbStrategyConfig(
    instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
    risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
    atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
    be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS)
s = V.OrbStrategy(cfg, atr_map, range_map, day_last_bar)
e.add_strategy(s)
print("运行 ...", flush=True)
e.run()

pos = e.trader.generate_positions_report()
g = defaultdict(list)
for _, p in pos.iterrows():
    if p["ts_closed"] is None:
        continue
    t = pd.Timestamp(p["ts_opened"]).tz_convert(V.ET)
    qty = int(p["peak_qty"])
    pnl = V.money_float(p["realized_pnl"])
    atr = atr_map.get(t.date())
    D = max(V.TICK, V.tick_round(V.ATR_STOP_FRACTION * atr)) if atr else None
    r_mult = pnl / (qty * D * V.MULTIPLIER) if D else None
    g[t.strftime("%H:%M")].append((pnl, qty, r_mult, t.strftime("%Y-%m-%d")))

print("\n" + "=" * 92)
print(f"{'入场bar':<9}{'笔数':<7}{'总盈亏':<16}{'平均每笔':<14}{'平均净R':<11}{'胜率':<8}{'累计占比'}")
tot = sum(len(v) for v in g.values())
tot_pnl = sum(x[0] for v in g.values() for x in v)
cum = 0
for k in sorted(g):
    v = g[k]
    pnl = sum(x[0] for x in v)
    rs = [x[2] for x in v if x[2] is not None]
    win = sum(1 for x in v if x[0] > 0) / len(v)
    cum += pnl
    print(f"{k:<9}{len(v):<7}${pnl:<14,.0f}${pnl/len(v):<12,.0f}"
          f"{(np.mean(rs) if rs else 0):<+10.3f}{win*100:>5.1f}%   {100*cum/tot_pnl:>6.1f}%")
print(f"{'合计':<9}{tot:<7}${tot_pnl:<14,.0f}")

print("\n=== 分界点: 10:10 (原窗口末端) 之后 vs 之前 ===")
late = [x for k, v in g.items() if k > "10:10" for x in v]
early = [x for k, v in g.items() if k <= "10:10" for x in v]
for label, v in (("09:30-10:10", early), ("10:15-10:30", late)):
    if not v:
        print(f"  {label}: 无交易"); continue
    pnl = sum(x[0] for x in v)
    rs = [x[2] for x in v if x[2] is not None]
    win = sum(1 for x in v if x[0] > 0) / len(v)
    print(f"  {label}: {len(v)} 笔, 总盈亏 ${pnl:,.0f}, 平均 ${pnl/len(v):,.0f}, "
          f"平均净R {np.mean(rs):+.3f}, 胜率 {win*100:.1f}%")
if late:
    print(f"\n  后段这 {len(late)} 笔的明细:")
    for pnl, qty, r, d in sorted(late, key=lambda x: x[3]):
        print(f"    {d}  {qty:>4d} 手  盈亏 ${pnl:>+10,.0f}  R {r:+.2f}")
