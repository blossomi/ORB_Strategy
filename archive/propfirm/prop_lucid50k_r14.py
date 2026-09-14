# -*- coding: utf-8 -*-
"""
prop_lucid50k_r14.py  (propfirm/)
=================================
LucidFlex $50K 的「生存 14 笔」档 —— 把 Labs 测试胜出的模式 A (以损定仓 + ATR 7.5%)
移植过来:
  1R = MLL $2,000 ÷ 14 ≈ $143 (恒定美元风险)
  手数 q_d = floor($143 / (当日止损pt × $2)), 最少 1 手 —— 波动大减仓、波动小加仓,
  每笔亏的钱恒为 ~$143 (floor 后略低)。
其余规则与主线一致: 考核 MLL $2,000 / 目标 $3,000 / DLL $1,200 / cons 50%;
funded 无 consistency, payout 50% 利润 cap $3,000, 90% 分成, payout 后重置。
策略本身零改动 (区间/收盘价入场/5R 保本/EOD 平仓)。

输出: 每笔止损与手数分布 / 考核表现 / 全程循环 / 季度盈亏 (含考核与出金阶段)。
对照: q=2 固定手数主口径 (README §4/§7.1)。
用法: python prop_lucid50k_r14.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import prop_sim as ps  # noqa: E402
import prop_full_journey as fj  # noqa: E402
import prop_funded_sim as fs  # noqa: E402

MULT = 2.0
DD = 2000.0
R1 = DD / 14.0                    # $142.86 —— 生存 14 笔
FIRM = "LucidFlex $50K"
FEE = fj.FEE_MODELS[FIRM]


def daily_custom(m: pd.DataFrame):
    """按日聚合 + 以损定仓手数。返回 (dates, day_pnl, day_risk, per_trade风险)。"""
    g = m.groupby("date").agg(pnl_pc=("pnl_pc", "sum"), stop_pt=("stop_pt", "max"))
    g = g.reset_index()
    q = np.maximum(1, np.floor(R1 / (g["stop_pt"] * MULT).to_numpy())).astype(int)
    day_pnl = q * g["pnl_pc"].to_numpy()
    day_risk = q * g["stop_pt"].to_numpy() * MULT
    return g, day_pnl, day_risk, q


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    base, day_pnl, day_risk, q = daily_custom(m)
    dates = base["date"].tolist()
    n = len(dates)
    risk_per_trade = q * base["stop_pt"].to_numpy() * MULT

    print(f"LucidFlex $50K 「生存 14 笔」档: 1R = ${R1:.0f} 恒定 (以损定仓 + ATR 7.5%)")
    print(f"  每笔实际风险: 中位 ${np.median(risk_per_trade):.0f} "
          f"(P10 ${np.percentile(risk_per_trade, 10):.0f} / "
          f"P90 ${np.percentile(risk_per_trade, 90):.0f}) | "
          f"生存连亏 = $2,000 / $143 = {DD / R1:.1f} 笔")
    yr = pd.DataFrame({"y": pd.to_datetime(dates).year, "q": q}).groupby("y")["q"]
    print("  各年手数中位: " + " ".join(f"{y}:{int(v)}" for y, v in yr.median().items()))

    # ---- [1] 考核 ----
    ht = np.ones(n, dtype=bool)
    r = ps.simulate_attempts(day_pnl, day_risk, ht, n, dd=DD, target=3000.0,
                             freeze=DD, mode="eod", cap=120, daily_loss=1200.0,
                             consistency=0.5)
    print(f"\n[1] 考核 (120 交易日窗): 通过 {r['pass_rate']:.1f}% | 爆 {r['blow_rate']:.1f}% "
          f"| 中位 {r['days_median']:.0f} 天   "
          f"[对照 q=2 固定: 32.2% / 29.1% / 56 天]")

    # ---- [2] 全程循环 ----
    res = fj.run_journey(day_pnl, dates, 0, FIRM, FEE)
    j = pd.DataFrame(res["journeys"])
    j["days"] = j["end"] - j["start"] + 1
    ch, fu = j[j["state"] == "考核"], j[j["state"] == "funded"]
    tot = fu["payout"].sum()
    print(f"\n[2] 全程循环 (2019Q1 → 2026Q3): 考核 {len(ch)} 轮 (过 "
          f"{len(ch[ch['result'] == '通过'])}) | funded {len(fu)} 轮 | "
          f"payout ${tot:,.0f} | 费用 ${res['fees']:,.0f} | 净 ${tot - res['fees']:,.0f} "
          f"  [对照 q=2 固定: 7 轮(6) / 6 轮 / $39,350 / $644 / $38,706]")

    # ---- [3] 季度账本 ----
    led = pd.DataFrame(res["ledger"], columns=["date", "type", "amount"])
    led["date"] = pd.to_datetime(led["date"])
    led["quarter"] = led["date"].dt.to_period("Q").astype(str)
    q_days = {}
    for _, row in j.iterrows():
        cur = pd.Timestamp(dates[row["start"]])
        end = pd.Timestamp(dates[row["end"]])
        while cur <= end:
            qk = f"{cur.year}Q{(cur.month - 1) // 3 + 1}"
            q_days.setdefault(qk, dict(考核=0, funded=0))[row["state"]] += 1
            cur += pd.Timedelta(days=1)
    piv = led.pivot_table(index="quarter", columns="type", values="amount",
                          aggfunc="sum").fillna(0.0)
    for col in ("payout", "考核费"):
        if col not in piv:
            piv[col] = 0.0
    all_q = [str(p) for p in pd.period_range("2019Q1", "2026Q3", freq="Q")]
    piv = piv.reindex(all_q, fill_value=0.0)
    piv["净现金流"] = piv["payout"] - piv["考核费"]
    piv["考核天"] = [q_days.get(k, {}).get("考核", 0) for k in piv.index]
    piv["资金号天"] = [q_days.get(k, {}).get("funded", 0) for k in piv.index]
    piv = piv.reset_index().rename(columns={"index": "quarter"})

    print("\n[3] 季度盈亏 (含考核阶段与出金阶段):")
    print(f"{'季度':<8} | {'payout':>8} | {'考核费':>6} | {'净现金流':>8} | {'考核天':>4} {'资金号天':>5}")
    print("-" * 60)
    for _, row in piv.iterrows():
        flag = " ←考核" if row["资金号天"] == 0 and row["考核天"] > 30 else ""
        print(f"{row['quarter']:<8} | {row['payout']:>8,.0f} | {row['考核费']:>6.0f} "
              f"| {row['净现金流']:>8,.0f} | {row['考核天']:>4} {row['资金号天']:>5}{flag}")
    tp, tc = piv["payout"].sum(), piv["考核费"].sum()
    neg = piv[piv["净现金流"] < 0]
    print("-" * 60)
    print(f"{'合计':<8} | {tp:>8,.0f} | {tc:>6.0f} | {tp - tc:>8,.0f} |")
    print(f"负现金流季度: {len(neg)} 个 ({', '.join(neg['quarter']) if len(neg) else '无'}) "
          f"| 最大单季回吐 -${-neg['净现金流'].min():,.0f}" if len(neg) else "无负现金流季度")
    piv.to_csv(HERE / "results" / "quarterly_lucid50k_r14.csv", index=False)
    print("\n明细已存 results/quarterly_lucid50k_r14.csv")


if __name__ == "__main__":
    main()
