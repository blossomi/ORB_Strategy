# -*- coding: utf-8 -*-
"""
alpha_beta.py  (GLM_working)
============================
对 ORB 策略日收益 vs NQ 买入持有日收益做 OLS 回归:
    Ret_strategy = alpha + beta * Ret_NQ
输出: alpha(日/年化)、beta、各自 p 值、R²、样本天数; 并按当日多/空方向拆分,
检验「策略收益是否只是变相做多/做空大盘」(beta 是否显著非零)。

口径:
  - 策略: 指定 (stop, BE, risk) 组合跑回测, 取账户每日权益 pct_change (未交易日=0)。
  - 基准: nq_5min_rth.parquet 日收盘 close-to-close 日收益 (与论文 QQQ 口径一致, 含隔夜)。
  - 成本: $0.5 + 2 tick 滑点/手/边, 本金 $250k。

用法: ../.venv/bin/python alpha_beta.py [--combo 0.075,5] [--combo 0.075,3]
"""
import argparse
import csv
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orb_core_v84 as core

CAPITAL = 250_000
BENCH_PATH = str(Path(__file__).resolve().parent.parent / "ORB_strategy" / "nq_5min_rth.parquet")


def ols_full(x: np.ndarray, y: np.ndarray) -> dict:
    """手动 OLS + 双侧 p 值 (linregress 只给斜率的 p)。"""
    n = len(x)
    beta, alpha = np.polyfit(x, y, 1)
    resid = y - (alpha + beta * x)
    sigma2 = float(resid @ resid) / (n - 2)
    xbar = float(x.mean())
    sxx = float(((x - xbar) ** 2).sum())
    se_beta = float(np.sqrt(sigma2 / sxx))
    se_alpha = float(np.sqrt(sigma2 * (1.0 / n + xbar**2 / sxx)))
    t_beta = beta / se_beta
    t_alpha = alpha / se_alpha
    return dict(
        n=n, alpha_daily=float(alpha), beta=float(beta),
        p_alpha=float(2 * stats.t.sf(abs(t_alpha), df=n - 2)),
        p_beta=float(2 * stats.t.sf(abs(t_beta), df=n - 2)),
        r2=float(1 - (resid @ resid) / (((y - y.mean()) ** 2).sum())),
        corr=float(np.corrcoef(x, y)[0, 1]),
    )


def benchmark_daily_returns() -> pd.Series:
    df = pd.read_parquet(BENCH_PATH).tz_convert(core.ET)
    daily_close = df["close"].resample("1D").last().dropna()
    daily_close.index = daily_close.index.normalize()
    return daily_close.pct_change().dropna()


def run_strategy_daily(stop: float, be: float) -> tuple[pd.Series, pd.Series]:
    """跑一次回测 → (每日权益收益 Series, 每日方向 Series [+1多/-1空])。"""
    data = core.build_data("2016-01-01", "2026-08-30")
    core.ensure_bars(data)

    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.backtest.models import PerContractFeeModel
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.identifiers import TraderId, Venue
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.objects import Money

    venue = Venue(core.VENUE)
    eng = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-AB"), logging=LoggingConfig(log_level="ERROR")))
    eng.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                  base_currency=USD, starting_balances=[Money(CAPITAL, USD)],
                  fee_model=PerContractFeeModel(Money(core.FEE_PER_SIDE, USD)))
    eng.add_instrument(data["instrument"])
    eng.add_data(data["bars"])
    cfg = core.OrbStrategyConfig(
        instrument_id=core.INSTRUMENT_ID, bar_type=str(data["bar_type"]),
        risk_per_trade=0.007, multiplier=core.MULTIPLIER, atr_stop_fraction=stop,
        max_qty=core.MAX_QTY, be_r_multiple=be, be_buffer_ticks=core.BE_BUFFER_TICKS)
    strat = core.OrbStrategy(cfg, data["atr_map"], data["range_map"], data["day_last_bar"])
    eng.add_strategy(strat)
    eng.run()

    acct = eng.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(eq.index).tz_convert(core.ET).normalize()
    daily_ret = eq.resample("1D").last().dropna().pct_change().fillna(0.0)
    daily_ret.index = daily_ret.index.normalize()

    # 每交易日方向 (策略一天最多一笔: entered_today 单标记)
    pos = eng.trader.generate_positions_report()
    side_map = {}
    for _, p in pos.iterrows():
        if p["ts_closed"] is None:
            continue
        d = pd.Timestamp(p["ts_opened"]).tz_convert(core.ET).normalize()
        side_map[d] = 1.0 if str(p["entry"]) == "BUY" else -1.0
    eng.reset()
    return daily_ret, pd.Series(side_map)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--combo", action="append", default=None,
                    help="止损,保本R (默认 0.075,5 与 0.075,3)")
    args = ap.parse_args()
    combos = args.combo or ["0.075,5", "0.075,3"]

    bench = benchmark_daily_returns()
    print(f"基准: NQ 连续日收益 close-to-close, {bench.index[0].date()} ~ {bench.index[-1].date()}, "
          f"{len(bench)} 天; 年化 {((1+bench).prod()**(252/len(bench))-1)*100:.1f}% "
          f"(几何), Sharpe {bench.mean()/bench.std()*np.sqrt(252):.2f}\n", flush=True)

    rows = []
    for c in combos:
        stop, be = (float(v) for v in c.split(","))
        sret, side = run_strategy_daily(stop, be)
        df = pd.DataFrame({"y": sret, "x": bench}).dropna()
        # 只在有基准收益的日子回归 (含策略未交易的 0 收益日, 与论文同口径)
        x, y = df["x"].to_numpy(), df["y"].to_numpy()
        m = ols_full(x, y)

        label = f"{stop:.1%}×{be:g}R"
        print(f"===== ORB {label} (risk 0.7%, $25万, 2tick滑点) =====")
        print(f"  全样本     : alpha日={m['alpha_daily']*100:.4f}%  alpha年化={m['alpha_daily']*252*100:+.1f}%  "
              f"beta={m['beta']:+.3f}  p(alpha)={m['p_alpha']:.2g}  p(beta)={m['p_beta']:.2g}  "
              f"R2={m['r2']:.3f}  相关={m['corr']:+.3f}  n={m['n']}")
        rows.append(dict(combo=label, segment="全样本", **m))

        for seg, mask in [("只看多日", side > 0), ("只看空日", side < 0)]:
            days = side.index[side.reindex(df.index).fillna(0) > 0 if seg == "只看多日"
                              else side.reindex(df.index).fillna(0) < 0]
            sub = df.loc[df.index.isin(days)]
            mm = ols_full(sub["x"].to_numpy(), sub["y"].to_numpy())
            n_long = int((side > 0).sum()); n_short = int((side < 0).sum())
            print(f"  {seg:<10}: alpha年化={mm['alpha_daily']*252*100:+.1f}%  beta={mm['beta']:+.3f}  "
                  f"p(beta)={mm['p_beta']:.2g}  R2={mm['r2']:.3f}  天数={mm['n']}"
                  f"({'多' if seg=='只看多日' else '空'}{n_long if seg=='只看多日' else n_short}天)")
            rows.append(dict(combo=label, segment=seg, **mm))
        print()

    os.makedirs("results", exist_ok=True)
    out = "results/alpha_beta.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
