# -*- coding: utf-8 -*-
"""
prop_lucid25k.py  (propfirm/)
=============================
LucidFlex $25K 两阶段参数设计:
  考核期: 10 个交易日内通过 (快攻, $51 重置便宜 → 可反复买门票)
    MLL $1,000 / 目标 $1,250 / DLL $600 (确认) / 无 consistency (LucidFlex)
  funded: 稳妥 (慢打, q=1, 满 5 个盈利日提款)

两个目标函数, 两套手数:
  - 考核: 大手数抢速度 —— 10 天 ≈ 10 笔, 期望 0.367R/笔 → 需要 1R ≈ $340
    才够 $1,250 目标, 即 q ≈ 8-10 (2019 起中位 stop 18.5pt)。代价: 3 笔连亏爆。
  - funded: q=1 —— 1R ≈ $37, 生存连亏 27 笔 > 历史最差 25 笔, 几乎不可爆。

输出: 考核 q 阶梯通过率 / funded q=1 vs q=2 对比 / 两阶段联合经济账。
用法: python prop_lucid25k.py
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
import prop_funded_sim as fs  # noqa: E402

MULT = 2.0
CH_CAP_DAYS = 10                 # 考核时间窗: 10 个交易日
CH25 = dict(dd=1000.0, target=1250.0, freeze=1000.0,
            daily_loss=600.0, consistency=None)   # DLL $600 等比假设
CH_QTY = (2, 4, 6, 8, 10, 12, 14)
# funded: 满 5 个盈利日可提 (payout_every=1 = 上次提取后至少隔 1 天), cap $1,500 等比假设
FU25 = dict(payout_every=1, payout_frac=0.5, payout_cap=1000.0,
            min_win_days=5, win_day=1.0)
FU_DD = 1000.0
FEE = 51.0


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    dates = sorted(m["date"].unique())
    print(f"LucidFlex $25K 两阶段设计 | MLL $1,000 / 目标 $1,250 / DLL $600(确认) / "
          f"考核窗 {CH_CAP_DAYS} 交易日 / 重置 ${FEE:.0f}")

    # ---- [1] 考核期: q 阶梯, 10 交易日窗 ----
    print("\n[1] 考核期 (10 交易日窗): 手数阶梯")
    print("   q | 通过% | 爆% | 未决% | 1R$中位 | 生存连亏 | E[重置次数]")
    print("-" * 68)
    rows = []
    for q in CH_QTY:
        day_pnl, day_risk, ht, n = ps._daily_arrays(m, q, MULT)
        r = ps.simulate_attempts(day_pnl, day_risk, ht, n, mode="eod",
                                 cap=CH_CAP_DAYS, **CH25)
        stop_med = float(m["stop_pt"].median())
        r1 = stop_med * MULT * q
        p = r["pass_rate"] / 100
        rows.append((q, p))
        print(f" {q:>3} | {r['pass_rate']:5.1f} | {r['blow_rate']:4.1f} | "
              f"{100 - r['pass_rate'] - r['blow_rate']:4.1f} | {r1:>7.0f} | "
              f"{CH25['dd'] / r1:>6.1f} | {(1 - p) / p if p > 0 else float('nan'):>6.1f}")
    best_q, best_p = max(rows, key=lambda t: t[1])
    print(f"  → 通过率最高: q={best_q} (P={best_p * 100:.1f}%), "
          f"E[重置 {(1 - best_p) / best_p:.1f} 次 = ${(1 - best_p) / best_p * FEE:.0f}]")

    # ---- [2] funded 期: q=1 稳妥 (对照 q=2), 5 盈利日提款节奏 ----
    print("\n[2] funded 期 (MLL $1,000, 满 5 盈利日提款, cap $1,000 确认, 90% 到手):")
    fs.DD = FU_DD                          # $25K 回撤带
    print("   q | 月均payout中位 | 月均均值 | 爆仓% | 寿命中位(日) | 爆前人均总提")
    print("-" * 74)
    fu_rows = {}
    for q in (1, 2):
        day_pnl, _, _, n = ps._daily_arrays(m, q, MULT)
        res = fs.simulate_funded(day_pnl, n, q, FU25)
        withp = res[res["n_payouts"] > 0]
        fu_rows[q] = res
        print(f" {q:>3} | {res['monthly_payout'].median():>12,.0f} | "
              f"{res['monthly_payout'].mean():>8,.0f} | {res['blew'].mean() * 100:>5.1f} | "
              f"{res['life_days'].median():>10.0f} | "
              f"${withp['total_payout'].median() if len(withp) else 0:>10,.0f}")

    # ---- [3] 两阶段联合经济账 ----
    # 近似: 通过日在历史里近似均匀分布 → funded 期望直接用全起点分布
    print("\n[3] 联合经济账 (考核 q=最优 × funded q=1):")
    fu1 = fu_rows[1]
    p = best_p
    e_reset = (1 - p) / p
    e_funded = float(fu1["total_payout"].mean())
    e_life = float(fu1["life_days"].mean())
    print(f"  P(10日内过) = {p * 100:.1f}% (q={best_q}) | E[重考] = {e_reset:.1f} 次 "
          f"| 考核期望成本 = ${(e_reset + 1) * FEE:,.0f}")
    print(f"  funded 期望总提 (q=1, 含爆前) = ${e_funded:,.0f} | 期望寿命 {e_life:.0f} 交易日 "
          f"({e_life / 21:.1f} 月)")
    print(f"  每个完整周期期望净 = ${e_funded - (e_reset + 1) * FEE:,.0f} "
          f"(funded ${e_funded:,.0f} - 考核 ${(e_reset + 1) * FEE:,.0f})")
    print(f"  对照 $50K q=2 单周期: 净 ~$4,080 - $196 ≈ $3,884 | 月均 $392 vs "
          f"这里 ${float(fu1['monthly_payout'].mean()):,.0f}")

    print("\n注: DLL $600 与 payout cap $1,000 已确认, 手数上限 20 (DLL 才是真手数帽), "
          "以 Lucid 后台为准; 「未决%」= 10 日窗内既未过也未爆 (重置继续, 计重置成本); "
          "联合经济账用 funded 全起点分布近似 (通过日≈均匀覆盖)。")


if __name__ == "__main__":
    main()
