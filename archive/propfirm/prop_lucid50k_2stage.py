# -*- coding: utf-8 -*-
"""
prop_lucid50k_2stage.py  (propfirm/)
====================================
LucidFlex $50K 两阶段异构手数矩阵 —— 回答:
  「考核阶段激进些, 一旦拿到资金号就稳扎稳打, 比单一手数方案好吗? 好多少?」

手数方案 (全部以损定仓: 1R = MLL/生存笔数 恒定, q_d = floor(1R/(stop_d×$2))):
  考核档:  r14 (1R $143) / r10 ($200) / r7 ($286) / r5 ($400) —— 越小越激进
  资金号档: r27 (1R $74, ≈q=2 固定的自适应版) / r20 ($100) / r14 ($143)

每组合跑完整 2019→2026 循环 (考核↔funded 状态机), 与两个基准对比:
  r14/r14 = $54,517 (单一手数最优, §7.5);  q2/q2 = $38,706 (原主口径)。
用法: python prop_lucid50k_2stage.py
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

MULT = 2.0
FIRM = "LucidFlex $50K"
FEE = fj.FEE_MODELS[FIRM]
CH_TIERS = (14, 10, 7, 5)          # 生存笔数 → 1R = 2000/笔数
FU_TIERS = (27, 20, 14)


def day_pnl_of(m: pd.DataFrame, tier: int):
    """以损定仓日盈亏: 1R = 2000/tier, q_d = floor(1R/(stop_d×$2))。"""
    g = m.groupby("date").agg(pnl_pc=("pnl_pc", "sum"), stop_pt=("stop_pt", "max"))
    g = g.reset_index()
    r1 = 2000.0 / tier
    q = np.maximum(1, np.floor(r1 / (g["stop_pt"] * MULT).to_numpy())).astype(int)
    return g["date"].tolist(), q * g["pnl_pc"].to_numpy(), q, r1


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    print("LucidFlex $50K 两阶段异构手数矩阵 (2019→2026 完整循环):")
    print(f"{'考核档':<10} | {'资金号档':<9} | {'考核轮(过)':>9} | {'payout':>9} "
          f"| {'费用':>6} | {'净':>8} | {'月净':>6}")
    print("-" * 76)
    rows = []
    for ct in CH_TIERS:
        ch_dates, ch_pnl, ch_q, ch_r1 = day_pnl_of(m, ct)
        for ft in FU_TIERS:
            _, fu_pnl, fu_q, fu_r1 = day_pnl_of(m, ft)
            res = fj.run_journey(ch_pnl, ch_dates, 0, FIRM, FEE, fu_pnl=fu_pnl)
            j = pd.DataFrame(res["journeys"])
            j["days"] = j["end"] - j["start"] + 1
            ch = j[j["state"] == "考核"]
            net = res["journeys"] and sum(x["payout"] for x in res["journeys"]
                                          if x["state"] == "funded") - res["fees"]
            months = (pd.Timestamp(ch_dates[-1]) - pd.Timestamp(ch_dates[0])).days / 30.44
            rows.append(dict(ch=ct, fu=ft, net=net, months=months,
                             ch_n=len(ch), ch_pass=len(ch[ch["result"] == "通过"])))
            print(f"r{ct} (1R${ch_r1:>3.0f},q~{int(np.median(ch_q)):>2}) | "
                  f"r{ft} (1R${fu_r1:>3.0f},q~{int(np.median(fu_q)):>2}) | "
                  f"{len(ch):>3} ({len(ch[ch['result'] == '通过'])}) | "
                  f"{sum(x['payout'] for x in res['journeys'] if x['state'] == 'funded'):>9,.0f} | "
                  f"{res['fees']:>6,.0f} | {net:>8,.0f} | {net / months:>6.0f}")
    df = pd.DataFrame(rows)
    df.to_csv(HERE / "results" / "lucid50k_2stage_matrix.csv", index=False)
    best = df.loc[df["net"].idxmax()]
    print("-" * 76)
    print(f"最优组合: 考核 r{int(best['ch'])} + 资金号 r{int(best['fu'])} → "
          f"净 ${best['net']:,.0f} (月净 ${best['net'] / best['months']:,.0f})")
    print("基准: r14/r14 净 $54,517 (月净 $598) | q2/q2 净 $38,706 (月净 $424)")


if __name__ == "__main__":
    main()
