# -*- coding: utf-8 -*-
"""
prop_scale_compare.py  (propfirm/)
===================================
「扩大账号数量, 复制交易, 利润会提高吗?」—— 三种放大方式的同敞口对比。

三种方式的总风险敞口相同 (每笔总风险 = 3 × q2单笔风险):
  A  1×$50K q=2            基线
  B  3×$50K q=2 完全复制    数学上 = 3×A (同信号同参数 → 路径 100% 相关, 同过同爆)
  C  1×$150K q=6 等比规格   dd/target/cap/费用 ×3 (LucidFlex $150K 实际规格
                           按等比假设, 拿到后台真实数字后改参数重跑)
  D  1×$50K q=6 裸加手数    敞口 ×3 但回撤带仍 $2,000 —— 展示为什么这是坏主意

用法: python prop_scale_compare.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import prop_sim as ps  # noqa: E402
import prop_full_journey as fj  # noqa: E402

Q = 2


def run(m: pd.DataFrame, q: int, firm: str, fee: dict, **ch) -> dict:
    day_pnl, _, _, n = ps._daily_arrays(m, q, fj.MULT)
    dates = sorted(m["date"].unique())
    return fj.run_journey(day_pnl, dates, q, firm, fee, **ch)


def summarize(res: dict, label: str) -> dict:
    j = pd.DataFrame(res["journeys"])
    j["days"] = j["end"] - j["start"] + 1
    ch, fu = j[j["state"] == "考核"], j[j["state"] == "funded"]
    total = fu["payout"].sum()
    fees = res["fees"]
    print(f"{label:<28} payout ${total:>10,.0f} | 费用 ${fees:>8,.0f} | "
          f"净 ${total - fees:>10,.0f} | 考核 {len(ch)} 轮(过 "
          f"{len(ch[ch['result'] == '通过'])}) | funded {len(fu)} 轮 | "
          f"考核占用 {ch['days'].sum():>4.0f} 天")
    return dict(payout=total, fees=fees, net=total - fees)


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    print("同一历史路径 (2019 起), 总敞口均为「3 × q2 单笔风险」的四种放大方式:\n")

    # A: 基线
    a = summarize(run(m, 2, "LucidFlex $50K", fj.FEE_MODELS["LucidFlex $50K"]),
                  "A  1×$50K q=2 (基线)")

    # B: 3×复制 = 3×A (同信号同参数, 路径 100% 相关 —— 直接乘, 无需重跑)
    print(f"{'B  3×$50K 完全复制':<28} payout ${a['payout'] * 3:>10,.0f} | "
          f"费用 ${a['fees'] * 3:>8,.0f} | 净 ${a['net'] * 3:>10,.0f} | "
          f"= 3×A, 同过同爆, 时间线与 A 完全同步")

    # C: 1×$150K 等比规格 (q=6, dd/target/cap/费用 ×3) —— LucidFlex $150K
    # 实际规格按等比假设; 拿到后台真实数字后改 FEE/参数重跑
    fj.FUNDED_SCENARIOS = dict(fj.FUNDED_SCENARIOS)
    fj.FUNDED_SCENARIOS["LucidFlex $150K 等比"] = dict(
        payout_every=7, payout_frac=0.5, payout_cap=9000.0,
        min_win_days=0, win_day=0.0)
    fee150 = dict(kind="per_attempt", per=276.0, name="LucidFlex $150K 等比")
    c = summarize(run(m, 6, "LucidFlex $150K 等比", fee150,
                      ch_dd=6000.0, ch_target=9000.0, ch_daily_loss=3600.0),
                  "C  1×$150K q=6 等比规格")

    # D: 1×$50K q=6 (裸加手数, 带宽不变)
    d = summarize(run(m, 6, "LucidFlex $50K", fj.FEE_MODELS["LucidFlex $50K"]),
                  "D  1×$50K q=6 裸加手数")

    print(f"\n对比 (基线 A 净 = ${a['net']:,.0f}):")
    print(f"  B/A = {(a['net'] * 3) / a['net']:.2f}  C/A = {c['net'] / a['net']:.2f}  "
          f"D/A = {d['net'] / a['net']:.2f}")
    print("  → 等比放大 (B/C) 严格线性; 裸加手数 (D) 用同敞口换到更少的联合带宽, "
          "结构上最差。")
    print("  注: C 的 $150K 规格 (dd6000/目标9000/cap9000/费$276/日亏3600) 是等比假设, "
          "以 Lucid 后台为准。")


if __name__ == "__main__":
    main()
