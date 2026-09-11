# -*- coding: utf-8 -*-
"""
distribution_fit.py
===================
对 v8.4 ORB 回测的每笔交易结果做「分布拟合 + 拟合优度检验 + 尾部/风险指标」。

拟合对象: R 倍数 (R = 每笔盈亏 / 该笔初始风险), 而不是美元盈亏。
  理由: R 倍数消除「复利仓位」的影响, 跨 16 年可比 (v8.4 每笔风险 = 权益 1%)。
  附: 也打印美元盈亏的描述统计做参考。

内容:
  1) 描述统计 + 正态性检验 (偏度/峰度/Shapiro-Wilk/Jarque-Bera)
  2) 候选分布 MLE 拟合 + 对数似然 + AIC/BIC + KS + Anderson-Darling 比较
  3) 右尾分析 (Hill 尾指数 + 对盈利单拟合 Pareto/对数正态/Gamma/Weibull)
  4) 风险/质量指标 (VaR/CVaR/胜率/盈亏比/期望/最大连亏)
  5) 输出图表 html_output/dist_fit_v8_4.png

用法: cd ORB_strategy && ../.venv/bin/python distribution_fit.py
"""
import importlib.util

import numpy as np
import pandas as pd
import scipy.stats as st
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

# ===========================================================================
# 配置
# ===========================================================================
START_DATE = "2016-01-01"
END_DATE = "2026-08-30"
CAPITAL = 50_000              # 对齐 notebook 里 v8.4 的「起始 $5万」
OUT_PNG = "html_output/dist_fit_v8_4.png"


def _load_v84():
    spec = importlib.util.spec_from_file_location("orb_v84", "orb_backtes_v8_4.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _run_and_extract(m):
    m.START_DATE = START_DATE
    m.END_DATE = END_DATE
    m.STARTING_CAPITAL = CAPITAL

    range_map = m.build_range_map()
    atr_map = m.build_atr_map()
    day_last_bar = m.build_day_last_bar_map()
    bars, instrument, bar_type = m.build_bars_and_instrument()

    venue = Venue(m.VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-BT-FIT"), logging=LoggingConfig(log_level="WARNING")))
    engine.add_venue(venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(CAPITAL, USD)],
                     fee_model=PerContractFeeModel(Money(m.COMMISSION_PER_CONTRACT, USD)))
    engine.add_instrument(instrument)
    engine.add_data(bars)
    cfg = m.OrbStrategyConfig(instrument_id=m.INSTRUMENT_ID, bar_type=str(bar_type),
                              risk_per_trade=m.RISK_PER_TRADE, multiplier=m.MULTIPLIER,
                              atr_stop_fraction=m.ATR_STOP_FRACTION, max_qty=m.MAX_QTY,
                              tp_r_multiple=m.TP_R_MULTIPLE, be_r_multiple=m.BE_R_MULTIPLE,
                              be_buffer_ticks=m.BE_BUFFER_TICKS)
    strat = m.OrbStrategy(cfg, atr_map, range_map, day_last_bar)
    engine.add_strategy(strat)
    engine.run()
    return m, engine


# ---- GoF 统计量 ----
def ks_d(data, cdf):
    x = np.sort(data)
    n = len(x)
    F = np.clip(cdf(x), 1e-15, 1 - 1e-15)
    Dp = np.max(np.arange(1, n + 1) / n - F)
    Dm = np.max(F - np.arange(0, n) / n)
    return float(max(Dp, Dm))


def ad_a2(data, cdf):
    x = np.sort(data)
    n = len(x)
    F = np.clip(cdf(x), 1e-15, 1 - 1e-15)
    s = 0.0
    for i in range(1, n + 1):
        s += (2 * i - 1) * (np.log(F[i - 1]) + np.log1p(-F[n - i]))
    return float(-n - s / n)


def hill_alpha(winners, k=None):
    """右尾 Hill 估计器: alpha 越小尾越肥 (alpha<=2 → 方差不存在)。"""
    x = np.sort(winners)[::-1]           # 降序
    if k is None:
        k = int(np.sqrt(len(x)))
    k = max(2, min(k, len(x) - 2))
    return float(k / np.sum(np.log(x[:k] / x[k]))), k


# ===========================================================================
# 主流程
# ===========================================================================
def main():
    m = _load_v84()
    m, engine = _run_and_extract(m)
    venue = Venue(m.VENUE)

    # ---- 转 R 倍数: R = 每笔盈亏 / (该笔名义风险 = RISK_PER_TRADE × 入场时权益) ----
    # 用「入场时权益」而非重构止损价, 更稳健 (v8.4 每笔风险 = 权益 1%)
    pos = engine.trader.generate_positions_report()
    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    eq = eq.sort_index()

    R, pnl = [], []
    for _, p in pos.iterrows():
        if p["ts_closed"] is None:
            continue
        realized = m.money_float(p["realized_pnl"])
        ts_open = pd.Timestamp(p["ts_opened"])
        eq_at = eq.asof(ts_open)
        if eq_at is None or eq_at <= 0:
            continue
        R.append(realized / (m.RISK_PER_TRADE * eq_at))
        pnl.append(realized)
    R = np.array(R)
    pnl = np.array(pnl)
    print(f"交易总数 {len(R):,} 笔 (R = 盈亏 / 入场时权益的 1%)\n")

    # ---- 1) 描述统计 + 正态性 ----
    print("=" * 78)
    print("1) R 倍数描述统计 + 正态性检验")
    print("=" * 78)
    win = R[R > 0]
    loss = R[R <= 0]
    print(f"均值 {R.mean():+.3f}   中位 {np.median(R):+.3f}   标准差 {R.std(ddof=1):.3f}")
    print(f"偏度 skew {st.skew(R):+.3f}   超峰度 ex-kurtosis {st.kurtosis(R):+.3f}")
    print(f"最小 {R.min():+.3f}   最大 {R.max():+.3f}")
    print(f"胜率 {len(win)/len(R)*100:.1f}%   盈亏比(PF) {win.sum()/abs(loss.sum()):.2f}   期望 E[R] {R.mean():+.3f}")
    sh = st.shapiro(R)
    jb = st.jarque_bera(R)
    print(f"Shapiro-Wilk p={sh.pvalue:.2e}   Jarque-Bera p={jb.pvalue:.2e}  "
          f"({'拒绝正态' if sh.pvalue < 0.05 else '无法拒绝正态'})")
    print(f"美元盈亏: 均值 ${pnl.mean():,.0f}  中位 ${np.median(pnl):,.0f}  最大赢 ${pnl.max():,.0f}  最大亏 ${pnl.min():,.0f}\n")

    # ---- 2) 候选分布拟合 + 比较 ----
    print("=" * 78)
    print("2) 候选分布 MLE 拟合 (拟合 R 倍数) —— 越小越好")
    print("=" * 78)
    dists = [
        ("正态 norm", st.norm),
        ("t 分布", st.t),
        ("拉普拉斯 laplace", st.laplace),
        ("logistic", st.logistic),
        ("对数正态 lognorm", st.lognorm),
        ("Gamma", st.gamma),
        ("Weibull_min", st.weibull_min),
        ("指数 expon", st.expon),
    ]
    header = f"{'分布':<20} {'k':>2} {'LL':>12} {'AIC':>12} {'BIC':>12} {'KS D':>8} {'AD A2':>8}"
    print(header)
    print("-" * 78)
    results = []
    for name, dist in dists:
        try:
            params = dist.fit(R)
            ll = float(np.sum(dist.logpdf(R, *params)))
            k = len(params)
            n = len(R)
            aic = 2 * k - 2 * ll
            bic = k * np.log(n) - 2 * ll
            cdf = lambda x, d=dist, p=params: d.cdf(x, *p)
            d_ks = ks_d(R, cdf)
            d_ad = ad_a2(R, cdf)
            results.append((name, k, ll, aic, bic, d_ks, d_ad))
            print(f"{name:<20} {k:>2} {ll:>12.1f} {aic:>12.1f} {bic:>12.1f} {d_ks:>8.4f} {d_ad:>8.3f}")
        except Exception as e:
            print(f"{name:<20} 拟合失败: {e}")
    print("\n注: KS/AD 的 p 值因参数由数据估计, 不能直接读表; 这里只比较统计量大小, 严格 p 值需 Lilliefors/自助法。")

    # ---- 3) 右尾分析 ----
    print("\n" + "=" * 78)
    print("3) 右尾分析 (盈利单 R>0, 长尾来自'持有到收盘不封顶')")
    print("=" * 78)
    winners = R[R > 0]
    if len(winners) > 10:
        alpha, k = hill_alpha(winners)
        print(f"盈利单 {len(winners):,} 笔, 中位 R={np.median(winners):+.2f}, 最大 R={winners.max():+.2f}")
        print(f"Hill 尾指数 alpha ≈ {alpha:.2f} (k={k})  →  "
              f"{'方差不存在(极肥尾)' if alpha <= 2 else ('有限方差, 肥尾' if alpha <= 3 else '接近正态尾')}")
        print("对盈利单拟合 (右尾候选):")
        for name, dist in [("Pareto", st.pareto), ("对数正态 lognorm", st.lognorm),
                           ("Gamma", st.gamma), ("Weibull_min", st.weibull_min)]:
            try:
                params = dist.fit(winners)
                ll = float(np.sum(dist.logpdf(winners, *params)))
                kk = len(params)
                aic = 2 * kk - 2 * ll
                cdf = lambda x, d=dist, p=params: d.cdf(x, *p)
                print(f"  {name:<20} AIC={aic:>12.1f}  AD={ad_a2(winners, cdf):>8.3f}  KS={ks_d(winners, cdf):>8.4f}")
            except Exception as e:
                print(f"  {name:<20} 拟合失败: {e}")

    # ---- 4) 风险/质量指标 ----
    print("\n" + "=" * 78)
    print("4) 风险 / 质量指标")
    print("=" * 78)
    for q in [0.01, 0.05]:
        var = np.quantile(R, q)
        cvar = R[R <= var].mean()
        print(f"VaR({q:.0%}) = {var:+.3f}R    CVaR/ES({q:.0%}) = {cvar:+.3f}R")
    # 最大连亏 (按 R 序列)
    streak = cur = 0
    for x in R:
        cur = cur + 1 if x <= 0 else 0
        streak = max(streak, cur)
    print(f"最大连亏 {streak} 笔")

    # ---- 5) 图 ----
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    ax = axes[0]
    lo, hi = np.percentile(R, [0.1, 99.9])
    xs = np.linspace(lo, hi, 400)
    ax.hist(R, bins=80, density=True, alpha=0.5, color="#26a69a", label="R empirical")
    for name, dist, color in [("norm", st.norm, "#ef5350"), ("lognorm", st.lognorm, "#ffb300"),
                              ("gamma", st.gamma, "#42a5f5"), ("weibull", st.weibull_min, "#ab47bc")]:
        try:
            p = dist.fit(R)
            ax.plot(xs, dist.pdf(xs, *p), color=color, lw=1.5, label=name)
        except Exception:
            pass
    ax.set_xlabel("R (PnL / risk)"); ax.set_ylabel("density"); ax.set_title("R distribution + fitted PDFs")
    ax.legend(fontsize=8)

    ax = axes[1]
    st.probplot(R, dist="norm", plot=ax)
    ax.set_title("Q-Q plot (vs normal)")
    fig.tight_layout()
    fig.savefig(OUT_PNG, dpi=110)
    print(f"\n图表已保存: {OUT_PNG}")


if __name__ == "__main__":
    main()
