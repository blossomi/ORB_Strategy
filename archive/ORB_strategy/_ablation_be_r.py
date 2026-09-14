# -*- coding: utf-8 -*-
"""A/B: BE 判定用的 R = 反推前名义值 vs 反推后实际值。

同时统计反推把止损距离撑宽了多少(nominal -> actual), 这是两者产生分歧的幅度。
其余参数一律用磁盘当前值(2020-2026 / 7R / 窗口到 10:30 / risk 0.7% / 1 tick)。
"""
import os
import statistics as st
import sys

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
print(f"窗口 {V.START_DATE}~{V.END_DATE}, bars={len(bars):,}, "
      f"窗口 {V.T_WIN_START:%H:%M}-{V.T_WIN_END:%H:%M}, BE {V.BE_R_MULTIPLE:g}R, "
      f"risk {V.RISK_PER_TRADE:.2%}, 滑点 {V.SLIPPAGE_TICKS}tick", flush=True)

PAIRS = []                       # (名义距离, 实际距离)
_orig_enter = V.OrbStrategy._enter


def tapped_enter(self, side, bar, d):
    _orig_enter(self, side, bar, d)
    if self.pending_entry:
        cid = list(self.pending_entry)[-1]
        val = self.pending_entry[cid]
        if isinstance(val, tuple):
            actual, nominal = val
            PAIRS.append((float(nominal), float(actual)))     # (名义, 实际)


def run(use_nominal):
    PAIRS.clear()                     # 避免跨两次运行累积重复计数
    V.BE_USE_NOMINAL_R = use_nominal
    fee = V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER
    e = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("BEAB-" + ("NOM" if use_nominal else "ACT")),
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
    sh = float(ret.mean() / ret.std() * sqrt257()) if ret.std() > 0 else 0.0
    dn = np.minimum(ret, 0.0)
    ds = float(np.sqrt(np.mean(dn ** 2)))
    so = float(ret.mean() / ds * sqrt257()) if ds > 0 else 0.0
    pos = e.trader.generate_positions_report()
    pnls = np.array([V.money_float(p["realized_pnl"]) for _, p in pos.iterrows()
                     if p["ts_closed"] is not None])
    gp = pnls[pnls > 0].sum(); gl = abs(pnls[pnls <= 0].sum())
    return dict(tag=("名义 R" if use_nominal else "实际 R"), n=s.n_entries, final=final, mdd=mdd,
                sharpe=sh, sortino=so, pf=(gp / gl if gl else 0),
                win=float((pnls > 0).mean()), be=s.n_be_moves,
                stopped=s.n_stopped, be_exits=s.n_be_exits, eod=s.n_eod)


def sqrt257():
    from math import sqrt
    return sqrt(252)


a = run(True)
b = run(False)

print("\n=== 反推把止损距离撑宽了多少 (名义 -> 实际) ===")
wid = [(ac / no - 1) for no, ac in PAIRS if no > 0]
wid.sort()
if wid:
    nz = sum(1 for w in wid if w > 1e-9)
    print(f"  样本 {len(wid)} 笔, 其中反推实际生效 {nz} 笔 ({100*nz/len(wid):.1f}%)")
    print(f"  放宽幅度: 中位 {st.median(wid):+.2%}  90分位 {wid[int(len(wid)*0.9)]:+.2%}  最大 {wid[-1]:+.2%}")
    print(f"  -> BE 触发价因此被推远: {V.BE_R_MULTIPLE:g}R 的差 = "
          f"{V.BE_R_MULTIPLE*st.median(wid):.2f}R 的中位偏移")

print("\n" + "=" * 96)
print(f"{'BE 用的 R':<12}{'入场':<8}{'终值':<16}{'MDD':<9}{'Sharpe':<8}{'Sortino':<9}{'PF':<7}{'胜率':<8}{'拉保本':<8}")
for r in (a, b):
    print(f"{r['tag']:<12}{r['n']:<8}${r['final']:<14,.0f}{r['mdd']*100:>6.1f}%  "
          f"{r['sharpe']:>6.2f}  {r['sortino']:>6.2f}   {r['pf']:>5.2f}  {r['win']*100:>5.1f}%  {r['be']}")
for r in (a, b):
    print(f"  [{r['tag']}] 出场: 初始止损 {r['stopped']} / 保本 {r['be_exits']} / 收盘 {r['eod']}")
print(f"\n换成名义 R 的差异: 终值 ${a['final']-b['final']:+,.0f} "
      f"({100*(a['final']-b['final'])/b['final']:+.2f}%), MDD {100*(a['mdd']-b['mdd']):+.1f}pp, "
      f"拉保本 {a['be']-b['be']:+d} 次")
