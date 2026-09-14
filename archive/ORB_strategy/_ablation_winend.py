# -*- coding: utf-8 -*-
"""单变量: 入场窗口末端 T_WIN_END = 10:10 vs 10:30 (其余全用磁盘当前值)。

背景: 入场时间分布显示 66% 在 09:30、10:05 之后只剩个位数, 所以延长窗口预期影响极小。
本脚本只改 T_WIN_END, 验证这个判断。
"""
import os
import sys
from datetime import time as dtime
from math import sqrt

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
print(f"窗口 {V.START_DATE}~{V.END_DATE}, bars={len(bars):,}", flush=True)


def run(win_end):
    V.T_WIN_END = win_end            # on_bar 直接读全局, 改这个即可
    fee = V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER
    e = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ABL-" + win_end.strftime("%H%M")),
        logging=LoggingConfig(log_level="ERROR")))
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
    e.run()
    acct = e.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())
    final = float(eq.iloc[-1])
    ret = daily.pct_change().dropna().to_numpy()
    sh = float(ret.mean() / ret.std() * sqrt(252)) if ret.std() > 0 else 0.0
    dn = np.minimum(ret, 0.0)
    ds = float(np.sqrt(np.mean(dn ** 2)))
    so = float(ret.mean() / ds * sqrt(252)) if ds > 0 else 0.0
    pos = e.trader.generate_positions_report()
    pnls = np.array([V.money_float(p["realized_pnl"]) for _, p in pos.iterrows()
                     if p["ts_closed"] is not None])
    gp = pnls[pnls > 0].sum(); gl = abs(pnls[pnls <= 0].sum())
    return dict(we=win_end, n=s.n_entries, final=final, mdd=mdd, sharpe=sh, sortino=so,
                pf=(gp / gl if gl else 0), win=float((pnls > 0).mean()),
                no_trade=s.n_no_trade, be=s.n_be_moves,
                stopped=s.n_stopped, be_exits=s.n_be_exits, eod=s.n_eod,
                lot_exact=s.n_lot_exact)


a = run(dtime(10, 10))
b = run(dtime(10, 30))          # 与磁盘当前值一致
print("\n" + "=" * 88)
print(f"{'T_WIN_END':<12}{'入场':<8}{'终值':<16}{'MDD':<9}{'Sharpe':<8}{'Sortino':<9}{'PF':<7}{'胜率':<8}{'无突破天'}")
for r in (a, b):
    print(f"{r['we'].strftime('%H:%M'):<12}{r['n']:<8}${r['final']:<14,.0f}{r['mdd']*100:>6.1f}%  "
          f"{r['sharpe']:>6.2f}  {r['sortino']:>6.2f}   {r['pf']:>5.2f}  {r['win']*100:>5.1f}%  {r['no_trade']}")
for r in (a, b):
    print(f"  [{r['we'].strftime('%H:%M')}] 出场: 初始止损 {r['stopped']} / 保本 {r['be_exits']} / 收盘 {r['eod']}; "
          f"拉保本 {r['be']} 次; 整数手跳过 {r['lot_exact']}")
print(f"\n延长窗口(10:10 -> 10:30)带来: 入场 {b['n']-a['n']:+d} 笔, "
      f"终值 ${b['final']-a['final']:+,.0f} ({100*(b['final']-a['final'])/a['final']:+.2f}%), "
      f"MDD {100*(b['mdd']-a['mdd']):+.1f}pp")
