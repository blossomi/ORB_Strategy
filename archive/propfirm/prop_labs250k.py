# -*- coding: utf-8 -*-
"""
prop_labs250k.py  (propfirm/)
=============================
Topstep Labs $250K Freedom Combine 详细测试:
  模式 A: 以损定仓 + ATR 7.5%×14日止损 —— q_d = floor(权益×risk% / (stop_d×$2)),
          每笔美元风险恒定 (1R = 权益×risk%), 手数随波动反比
  模式 B: 固定手数 + 固定 20pt 止损 —— 1R = q×$40 恒定
  两族按「生存线」配对 (带宽/1R): 27 笔 / 14 笔 / 5.7 笔 三档, 平均敞口逐档对齐。

考核: MLL $10,000 EOD trailing / 目标 $15,000 / DLL $5,000 / consistency 55% /
      90 天 ≈ 63 交易日 / $499 一次性无 reset。
funded: XFA 首次 payout 前 MLL $10,000 trailing, 首提后 MLL=$0 (只剩 DLL);
        payout = 满 5 个 ≥$1,000 盈利日 + 14 天节奏 (用户口径), 50% 利润 cap
        $25,000, 90% 分成, payout 后(首提后)无 MLL 可重置。
对照: LucidFlex $50K q=2 (已有结果, §4/§7), 5 个号完全复制 = ×5。

用法: python prop_labs250k.py
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
DD = 10000.0
TARGET = 15000.0
DLL = 5000.0
CONS = 0.55
CAP_DAYS = 63                  # 90 日历天 ≈ 63 交易日
FEE = 499.0
SPLIT = 0.90
FU = dict(win_day=1000.0, min_win_days=5, payout_every=14,
          payout_frac=0.5, payout_cap=25000.0)

# 三档风险: (标签, A族 risk%, B族 q) —— 同生存线配对
PAIRS = [("生存27笔", 0.00148, 9), ("生存14笔", 0.0028, 18), ("生存5.7笔", 0.007, 44)]


def daily_base(m: pd.DataFrame) -> pd.DataFrame:
    """按日聚合: 每手净盈亏合计 / 当日止损点数 (单笔/日)。"""
    g = m.groupby("date").agg(pnl_pc=("pnl_pc", "sum"), stop_pt=("stop_pt", "max"))
    return g.reset_index()


def daily_arrays_A(base: pd.DataFrame, risk_frac: float, equity0: float = 250_000.0):
    """模式 A: 每日 q_d = floor(权益×risk% / (stop_d×$2)) (考核期权益恒定)。"""
    risk_d = equity0 * risk_frac
    q = np.maximum(1, np.floor(risk_d / (base["stop_pt"] * MULT).to_numpy())).astype(int)
    day_pnl = q * base["pnl_pc"].to_numpy()
    day_risk = q * base["stop_pt"].to_numpy() * MULT
    return day_pnl, day_risk, q, risk_d


def daily_arrays_B(base20: pd.DataFrame, q_fixed: int):
    day_pnl = q_fixed * base20["pnl_pc"].to_numpy()
    day_risk = np.full(len(base20), q_fixed * 20.0 * MULT)
    return day_pnl, day_risk


def funded_250k(base: pd.DataFrame, mode: str, risk_frac: float = 0.0,
                q_fixed: int = 0, n_starts: int = None) -> pd.DataFrame:
    """funded 生命周期: 首提前 MLL $10,000 trailing; 首提后 MLL=$0 只剩 DLL;
    payout = 满 5 个 ≥$1,000 盈利日 + ≥14 天, 50% 利润 cap $25,000, 90% 到手。
    模式 A 权益随盈亏/提款变化 (以损定仓 q_d 重算); 模式 B 固定手数。"""
    dates = pd.to_datetime(base["date"]).tolist()
    pnl_pc = base["pnl_pc"].to_numpy()
    stop_pt = base["stop_pt"].to_numpy()
    n = len(base)
    starts = range(0, (n_starts or n))
    rows = []
    for s in starts:
        equity = 250_000.0
        floor = -DD                      # 相对权益 (rel = equity - 250000 - 已提)
        peak = 0.0
        rel = 0.0
        days_since_payout = 0
        win_days = 0
        first_payout_done = False
        payouts = []
        blew_day = None
        for d in range(s, n):
            if mode == "A":
                q = max(1, int(equity * risk_frac / (stop_pt[d] * MULT)))
            else:
                q = q_fixed
            day_pnl = q * pnl_pc[d]
            day_loss = q * stop_pt[d] * MULT
            rel += day_pnl
            equity += day_pnl
            days_since_payout += 1
            if pnl_pc[d] > 0 and day_pnl >= FU["win_day"]:
                win_days += 1
            # DLL 每日校验 (当日从日初的回撤, 近似 = 当日止损额×1.05)
            if day_loss * 1.05 >= DLL:
                blew_day = d - s + 1
                break
            if not first_payout_done:
                peak = max(peak, rel)
                floor = max(floor, peak - DD)
                if rel <= floor:
                    blew_day = d - s + 1
                    break
            if rel > 0 and days_since_payout >= FU["payout_every"] \
                    and win_days >= FU["min_win_days"]:
                take = min(rel * FU["payout_frac"], FU["payout_cap"])
                payouts.append(take * SPLIT)
                rel -= take
                equity -= take
                days_since_payout = 0
                win_days = 0
                first_payout_done = True     # 首提后 MLL=$0
        life = blew_day if blew_day else n - s
        months = life / 21.0
        tot = float(np.sum(payouts))
        rows.append(dict(start=s, blew=blew_day is not None, life_days=life,
                         n_payouts=len(payouts), total_payout=tot,
                         monthly_payout=tot / months if months > 0 else 0.0))
    return pd.DataFrame(rows)


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    base = daily_base(m)
    base20 = daily_base(ps.load_trades_engine(
        str(HERE / "results" / "prop_trades_fixed20pt_2019.csv")))
    n = len(base)

    # ---- [1] 考核矩阵 ----
    print("[1] Labs 250K 考核 (63 交易日窗, MLL 10K/目标 15K/DLL 5K/cons55%):")
    print(f"{'模式':<24} | {'平均1R$':>7} | {'通过%':>5} {'爆%':>5} {'未决%':>5} | "
          f"{'E[门票]':>7}")
    print("-" * 76)
    results = {}
    for tag, rf, qb in PAIRS:
        # A 族
        dp, dr, q_a, r1_a = daily_arrays_A(base, rf)
        ht = np.ones(n, dtype=bool)
        rA = ps.simulate_attempts(dp, dr, ht, n, dd=DD, target=TARGET, freeze=DD,
                                  mode="eod", cap=CAP_DAYS, daily_loss=DLL,
                                  consistency=CONS)
        pA = rA["pass_rate"] / 100
        print(f"A {tag} ATR+{rf:.3%}     | {r1_a:>7.0f} | {rA['pass_rate']:5.1f} "
              f"{rA['blow_rate']:5.1f} {100-rA['pass_rate']-rA['blow_rate']:5.1f} | "
              f"${FEE/pA if pA > 0 else float('inf'):>6,.0f}")
        results[f"A-{tag}"] = (pA, r1_a)
        # B 族
        dpB, drB = daily_arrays_B(base20, qb)
        rB = ps.simulate_attempts(dpB, drB, ht, n, dd=DD, target=TARGET, freeze=DD,
                                  mode="eod", cap=CAP_DAYS, daily_loss=DLL,
                                  consistency=CONS)
        pB = rB["pass_rate"] / 100
        r1_b = qb * 40.0
        print(f"B {tag} 20pt q={qb:<3}       | {r1_b:>7.0f} | {rB['pass_rate']:5.1f} "
              f"{rB['blow_rate']:5.1f} {100-rB['pass_rate']-rB['blow_rate']:5.1f} | "
              f"${FEE/pB if pB > 0 else float('inf'):>6,.0f}")
        results[f"B-{tag}"] = (pB, r1_b)

    # ---- [2] funded 阶段 (每档最优代表: 生存27/14 档) ----
    print("\n[2] funded 阶段 (首提后 MLL=$0, DLL 5K, 满5个$1000+盈利日+14天, cap 25K):")
    print(f"{'模式':<24} | {'月均中位':>8} {'月均均值':>8} | {'爆%':>5} {'寿命中位':>6} "
          f"| {'爆前总提中位':>10}")
    print("-" * 84)
    fu_rows = {}
    for tag, rf, qb in PAIRS[:2]:
        fuA = funded_250k(base, "A", risk_frac=rf)
        fu_rows[f"A-{tag}"] = fuA
        wA = fuA[fuA["n_payouts"] > 0]
        print(f"A {tag} ATR+{rf:.3%}     | {fuA['monthly_payout'].median():>8,.0f} "
              f"{fuA['monthly_payout'].mean():>8,.0f} | {fuA['blew'].mean()*100:5.1f} "
              f"{fuA['life_days'].median():>6.0f} | "
              f"${wA['total_payout'].median() if len(wA) else 0:>10,.0f}")
        fuB = funded_250k(base20, "B", q_fixed=qb)
        fu_rows[f"B-{tag}"] = fuB
        wB = fuB[fuB["n_payouts"] > 0]
        print(f"B {tag} 20pt q={qb:<3}       | {fuB['monthly_payout'].median():>8,.0f} "
              f"{fuB['monthly_payout'].mean():>8,.0f} | {fuB['blew'].mean()*100:5.1f} "
              f"{fuB['life_days'].median():>6.0f} | "
              f"${wB['total_payout'].median() if len(wB) else 0:>10,.0f}")

    # ---- [3] 经济账 vs 5×LucidFlex $50K ----
    print("\n[3] 经济账对比 (每完整周期, 2019 起期望):")
    print(f"{'方案':<28} | {'E[门票]':>7} | {'funded月均':>9} | {'期望寿命':>7} "
          f"| {'周期净':>8} | {'月净':>7}")
    print("-" * 84)
    # LucidFlex $50K 已有数字 (README §4/§7): 考核 32.2%/E[门票]=92/0.322≈286;
    # funded q=2 月均均值 533、期望寿命均值约 10.4 月
    luc_e = 92 / 0.322
    luc_m, luc_life = 533.0, 218 / 21
    print(f"{'LucidFlex 50K ×1':<28} | ${luc_e:>6,.0f} | ${luc_m:>8,.0f} | "
          f"{luc_life:>6.1f}月 | ${luc_m*luc_life-luc_e:>7,.0f} | "
          f"${(luc_m*luc_life-luc_e)/luc_life:>6,.0f}")
    print(f"{'LucidFlex 50K ×5 (复制)':<28} | ${luc_e*5:>6,.0f} | ${luc_m*5:>8,.0f} | "
          f"{luc_life:>6.1f}月 | ${(luc_m*luc_life-luc_e)*5:>7,.0f} | "
          f"${(luc_m*luc_life-luc_e)/luc_life*1:>6,.0f}/号")
    for key, fu in fu_rows.items():
        p, r1 = results[key]
        e_fee = FEE / p
        mth = float(fu["monthly_payout"].mean())
        life = float(fu["life_days"].mean()) / 21
        print(f"{key + ' Labs250K':<28} | ${e_fee:>6,.0f} | ${mth:>8,.0f} | "
              f"{life:>6.1f}月 | ${mth*life-e_fee:>7,.0f} | "
              f"${(mth*life-e_fee)/life:>6,.0f}")


if __name__ == "__main__":
    main()
