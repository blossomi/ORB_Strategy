# -*- coding: utf-8 -*-
"""
prop_quarterly.py  (propfirm/)
==============================
把单路径全程模拟 (prop_full_journey.py) 的逐笔现金流按**季度**分解:
  - payout (到手 90%)
  - 账号成本 (考核费 / reset 费 / 考核月费 —— 即「买号/养号」的全部现金成本)
  - 净现金流
  - 阶段状态 (当季主要在考核还是资金号)

口径: LucidFlex q=2 为主, Topstep q=2 对照。成本参数在 prop_full_journey.FEE_MODELS。
注: 「卖号成本」这里按账号获取成本 (考核费) 口径; 若指把账号变现卖掉 (违反
firm 条款, 不建模) 或提款手续费 (Lucid 无), 另说。

用法: python prop_quarterly.py
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


def quarterly(m: pd.DataFrame, firm: str, q: int) -> pd.DataFrame:
    fee = fj.FEE_MODELS[firm]
    day_pnl, _, _, n = ps._daily_arrays(m, q, fj.MULT)
    dates = sorted(m["date"].unique())
    res = fj.run_journey(day_pnl, dates, q, firm, fee)
    led = pd.DataFrame(res["ledger"], columns=["date", "type", "amount"])
    led["date"] = pd.to_datetime(led["date"])
    led["quarter"] = led["date"].dt.to_period("Q").astype(str)

    # 阶段状态: 每季度考核/资金号占用天数
    j = pd.DataFrame(res["journeys"])
    j["days"] = j["end"] - j["start"] + 1
    stage = []
    for _, r in j.iterrows():
        d0, d1 = pd.Timestamp(dates[r["start"]]), pd.Timestamp(dates[r["end"]])
        stage.append(dict(quarter=None, state=r["state"], days=r["days"],
                          d0=d0, d1=d1))
    # 按季度拆段天数
    q_days = {}
    for s in stage:
        cur_d = s["d0"]
        while cur_d <= s["d1"]:
            qk = f"{cur_d.year}Q{(cur_d.month - 1) // 3 + 1}"
            q_days.setdefault(qk, dict(考核=0, funded=0))[s["state"]] += 1
            cur_d += pd.Timedelta(days=1)

    piv = led.pivot_table(index="quarter", columns="type", values="amount",
                          aggfunc="sum").fillna(0.0)
    for col in ("payout", "考核费", "reset费", "考核月费", "激活费"):
        if col not in piv:
            piv[col] = 0.0
    # 补全无现金流但仍在考核/资金号的季度 (纯考核期没有 ledger 条目)
    all_q = [str(p) for p in pd.period_range("2019Q1", "2026Q3", freq="Q")]
    piv = piv.reindex(all_q, fill_value=0.0)
    piv["账号成本"] = piv["考核费"] + piv["reset费"] + piv["考核月费"] + piv["激活费"]
    piv["净现金流"] = piv["payout"] - piv["账号成本"]
    piv["考核天"] = [q_days.get(qk, {}).get("考核", 0) for qk in piv.index]
    piv["资金号天"] = [q_days.get(qk, {}).get("funded", 0) for qk in piv.index]
    piv = piv.reset_index().rename(columns={"index": "quarter"})
    return piv[["quarter", "payout", "考核费", "激活费", "reset费", "考核月费",
                "账号成本", "净现金流", "考核天", "资金号天"]]


def show(piv: pd.DataFrame, title: str) -> None:
    print(f"\n=== {title} ===")
    print(f"{'季度':<8} | {'payout':>8} | {'考核费':>6} {'激活':>6} {'reset':>5} {'月费':>5} "
          f"| {'账号成本':>7} | {'净现金流':>8} | {'考核天':>4} {'资金号天':>5}")
    print("-" * 78)
    for _, r in piv.iterrows():
        flag = " ←考核期" if r["资金号天"] == 0 and r["考核天"] > 30 else ""
        print(f"{r['quarter']:<8} | {r['payout']:>8,.0f} | {r['考核费']:>6.0f} {r['激活费']:>6.0f} "
              f"{r['reset费']:>5.0f} {r['考核月费']:>5.0f} | {r['账号成本']:>7,.0f} "
              f"| {r['净现金流']:>8,.0f} | {r['考核天']:>4} {r['资金号天']:>5}{flag}")
    tot = piv[["payout", "考核费", "激活费", "reset费", "考核月费", "账号成本",
               "净现金流"]].sum()
    print("-" * 78)
    print(f"{'合计':<8} | {tot['payout']:>8,.0f} | {tot['考核费']:>6.0f} {tot['激活费']:>6.0f} "
          f"{tot['reset费']:>5.0f} {tot['考核月费']:>5.0f} | {tot['账号成本']:>7,.0f} "
          f"| {tot['净现金流']:>8,.0f} |")
    neg = piv[piv["净现金流"] < 0]
    print(f"负现金流季度: {len(neg)} 个 "
          f"({', '.join(neg['quarter']) if len(neg) else '无'}) | "
          f"最大单季回吐 -${-neg['净现金流'].min():,.0f}" if len(neg) else "")


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    p1 = quarterly(m, "LucidFlex $50K", 2)
    show(p1, "LucidFlex $50K × q=2 (主口径, 2019Q1 → 2026Q3)")
    p1.to_csv(HERE / "results" / "quarterly_lucidflex_q2.csv", index=False)
    p2 = quarterly(m, "Topstep XFA $50K", 2)
    show(p2, "Topstep XFA $50K × q=2 (对照)")
    p2.to_csv(HERE / "results" / "quarterly_topstep_q2.csv", index=False)
    print("\n注: 单一历史路径, 含路径运气; 费用为量级参数 (见 prop_full_journey.FEE_MODELS); "
          "季度按日历季, 天数为日历天 (含周末), 两者加总 > 交易日数属正常。")


if __name__ == "__main__":
    main()
