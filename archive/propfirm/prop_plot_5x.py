# -*- coding: utf-8 -*-
"""
prop_plot_5x.py  (propfirm/)
============================
「同时买 5 个 LucidFlex $50K 考试号」的资金曲线 (2019-01 → 2026-08):
  5 个号跑完全相同的策略 = 完全复制 (同过同爆) → 现金流 = 单号 × 5。
  起点 0 → 买号瞬间 -5×$92 = -$460 (考核费) → 随 payout/重考累计到终点。
  两条线: r14 档 (以损定仓 1R=$143, 当前最优) vs q=2 固定 (原方案)。
  红色背景带 = 考核阶段 (无 payout, 只有费用), 绿色 = 资金号阶段。

用法: python prop_plot_5x.py   → results/equity_5x_lucid50k.png
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.dates as mdates  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import prop_sim as ps  # noqa: E402
import prop_full_journey as fj  # noqa: E402
from prop_lucid50k_r14 import daily_custom  # noqa: E402

plt.rcParams["font.sans-serif"] = ["Arial Unicode MS", "PingFang HK", "Heiti TC",
                                   "Helvetica Neue"]
plt.rcParams["axes.unicode_minus"] = False

MULT = 2.0


def journey_ledger(day_pnl: np.ndarray, dates: list) -> tuple[list, list]:
    res = fj.run_journey(day_pnl, dates, 0, "LucidFlex $50K",
                         fj.FEE_MODELS["LucidFlex $50K"])
    return res["ledger"], res["journeys"]


COST_TYPES = {"考核费", "reset费", "考核月费", "激活费"}


def cumulative(ledger: list, k: float) -> tuple[pd.Series, float]:
    df = pd.DataFrame(ledger, columns=["date", "type", "amount"])
    df["date"] = pd.to_datetime(df["date"])
    # ledger 里成本条目存正数 —— 现金流视角成本取负
    df["signed"] = df.apply(
        lambda r: -r["amount"] if r["type"] in COST_TYPES else r["amount"], axis=1)
    df["signed"] = df["signed"] * k
    df = df.sort_values("date")
    cum = df.set_index("date")["signed"].cumsum()
    return cum, float(cum.iloc[-1])


def stage_spans(ax, journeys: list, dates: list) -> None:
    for r in journeys:
        d0 = pd.Timestamp(dates[r["start"]])
        d1 = pd.Timestamp(dates[min(r["end"], len(dates) - 1)])
        color = "#e74c3c" if r["state"] == "考核" else "#2ecc71"
        ax.axvspan(d0, d1, color=color, alpha=0.06, lw=0)


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    # r14 档 (以损定仓 1R=$143)
    base, day_pnl_r14, _, _ = daily_custom(m)
    dates = base["date"].tolist()
    led14, journeys14 = journey_ledger(day_pnl_r14, dates)
    # q=2 固定
    dp2, _, _, n2 = ps._daily_arrays(m, 2, MULT)
    led2, journeys2 = journey_ledger(dp2, dates)

    K = 5.0
    cum14, end14 = cumulative(led14, K)
    cum2, end2 = cumulative(led2, K)
    dd14 = float((cum14 - cum14.cummax()).min())
    dd2 = float((cum2 - cum2.cummax()).min())

    fig, ax = plt.subplots(figsize=(13, 6.5), dpi=130)
    stage_spans(ax, journeys14, dates)

    ax.plot(cum14.index, cum14.values, drawstyle="steps-post", lw=1.8,
            color="#2980b9",
            label=f"5×LucidFlex 50K, r14 档 (1R=143 以损定仓) — 终点 {end14:,.0f} 美元")
    ax.plot(cum2.index, cum2.values, drawstyle="steps-post", lw=1.5,
            color="#8e44ad", label=f"5×LucidFlex 50K, q=2 固定 — 终点 {end2:,.0f} 美元")

    # 关键标注 (asof: 纯考核期无现金流事件, 取此前最近累计值)
    ax.axhline(0, color="gray", lw=0.8, ls="--")
    v2022 = float(cum14.asof(pd.Timestamp("2022-07-01")))
    v2025 = float(cum14.asof(pd.Timestamp("2025-08-01")))
    ax.annotate("买入 5 个号\n-460 美元", xy=(pd.Timestamp("2019-01-02"), -460),
                xytext=(pd.Timestamp("2019-06-01"), -14000),
                fontsize=9, color="#c0392b",
                arrowprops=dict(arrowstyle="->", color="#c0392b", lw=0.8))
    ax.annotate("2022 大年",
                xy=(pd.Timestamp("2022-07-01"), v2022),
                xytext=(pd.Timestamp("2020-11-01"), v2022 - 40000), fontsize=9,
                arrowprops=dict(arrowstyle="->", color="gray", lw=0.8))
    ax.annotate("2025 弱年:\n连续考核打转\n(曲线横盘)",
                xy=(pd.Timestamp("2025-08-01"), v2025),
                xytext=(pd.Timestamp("2025-03-01"), v2025 - 30000), fontsize=9,
                color="#c0392b",
                arrowprops=dict(arrowstyle="->", color="#c0392b", lw=0.8))
    ax.annotate(f"{end14:,.0f}", xy=(cum14.index[-1], end14),
                xytext=(pd.Timestamp("2025-10-01"), end14 + 15000),
                fontsize=10, fontweight="bold", color="#2980b9")

    ax.set_title("5 × LucidFlex 50K 完全复制 — 累计净现金流 (2019-01 → 2026-08, 费用已扣)\n"
                 f"r14 档: 终点 {end14:,.0f}, 曲线最大回撤 {-dd14:,.0f}  |  "
                 f"q=2 固定: 终点 {end2:,.0f}, 最大回撤 {-dd2:,.0f}  |  "
                 "红带=考核期, 绿带=资金号期",
                 fontsize=11)
    ax.set_ylabel("累计净现金流 (美元)")
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.grid(alpha=0.25)
    ax.legend(loc="upper left", fontsize=9)
    fig.tight_layout()
    out = HERE / "results" / "equity_5x_lucid50k.png"
    fig.savefig(out, dpi=130)
    print(f"已保存 {out}")
    print(f"r14 档: 终点 ${end14:,.0f} | 最大曲线回撤 ${-dd14:,.0f} | "
          f"起点 -$460 (5×$92 考核费)")
    print(f"q=2 固定: 终点 ${end2:,.0f} | 最大曲线回撤 ${-dd2:,.0f}")


if __name__ == "__main__":
    main()
