# -*- coding: utf-8 -*-
"""缺陷 1 修复的量化影响: 全样本 (2016-01-01 ~ 2026-08-30) 对比
   SLIPPAGE_TICKS = 0 (旧行为: 号称有滑点、实际零滑点)
   SLIPPAGE_TICKS = 1 (修复后: 1 tick = 0.25pt = $0.50/手/边)

同窗口、同参数, 只改滑点。输出终值/年化/MDD/Sharpe/Sortino/PF/胜率/手数规模。
"""
import os
import sys
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
print(f"窗口 {V.START_DATE}~{V.END_DATE}, bars={len(bars)}", flush=True)

venue = Venue(V.VENUE)
acct_start = pd.Timestamp(V.START_DATE)
acct_end = pd.Timestamp(V.END_DATE)
df = pd.read_parquet(V.DATA_PATH).tz_convert(V.ET)
df = df[(df.index >= acct_start) & (df.index < acct_end + pd.Timedelta(days=1))]
years = (df.index[-1] - df.index[0]).days / 365.25


def run(slip_ticks):
    fee = V.COMMISSION_PER_CONTRACT + slip_ticks * V.TICK * V.MULTIPLIER
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId(f"IMPACT-{slip_ticks}"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(
        venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        base_currency=USD, starting_balances=[Money(V.STARTING_CAPITAL, USD)],
        fee_model=PerContractFeeModel(Money(fee, USD)),
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
    engine.run()

    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())
    final = float(eq.iloc[-1])
    annual = (final / V.STARTING_CAPITAL) ** (1.0 / years) - 1.0
    ret = daily.pct_change().dropna().to_numpy()
    sharpe = float(ret.mean() / ret.std() * sqrt(252)) if ret.std() > 0 else 0.0
    dn = np.minimum(ret, 0.0)
    dstd = float(np.sqrt(np.mean(dn ** 2)))
    sortino = float(ret.mean() / dstd * sqrt(252)) if dstd > 0 else 0.0
    pos = engine.trader.generate_positions_report()
    pnls = np.array([V.money_float(p["realized_pnl"]) for _, p in pos.iterrows()
                     if p["ts_closed"] is not None])
    qtys = np.array([int(p["peak_qty"]) for _, p in pos.iterrows() if p["ts_closed"] is not None])
    winrate = float((pnls > 0).mean())
    gp = pnls[pnls > 0].sum()
    gl = abs(pnls[pnls <= 0].sum())
    return dict(slip=slip_ticks, fee=fee, n=len(pnls), final=final, annual=annual, mdd=mdd,
                sharpe=sharpe, sortino=sortino, pf=gp / gl, winrate=winrate,
                qty_min=int(qtys.min()), qty_med=int(np.median(qtys)), qty_max=int(qtys.max()),
                capped=strat.n_capped, cant_afford=strat.n_cant_afford, no_trade=strat.n_no_trade,
                be=strat.n_be_moves, stopped=strat.n_stopped, be_exits=strat.n_be_exits, eod=strat.n_eod)


res = [run(0), run(1)]
print("\n" + "=" * 78)
print(f"{'滑点':<8}{'每手每边':<10}{'笔数':<7}{'终值':<16}{'年化':<9}{'MDD':<9}{'Sharpe':<8}{'Sortino':<9}{'PF':<7}{'胜率':<8}")
print("=" * 78)
for r in res:
    print(f"{r['slip']} tick{'':<2}${r['fee']:<8.2f}{r['n']:<7}{'$' + format(r['final'], ',.0f'):<16}"
          f"{r['annual']*100:>6.1f}%  {r['mdd']*100:>6.1f}%  {r['sharpe']:>6.2f}  {r['sortino']:>6.2f}   "
          f"{r['pf']:>5.2f}  {r['winrate']*100:>5.1f}%")
for r in res:
    print(f"\n[r={r['slip']}] 手数 min/中位/max = {r['qty_min']}/{r['qty_med']}/{r['qty_max']}  "
          f"被上限压制 {r['capped']} 天, 买不起 {r['cant_afford']} 天, 无突破 {r['no_trade']} 天")
    print(f"        出场: 初始止损 {r['stopped']} / 保本 {r['be_exits']} / 收盘 {r['eod']}; 拉保本 {r['be']} 次")
d = res[1]["annual"] - res[0]["annual"]
print(f"\n滑点 1 tick 的代价: 年化 {res[0]['annual']*100:.1f}% → {res[1]['annual']*100:.1f}% "
      f"({d*100:+.1f} pp), 终值差 ${res[1]['final']-res[0]['final']:,.0f}")
print(f"回测年限 {years:.2f} 年; 成本假设: 手续费 ${V.COMMISSION_PER_CONTRACT}/手/边 + 滑点 N tick")
