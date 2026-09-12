# -*- coding: utf-8 -*-
"""单变量拆解: 区间起点 9:00-9:29 vs 9:15-9:29 (其余参数一律用磁盘上当前的值)。

背景: 磁盘上的 v8_4.py 在 09-12 01:40 被同时改了 4 处 ——
  区间起点 9:00 -> 9:15 / risk 0.7% -> 0.6% / 滑点 1 -> 2 tick / (注释)
结果从 $2.28M 掉到 $0.87M。滑点本身只值约 $196k, 所以主因要单独测。

本脚本只动 T_RANGE_START, 其它全从模块读取当前值。
"""
import os
import sys
from math import sqrt

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

from datetime import time as dtime

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

bars, instrument, bar_type = V.build_bars_and_instrument()
atr_map = V.build_atr_map()
day_last_bar = V.build_day_last_bar_map()
venue = Venue(V.VENUE)


def run(range_start):
    V.T_RANGE_START = range_start
    range_map = V.build_range_map()
    fee = V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER
    e = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ABL-" + range_start.strftime("%H%M")),
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
    sharpe = float(ret.mean() / ret.std() * sqrt(252)) if ret.std() > 0 else 0.0
    dn = np.minimum(ret, 0.0)
    ds = float(np.sqrt(np.mean(dn ** 2)))
    sortino = float(ret.mean() / ds * sqrt(252)) if ds > 0 else 0.0
    pos = e.trader.generate_positions_report()
    pnls = np.array([V.money_float(p["realized_pnl"]) for _, p in pos.iterrows()
                     if p["ts_closed"] is not None])
    qty = np.array([int(p["peak_qty"]) for _, p in pos.iterrows() if p["ts_closed"] is not None])
    gp = pnls[pnls > 0].sum(); gl = abs(pnls[pnls <= 0].sum())
    # 区间宽度分布
    widths = [hi - lo for hi, lo in (range_map.get(d) for d in list(range_map)[-30:]) if hi is not None]
    return dict(rs=range_start, n=len(pnls), final=final,
                annual=(final / V.STARTING_CAPITAL) ** (1 / 10.65) - 1,
                mdd=mdd, sharpe=sharpe, sortino=sortino,
                pf=(gp / gl if gl else float("inf")), win=float((pnls > 0).mean()),
                qmed=int(np.median(qty)), qmax=int(qty.max()),
                no_trade=s.n_no_trade, capped=s.n_capped,
                med_width=float(np.median(widths)) if widths else 0.0)


print(f"当前文件参数: risk={V.RISK_PER_TRADE}, 滑点={V.SLIPPAGE_TICKS}tick, "
      f"MAX_QTY={V.MAX_QTY}, BE={V.BE_R_MULTIPLE}R, 止损={V.ATR_STOP_FRACTION:.1%}ATR", flush=True)
a = run(dtime(9, 0))
b = run(dtime(9, 15))

print("\n" + "=" * 92)
print(f"{'区间起点':<10}{'笔数':<7}{'终值':<16}{'年化':<9}{'MDD':<9}{'Sharpe':<8}{'Sortino':<9}{'PF':<7}{'胜率':<8}{'无交易天'}")
for r in (a, b):
    print(f"{r['rs'].strftime('%H:%M'):<10}{r['n']:<7}${r['final']:<14,.0f}{r['annual']*100:>6.1f}%  "
          f"{r['mdd']*100:>6.1f}%  {r['sharpe']:>6.2f}  {r['sortino']:>6.2f}   {r['pf']:>5.2f}  "
          f"{r['win']*100:>5.1f}%  {r['no_trade']}")
for r in (a, b):
    print(f"  [{r['rs'].strftime('%H:%M')}] 手数中位 {r['qmed']} / max {r['qmax']}, 被上限压制 {r['capped']} 天, "
          f"近期区间中位宽度 {r['med_width']:.1f} pt")
print(f"\n仅把区间起点从 09:15 改回 09:00: 终值 ${b['final']:,.0f} -> ${a['final']:,.0f} "
      f"({100*(a['final']-b['final'])/b['final']:+.1f}%), 年化 {b['annual']*100:.1f}% -> {a['annual']*100:.1f}%")
