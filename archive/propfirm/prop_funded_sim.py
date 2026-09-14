# -*- coding: utf-8 -*-
"""
prop_funded_sim.py  (propfirm/)
===============================
考核只是门票 —— 这个脚本回答: 「通过之后的资金号(funded), 用这套策略能拿多少钱?
稳定吗?」

funded 阶段与考核阶段的本质区别:
  - 没有 target, 持续交易, 定期 payout 落袋;
  - payout 后余额重置回起始值, trailing 地板同步重置 → 生存能力周期性刷新
    (这是 funded 比考核更容易长期存活的核心机制: 利润定期落袋, 地板永远
    不会漂到贴近当前权益);
  - 爆了 = 重新考核(时间+费用), 所以看的指标是 payout 流的期望与稳定性。

模拟口径 (参数化, 2026-09 规则来自官网/帮助中心, 用前请再核对):
  Topstep XFA $50K   : EOD trailing DD $2000 (地板 trail 到起始余额即冻结);
                       payout = 超出起始利润的 50%, 单次 cap $5,000, 分成 90%;
                       节奏: 自上次 payout 起 ≥3 个 $150+ 净赢利日 (Standard 路径
                       首次 5 个, 稳态按 3 建模) + ≥14 天周期;
                       payout 后余额回起始、地板重置。
  LucidFlex  $50K    : EOD DD, 无 consistency, payout = 利润 50% cap $3,000,
                       分成 90%, 无窗口 (按 7 天周期建模), payout 后重置。

手数: q 固定 (与考核一致的 q=2/q=3)。两家 $50K 档的手数上限 (Topstep scaling /
Lucid 10:1 micro scaling) 都远高于 q=3, 不构成约束。
窗口: 2019 起主线逐笔 (用户指定近几年口径); 每个历史交易日 = 一次「刚通过考核
进入 funded」的起点; 走到爆仓或样本尾 (寿命右删失)。

用法: python prop_funded_sim.py
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

MULT = 2.0
DD = 2000.0
SPLIT = 0.90

FUNDED_SCENARIOS = {
    "Topstep XFA $50K": dict(payout_every=14, payout_frac=0.5, payout_cap=5000.0,
                             min_win_days=3, win_day=150.0),
    "LucidFlex $50K":   dict(payout_every=7,  payout_frac=0.5, payout_cap=3000.0,
                             min_win_days=0, win_day=0.0),
}
QTY_LADDER = (2, 3)


def simulate_funded(day_pnl: np.ndarray, n_days: int, q: int, sc: dict,
                    stride: int = 1) -> pd.DataFrame:
    """逐起点跑 funded 生命周期。返回每起点的 payout 流摘要。"""
    n_starts = n_days
    rows = []
    for s in range(0, n_starts, stride):
        eq = 0.0
        floor = -DD
        peak = 0.0
        frozen = False
        days_since_payout = 0
        win_days_since = 0
        payouts = []          # (起点相对日, 实际到手 $ = 提取额×分成)
        blew_day = None
        for d in range(s, n_days):
            eq += day_pnl[d]
            days_since_payout += 1
            if day_pnl[d] >= sc["win_day"] > 0:
                win_days_since += 1
            if not frozen:
                peak = max(peak, eq)
                floor = max(floor, peak - DD)
                if floor >= 0:          # 地板 trail 到起始余额即冻结 (相对口径 0)
                    frozen = True
            if eq <= floor:
                blew_day = d - s + 1
                break
            if eq > 0 and days_since_payout >= sc["payout_every"] \
                    and win_days_since >= sc["min_win_days"]:
                take = min(eq * sc["payout_frac"], sc["payout_cap"])
                payouts.append(take * SPLIT)
                eq -= take
                floor = -DD             # payout 后余额/地板重置
                peak = 0.0
                frozen = False
                days_since_payout = 0
                win_days_since = 0
        life_days = blew_day if blew_day else n_days - s
        months = life_days / 21.0
        tot = float(np.sum(payouts))
        rows.append(dict(
            start=s, blew=blew_day is not None,
            life_days=life_days,
            n_payouts=len(payouts),
            total_payout=tot,
            monthly_payout=tot / months if months > 0 else 0.0,
        ))
    return pd.DataFrame(rows)


def monthly_flow(day_pnl: np.ndarray, n_days: int, q: int, sc: dict,
                 starts: list[int]) -> pd.DataFrame:
    """聚合 payout 到相对月 (每起点自己的第 N 个月), 看稳定性。

    零 payout 月占比的分母只含「活到该月」的起点 (爆仓月之后不计入)。
    """
    pay_rows, alive_rows = [], []
    for s in starts:
        eq = 0.0
        floor = -DD
        peak = 0.0
        frozen = False
        days_since_payout = 0
        win_days_since = 0
        month_payout = {}
        blew = False
        for d in range(s, n_days):
            eq += day_pnl[d]
            days_since_payout += 1
            if day_pnl[d] >= sc["win_day"] > 0:
                win_days_since += 1
            if not frozen:
                peak = max(peak, eq)
                floor = max(floor, peak - DD)
                if floor >= 0:
                    frozen = True
            if eq <= floor:
                blew = True
                break
            if eq > 0 and days_since_payout >= sc["payout_every"] \
                    and win_days_since >= sc["min_win_days"]:
                take = min(eq * sc["payout_frac"], sc["payout_cap"])
                mth = (d - s) // 21
                month_payout[mth] = month_payout.get(mth, 0.0) + take * SPLIT
                eq -= take
                floor = -DD
                peak = 0.0
                frozen = False
                days_since_payout = 0
                win_days_since = 0
        last_m = ((d - s) // 21) if blew else ((n_days - 1 - s) // 21)
        pay_rows.append(month_payout)
        alive_rows.append(last_m)
    max_m = max(alive_rows) + 1
    mat = pd.DataFrame([pd.Series(r).reindex(range(max_m)) for r in pay_rows])
    alive = pd.DataFrame([[m <= a for m in range(max_m)] for a in alive_rows],
                         columns=range(max_m))
    med, zshare = [], []
    for m in range(max_m):
        sub = mat[m][alive[m]]
        med.append(float(sub.median()) if len(sub) else 0.0)
        zshare.append(float((sub == 0).mean()) if len(sub) else float("nan"))
    return pd.DataFrame({"month": range(max_m), "median": med, "zero_share": zshare})


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    print("funded 阶段模拟 (2019 起, 每个历史交易日 = 一次刚通过的起点)")
    out_rows = []
    for sc_name, sc in FUNDED_SCENARIOS.items():
        for q in QTY_LADDER:
            day_pnl, day_risk, has_trade, n = ps._daily_arrays(m, q, MULT)
            res = simulate_funded(day_pnl, n, q, sc)
            res.insert(0, "scenario", sc_name)
            res.insert(1, "qty", q)
            out_rows.append(res)
            surv = res[~res["blew"]]
            blew = res[res["blew"]]
            print(f"\n=== {sc_name} × q={q} ===")
            print(f"  存活(到样本尾仍在跑): {(~res['blew']).mean() * 100:.1f}% | "
                  f"爆仓: {res['blew'].mean() * 100:.1f}% | "
                  f"爆仓者中位寿命 {blew['life_days'].median():.0f} 交易日")
            print(f"  月均 payout (到手 90%): 中位 ${res['monthly_payout'].median():,.0f} | "
                  f"均值 ${res['monthly_payout'].mean():,.0f} | "
                  f"P10 ${res['monthly_payout'].quantile(0.1):,.0f} | "
                  f"P90 ${res['monthly_payout'].quantile(0.9):,.0f}")
            print(f"  单次 payout 金额中位: "
                  f"${(res[res['n_payouts'] > 0]['total_payout'].sum()
                       / max(1, res['n_payouts'].sum()) if len(res) else 0):,.0f}"
                  f" (总额/次数)")
            withp = res[res["n_payouts"] > 0]
            if len(withp):
                print(f"  有 payout 记录的起点: {len(withp)} / {len(res)} | "
                      f"人均 payout 次数 {withp['n_payouts'].median():.0f} | "
                      f"人均总额 ${withp['total_payout'].median():,.0f}")
            # 前 12 个月的月度流 (代表性: 全起点中位)
            starts = list(range(0, n - 252, 5))
            mf = monthly_flow(day_pnl, n, q, sc, starts)
            first12 = mf.head(12)
            print(f"  第1-12个月 payout 中位: "
                  + " ".join(f"${v:,.0f}" for v in first12["median"]))
            print(f"  零 payout 月占比(按月): "
                  + " ".join(f"{v * 100:.0f}%" for v in first12["zero_share"]))
    print("\n注: 寿命右删失 (存活到样本尾的起点, 真实总 payout 会更高); "
          "月 = 21 交易日。")
    pd.concat(out_rows, ignore_index=True).to_csv(
        HERE / "results" / "funded_sim_2019.csv", index=False)
    print("明细已存 results/funded_sim_2019.csv")


if __name__ == "__main__":
    main()
