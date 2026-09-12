# -*- coding: utf-8 -*-
"""
_repro_sep5.py  (GLM_working)
=============================
复现 9/5 heatmap_v8_6 的 (7.5%ATR, 3R BE, 0.7% risk) = 年化 76% / MDD -29.9%，
并把「滑点 0 vs 2 tick」「本金 $25k vs $250k」做 2x2 分解，定量回答：
为什么同参数在 GLM_working Stage A 里是 50.4% / -51.0%。

复现手段: monkey-patch orb_core.FEE_PER_SIDE (run_backtest 每次调用时读模块全局)。
额外输出: 每日历年 年化收益 + 当年内 MDD, 对比两种成本口径下回撤的来源差异。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import orb_core_v84 as core

START, END = "2016-01-01", "2026-08-30"
STOP, BE, RISK = 0.075, 3.0, 0.007


def yearly_breakdown(daily_eq):
    """按日历年切权益曲线: 年收益 + 年内 MDD。"""
    import pandas as pd
    daily = daily_eq.copy()
    daily.index = pd.to_datetime(daily.index)
    out = []
    for y, g in daily.groupby(daily.index.year):
        peak = g.cummax()
        mdd = float((g - peak).div(peak).min())
        out.append((y, float(g.iloc[-1] / g.iloc[0] - 1.0), mdd))
    return out


def run_once(slippage_ticks: float, capital: float, want_yearly: bool):
    core.FEE_PER_SIDE = 0.5 + slippage_ticks * core.TICK * core.MULTIPLIER
    data = core.build_data(START, END)
    core.ensure_bars(data)

    # 跑回测并取每日权益 (compute_metrics 内部重算, 这里单独再取一次账户报告)
    m = core.run_backtest(STOP, BE, RISK, data, capital)

    yearly = None
    if want_yearly:
        # 重跑一次只为拿权益曲线 (engine 已 reset; 直接再跑, 17s 可接受)
        from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
        from nautilus_trader.backtest.models import PerContractFeeModel
        from nautilus_trader.config import LoggingConfig
        from nautilus_trader.model.currencies import USD
        from nautilus_trader.model.identifiers import TraderId, Venue
        from nautilus_trader.model.enums import AccountType, OmsType
        from nautilus_trader.model.objects import Money
        venue = Venue(core.VENUE)
        eng = BacktestEngine(config=BacktestEngineConfig(
            trader_id=TraderId("ORB-REPRO"), logging=LoggingConfig(log_level="ERROR")))
        eng.add_venue(venue=venue, oms_type=OmsType.NETTING,
                      account_type=AccountType.MARGIN, base_currency=USD,
                      starting_balances=[Money(capital, USD)],
                      fee_model=PerContractFeeModel(Money(core.FEE_PER_SIDE, USD)))
        eng.add_instrument(data["instrument"])
        eng.add_data(data["bars"])
        cfg = core.OrbStrategyConfig(
            instrument_id=core.INSTRUMENT_ID, bar_type=str(data["bar_type"]),
            risk_per_trade=RISK, multiplier=core.MULTIPLIER,
            atr_stop_fraction=STOP, max_qty=core.MAX_QTY,
            be_r_multiple=BE, be_buffer_ticks=core.BE_BUFFER_TICKS)
        strat = core.OrbStrategy(cfg, data["atr_map"], data["range_map"],
                                 data["day_last_bar"])
        eng.add_strategy(strat)
        eng.run()
        acct = eng.trader.generate_account_report(venue)
        eq = acct["total"].astype(float)
        eq.index = pd_to_dt(acct.index)
        yearly = yearly_breakdown(eq)
        eng.reset()
    return m, yearly


def pd_to_dt(idx):
    import pandas as pd
    return pd.to_datetime(idx)


def main():
    cells = [
        ("A v8_6复刻: 滑点=0,   本金=$25k ", 0.0, 25_000),
        ("B          : 滑点=2tick, 本金=$25k ", 2.0, 25_000),
        ("C          : 滑点=0,   本金=$250k", 0.0, 250_000),
        ("D Stage A基线: 滑点=2tick, 本金=$250k", 2.0, 250_000),
    ]
    results = {}
    for name, slip, cap in cells:
        m, _ = run_once(slip, cap, want_yearly=False)
        results[name] = m
        print(f"{name}| 年化={m['annual']*100:6.1f}%  MDD={m['mdd']*100:6.1f}%  "
              f"Sharpe={m['sharpe']:.2f}  最终权益=${m['final_equity']:>12,.0f}  "
              f"笔数={m['n_entries']}", flush=True)

    # 年度分解: A(无滑点) vs D(有滑点) —— 回撤来源对比
    for name, slip, cap in [cells[0], cells[3]]:
        m, yearly = run_once(slip, cap, want_yearly=True)
        print(f"\n{name} 年度分解 (日历年收益 / 年内MDD):")
        for y, ret, mdd in yearly:
            bar = "#" * max(0, int(abs(mdd) * 40))
            print(f"  {y}  {ret*100:+7.1f}%   {mdd*100:+6.1f}%  {bar}")


if __name__ == "__main__":
    main()
