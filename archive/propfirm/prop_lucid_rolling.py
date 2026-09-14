# -*- coding: utf-8 -*-
"""
prop_lucid_rolling.py  (propfirm/)
===================================
用 Lucid 官方帮助中心的真实规则重跑「LucidFlex $50K 单账号连续路径」, 并在最近
3-4 年历史数据上做滚动回测 (只操作一个号, 不 copy)。

与 prop_full_journey.py 的规则差异 (官方口径, support.lucidtrading.com, 2026-09 抓取):
  1. MLL 地板 = max(-$2,000, min(最高收盘余额 - $2,000, +$100)): 越过 Initial Trail
     Balance ($52,100) 后永久锁在 $50,100 (= 起始+$100), 不是锁在起始 (0)。
  2. payout 后地板不回落。官方原文: "Once you request a payout from LucidFlex, your MLL
     automatically adjusts to the Locked MLL Balance" → 提款后地板固定在 +$100。
     旧模型每次 payout 把地板重置回 -$2,000 (= 一个全新缓冲), 把提款当成风控;
     实际提款是在消耗缓冲 (余额降、地板不降)。
  3. payout 条件 = 本 cycle 有 5 个「单日利润 ≥ $150」的日子 + cycle 净利为正;
     最低请求 $500; 单次上限 = cycle 利润的 50% 且 ≤ $2,000
     (旧模型: cap $3,000、每 7 天一提、无盈利日要求)。
  4. 每个账号最多 5 次 payout, 之后转移 LucidLive 审查 (旧模型: 全程无限提款)。
  5. 考核期 consistency 50% (最大单日利润 / 账户利润), 与旧模型一致。
  6. LucidFlex 无 DLL (可选, 影响价格不影响风险) → 不建模。
  7. 手数上限: 考核 40 micro; funded 按 scaling plan 随利润档 20/30/40 micro。
  8. 费用: 新号 $140 (DLL-ON 档标价), 考核爆掉重置 $95; 旧模型一律 $92。

以损定仓: 1R = $2,000 / 生存笔数, q_d = floor(1R / (当日止损pt × $2))。

用法:
  ../.venv/bin/python prop_lucid_rolling.py            # 全矩阵 + 组合 + 滚动
  ../.venv/bin/python prop_lucid_rolling.py --quick    # 只跑两组组合 + 滚动
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import prop_sim as ps  # noqa: E402

MULT = 2.0
DD = 2000.0
TARGET = 3000.0
TRAIL_LOCK = DD + 100.0          # Initial Trail Balance 相对起始 = +$2,100
LOCKED_FLOOR = 100.0             # 锁定后地板 = 起始 + $100
CONS = 0.5
SPLIT = 0.90

FU_WIN_DAY = 150.0               # $50K: 单日利润 ≥ $150 才算一个合格日
FU_MIN_WIN_DAYS = 5
FU_PAYOUT_MIN = 500.0
FU_PAYOUT_FRAC = 0.5
FU_PAYOUT_CAP = 2000.0
FU_MAX_PAYOUTS = 5               # 每号上限, 之后转移 LucidLive 审查
FU_SCALE = ((1000.0, 20), (2000.0, 30), (float("inf"), 40))   # 利润档 → 手数上限 (micro)
EVAL_MAX_Q = 40                  # 考核 4 mini = 40 micro

FEE_NEW = 140.0                  # 新考核号 (DLL-ON 档标价; 促销价另算)
FEE_RESET = 95.0                 # 已爆考核号重置价
FEE_LEGACY = 92.0                # 旧模型口径 (对比用)
LEGACY_PAYOUT_EVERY = 7


@dataclass
class Series:
    dates: list
    pnl_pc: np.ndarray
    stop_pt: np.ndarray
    _q: dict = field(default_factory=dict)

    def q(self, tier: int) -> np.ndarray:
        """r=tier → 1R = 2000/tier, q_d = floor(1R/(stop_d×$2)), 最少 1 手。"""
        if tier not in self._q:
            r1 = DD / tier
            self._q[tier] = np.maximum(1, np.floor(r1 / (self.stop_pt * MULT))).astype(int)
        return self._q[tier]


def load_series() -> Series:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    g = m.groupby("date").agg(pnl_pc=("pnl_pc", "sum"), stop_pt=("stop_pt", "max"))
    g = g.reset_index()
    return Series(g["date"].tolist(), g["pnl_pc"].to_numpy(), g["stop_pt"].to_numpy())


def cap_funded_q(base_q: int, profit: float) -> int:
    for th, mq in FU_SCALE:
        if profit < th:
            return min(base_q, mq)
    return base_q


# ---------------------------------------------------------------------------
# 单账号状态机
# ---------------------------------------------------------------------------
def run_account(s: Series, i0: int, i1: int, ch_t: int, fu_t: int,
                rules: str = "official", payout_policy: str = "max",
                buffer_floor_after_payout: float = 0.0,
                fu_max_payouts: int = FU_MAX_PAYOUTS,
                fu_payout_cap: float = FU_PAYOUT_CAP,
                fu_win_days_req: int = FU_MIN_WIN_DAYS,
                fu_floor_reset: bool = False,
                fee_new: float = FEE_NEW, fee_reset: float = FEE_RESET) -> dict:
    """从 i0 跑到 i1 (两端含), 单账号连续路径 (考核↔funded 状态机)。

    rules='official' → 官方规则; 'legacy' → 旧模型 (payout 后地板重置 -2000、cap $3000、
        7 天节奏无盈利日要求、无限次提款、$92/次)。
    payout_policy='max' → 条件一满就提到上限; 'buffer' → 提款后剩余缓冲
        (余额 - 锁定地板) < buffer_floor_after_payout 时先不提。
    """
    legacy = rules == "legacy"
    q_ch_all, q_fu_all = s.q(ch_t), s.q(fu_t)
    pnl_pc, dates = s.pnl_pc, s.dates

    state, bal = "eval", 0.0
    floor, peak, frozen = -DD, 0.0, False
    best_day = 0.0
    n_payouts, bal_at_last, win_days, days_since = 0, 0.0, 0, LEGACY_PAYOUT_EVERY
    fees = 0.0
    fee_log, payouts, rounds = [], [], []
    blown_eval = blown_fu = passed = live = 0
    seg_start, seg_payouts = i0, 0.0

    def charge(amount: float, idx: int, kind: str) -> None:
        nonlocal fees
        fees += amount
        fee_log.append((dates[idx], kind, amount))

    charge(FEE_LEGACY if legacy else fee_new, i0, "考核号")

    def reset_eval(idx: int, fee: float, kind: str) -> None:
        nonlocal state, bal, floor, peak, frozen, best_day, seg_start, seg_payouts
        nonlocal n_payouts, bal_at_last, win_days, days_since
        charge(fee, idx, kind)
        state, bal, floor, peak, frozen, best_day = "eval", 0.0, -DD, 0.0, False, 0.0
        n_payouts, bal_at_last, win_days, days_since = 0, 0.0, 0, LEGACY_PAYOUT_EVERY
        seg_start, seg_payouts = idx, 0.0

    for d in range(i0, i1 + 1):
        if state == "eval":
            q = min(int(q_ch_all[d]), EVAL_MAX_Q)
            day = q * pnl_pc[d]
            bal += day
            best_day = max(best_day, day)
            peak = max(peak, bal)
            if legacy:
                if not frozen:
                    floor = max(floor, peak - DD)
                    if peak >= DD:
                        frozen = True
            else:
                floor = max(-DD, min(peak - DD, LOCKED_FLOOR))
            if bal <= floor:
                rounds.append(dict(state="考核", start=seg_start, end=d, result="爆",
                                   payout=seg_payouts))
                blown_eval += 1
                reset_eval(d, fee_reset, "考核重置")
                continue
            if bal >= TARGET and best_day <= CONS * bal:
                rounds.append(dict(state="考核", start=seg_start, end=d, result="通过",
                                   payout=0.0))
                passed += 1
                state, bal, floor, peak, frozen, best_day = "funded", 0.0, -DD, 0.0, False, 0.0
                n_payouts, bal_at_last, win_days, days_since = 0, 0.0, 0, 0
                seg_start, seg_payouts = d + 1, 0.0
            continue

        # ---- funded ----
        base_q = int(q_fu_all[d])
        q = base_q if legacy else cap_funded_q(base_q, max(bal, 0.0))
        day = q * pnl_pc[d]
        bal += day
        days_since += 1
        if day >= FU_WIN_DAY:
            win_days += 1
        peak = max(peak, bal)
        if legacy:
            if not frozen:
                floor = max(floor, peak - DD)
                if floor >= 0:
                    frozen = True
        else:
            floor = max(-DD, min(peak - DD, LOCKED_FLOOR))
            if n_payouts > 0 and not fu_floor_reset:   # 官方: 提款后地板 = 锁定地板 (+$100)
                floor = max(floor, LOCKED_FLOOR)
            elif n_payouts > 0 and fu_floor_reset:     # 旧假设: 提款后地板重置回 -$2,000
                floor, peak = -DD, 0.0
        if bal <= floor:
            rounds.append(dict(state="funded", start=seg_start, end=d, result="爆",
                               payout=seg_payouts))
            blown_fu += 1
            if n_payouts >= FU_MAX_PAYOUTS and not legacy:
                live += 1
            reset_eval(d, FEE_LEGACY if legacy else fee_new, "新考核号")
            continue

        if legacy:
            take = min(max(bal, 0.0) * FU_PAYOUT_FRAC, 3000.0)
            if bal > 0 and days_since >= LEGACY_PAYOUT_EVERY:
                payouts.append((dates[d], take * SPLIT, take))
                seg_payouts += take * SPLIT
                bal -= take
                floor, peak, frozen = -DD, 0.0, False
                n_payouts += 1
                bal_at_last, days_since = bal, 0
            continue

        cycle_profit = bal - bal_at_last
        take = min(cycle_profit * FU_PAYOUT_FRAC, fu_payout_cap)
        if take < FU_PAYOUT_MIN or cycle_profit <= 0 or win_days < fu_win_days_req:
            continue
        if n_payouts >= fu_max_payouts:
            continue
        if payout_policy == "buffer" and (bal - take) - LOCKED_FLOOR < buffer_floor_after_payout:
            continue
        payouts.append((dates[d], take * SPLIT, take))
        seg_payouts += take * SPLIT
        bal -= take
        bal_at_last, win_days, days_since = bal, 0, 0
        n_payouts += 1
        if n_payouts >= fu_max_payouts:        # 满 5 次 → 转移 LucidLive 审查
            rounds.append(dict(state="funded", start=seg_start, end=d,
                               result="满5次提款", payout=seg_payouts))
            live += 1
            reset_eval(d, fee_new, "新考核号")

    rounds.append(dict(state="考核" if state == "eval" else "funded", start=seg_start,
                       end=i1, result="进行中", payout=seg_payouts))
    taken = sum(p[1] for p in payouts)
    return dict(payouts=payouts, rounds=rounds, fees=fees, net=taken - fees,
                taken=taken, blown_eval=blown_eval, blown_fu=blown_fu, passed=passed,
                live=live, fee_log=fee_log)


def summarize(res: dict, dates: list, i0: int, i1: int) -> dict:
    j = pd.DataFrame(res["rounds"])
    months = (pd.Timestamp(dates[i1]) - pd.Timestamp(dates[i0])).days / 30.44
    ch = j[j["state"] == "考核"]
    fu = j[j["state"] == "funded"]
    return dict(net=res["net"], months=months, monthly_net=res["net"] / months,
                payouts=len(res["payouts"]), taken=res["taken"], fees=res["fees"],
                ch_rounds=len(ch), ch_pass=int((ch["result"] == "通过").sum()),
                ch_blow=int((ch["result"] == "爆").sum()),
                fu_rounds=len(fu), fu_blow=res["blown_fu"], live=res["live"],
                end_state=res["rounds"][-1]["state"], end_result=res["rounds"][-1]["result"])


def _tag(r) -> str:
    if r.state == "考核":
        return "C✓" if r.result == "通过" else "C✗" if r.result == "爆" else "C·"
    return "F★" if r.result == "满5次提款" else "F✓" if r.result == "进行中" else "F✗"


def print_journey(res: dict, dates: list, tag: str) -> None:
    j = pd.DataFrame(res["rounds"])
    j["days"] = j["end"] - j["start"] + 1
    tl = " ".join(f"{_tag(r)}({r.days})" for r in j.itertuples())
    print(f"    时间线 {tag}: {tl}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()

    s = load_series()
    n = len(s.dates)
    dates = s.dates
    print("=" * 110)
    print("LucidFlex $50K 单账号 · 官方规则重跑 (prop_lucid_rolling.py)")
    print("=" * 110)
    print(f"数据: {len(s.pnl_pc)} 笔 (= {n} 个交易日), {dates[0]} → {dates[-1]}; "
          f"逐笔每手净盈亏/止损pt 来自 ORB v8.4 (MNQ $2/点, 含滑点+佣金)")
    print("规则(官方): MLL $2,000 EOD trailing, 越过 +$2,100 后锁在起始+$100; 目标 $3,000; "
          "考核 cons 50%; LucidFlex 无 DLL")
    print(f"提款: 每 cycle 需 {FU_MIN_WIN_DAYS} 个 ≥${FU_WIN_DAY:.0f} 盈利日 + 正净利; "
          f"请求 ≥${FU_PAYOUT_MIN:.0f}; 单次 ≤ min(cycle利润×50%, ${FU_PAYOUT_CAP:.0f}); "
          f"每号 ≤{FU_MAX_PAYOUTS} 次 → 之后移 LucidLive")
    print(f"费用: 新号 ${FEE_NEW:.0f} / 爆号重置 ${FEE_RESET:.0f}  (旧模型一律 ${FEE_LEGACY:.0f})")

    print("\n[0] 手数与「合格日」画像 (以损定仓 q_d = floor(1R/(stop_d×$2))):")
    print(f"{'档':>5} | {'1R$':>5} | {'q中位':>5} | {'qP90':>5} | {'qmax':>5} | "
          f"{'q>20天':>7} | {'q>40天':>7} | {'单日≥$150':>9} | {'占比':>7}")
    for t in (27, 20, 14, 10, 7):
        q = s.q(t)
        day = q * s.pnl_pc
        print(f"r{t:<4} | {DD/t:>5.0f} | {np.median(q):>5.0f} | {np.percentile(q,90):>5.0f} | "
              f"{q.max():>5} | {(q>20).sum():>7} | {(q>40).sum():>7} | "
              f"{(day>=FU_WIN_DAY).sum():>9} | {(day>=FU_WIN_DAY).mean()*100:>6.1f}%")
    print("  注: q>20 天 = funded scaling plan 会砍手数的天数 (利润 <$1,000 → 上限 20 micro)")

    # ---- 全样本矩阵: 官方 vs 旧模型 ----
    mats = {}
    for rules in ("official", "legacy"):
        rows = []
        for ct in (14, 10, 7, 5):
            for ft in (27, 20, 14, 10, 7):
                if args.quick and (ct, ft) not in ((14, 7), (7, 14)):
                    continue
                r = run_account(s, 0, n - 1, ct, ft, rules=rules)
                rows.append(dict(rules=rules, ch=ct, fu=ft, **summarize(r, dates, 0, n - 1)))
        df = pd.DataFrame(rows)
        df["combo"] = df.apply(lambda r: f"考{int(r.ch)}/资{int(r.fu)}", axis=1)
        mats[rules] = df
        top = df.sort_values("net", ascending=False).head(8)
        print(f"\n[1] 全样本连续单账号路径 {dates[0]} → {dates[-1]} "
              f"({'旧模型 README §7.6' if rules=='legacy' else '官方规则'}):")
        print("-" * 110)
        print(f"{'组合':>8} | {'净$':>9} | {'月净$':>7} | {'提款':>4} | {'费用$':>7} | "
              f"{'考核轮':>6} | {'过':>3} | {'爆':>3} | {'funded轮':>7} | {'爆':>3} | "
              f"{'满5转live':>8} | 期末")
        for _, r in top.iterrows():
            print(f"{r['combo']:>8} | {r['net']:>9,.0f} | {r['monthly_net']:>7,.0f} | "
                  f"{r['payouts']:>4} | {r['fees']:>7,.0f} | {r['ch_rounds']:>6} | "
                  f"{r['ch_pass']:>3} | {r['ch_blow']:>3} | {r['fu_rounds']:>7} | "
                  f"{r['fu_blow']:>3} | {r['live']:>8} | {r['end_state']}/{r['end_result']}")
    mats["official"].to_csv(HERE / "results" / "lucid_rolling_official_matrix.csv", index=False)
    mats["legacy"].to_csv(HERE / "results" / "lucid_rolling_legacy_matrix.csv", index=False)

    # ---- 重点组合 ----
    print("\n[2] 重点组合明细 (官方 vs 旧模型, 全样本):")
    for ct, ft, tag in ((14, 7, "考核 r14 + 资金 r7 (你的选择)"),
                        (7, 14, "考核 r7 + 资金 r14 (README §7.6 最优)"),
                        (14, 14, "单手数 r14/r14"),
                        (7, 7, "全程 r7")):
        ro = run_account(s, 0, n - 1, ct, ft, rules="official")
        rl = run_account(s, 0, n - 1, ct, ft, rules="legacy")
        so, sl = summarize(ro, dates, 0, n - 1), summarize(rl, dates, 0, n - 1)
        shrink = 100 * (1 - so["net"] / sl["net"]) if sl["net"] else float("nan")
        print(f"\n  ▸ {tag}")
        print(f"    官方规则: 净 ${so['net']:>9,.0f} (月净 ${so['monthly_net']:>6,.0f}) | "
              f"提款 {so['payouts']} 次 / 到手 ${so['taken']:,.0f} | 费用 ${so['fees']:,.0f}")
        print(f"              考核 {so['ch_rounds']} 轮 (过 {so['ch_pass']} / 爆 {so['ch_blow']})"
              f" | funded {so['fu_rounds']} 轮 (爆 {so['fu_blow']}, 满5次转移 {so['live']})"
              f" | 期末 {so['end_state']}/{so['end_result']}")
        print(f"    旧模型  : 净 ${sl['net']:>9,.0f} (月净 ${sl['monthly_net']:>6,.0f}) | "
              f"提款 {sl['payouts']} 次 | 费用 ${sl['fees']:,.0f}  → 官方规则缩水 {shrink:.0f}%")
        print_journey(ro, dates, "(官方)")
        print_journey(rl, dates, "(旧模型)")

    # ---- 滚动回测 ----
    def idx_from(year: int, month: int) -> int:
        for i, d in enumerate(dates):
            if (d.year, d.month) >= (year, month):
                return i
        return n - 1

    print("\n[3] 滚动回测: 单账号, 不 copy, 官方规则 —— 每个交易日当作一个「买入起点」, "
          "跑到 2026-08-28:")
    rows_out = []
    for ct, ft in ((14, 7), (7, 14), (14, 14)):
        for start_ym, span in (((2022, 9), "近4年"), ((2023, 9), "近3年")):
            i0 = idx_from(*start_ym)
            recs = [summarize(run_account(s, i, n - 1, ct, ft, rules="official"),
                              dates, i, n - 1) for i in range(i0, n - 1)]
            w = pd.DataFrame(recs)
            months = (pd.Timestamp(dates[-1]) - pd.Timestamp(dates[i0])).days / 30.44
            print(f"\n  ▸ 考{ct}/资{ft} · {span} ({dates[i0]} → {dates[-1]}, "
                  f"{len(w)} 个起点, 窗口 {months:.1f} 月):")
            print(f"    净: 中位 ${w['net'].median():>8,.0f} | 均值 ${w['net'].mean():>8,.0f} | "
                  f"P10 ${w['net'].quantile(0.1):>8,.0f} | P90 ${w['net'].quantile(0.9):>8,.0f}")
            print(f"    负收益起点 {100*(w['net'] < 0).mean():5.1f}% | 零提款起点 "
                  f"{100*(w['payouts'] == 0).mean():5.1f}% | 提款次数中位 {w['payouts'].median():.0f} "
                  f"(P90 {w['payouts'].quantile(0.9):.0f}) | 费用中位 ${w['fees'].median():,.0f} | "
                  f"期末仍在 funded {100*(w['end_state'] == 'funded').mean():.1f}%")
            rows_out.append(dict(combo=f"考{ct}/资{ft}", span=span, starts=len(w),
                                 months=months, net_med=w["net"].median(),
                                 net_mean=w["net"].mean(),
                                 net_p10=w["net"].quantile(0.1),
                                 net_p90=w["net"].quantile(0.9),
                                 neg_share=(w["net"] < 0).mean() * 100,
                                 zero_pay_share=(w["payouts"] == 0).mean() * 100,
                                 pay_med=w["payouts"].median(),
                                 ch_med=w["ch_rounds"].median(),
                                 fee_med=w["fees"].median(),
                                 alive_share=(w["end_state"] == "funded").mean() * 100))
    pd.DataFrame(rows_out).to_csv(HERE / "results" / "lucid_rolling_recent.csv", index=False)

    # ---- 固定 12 个月窗口 ----
    print("\n[4] 固定 12 个月窗口滚动 (近 4 年, 每 5 个交易日一个起点, 官方规则):")
    i0 = idx_from(2022, 9)
    for ct, ft in ((14, 7), (7, 14)):
        recs = []
        for i in range(i0, n, 5):
            j1 = min(i + 252, n - 1)
            if j1 - i < 220:
                continue
            recs.append(summarize(run_account(s, i, j1, ct, ft, rules="official"),
                                  dates, i, j1))
        w = pd.DataFrame(recs)
        print(f"  考{ct}/资{ft}: {len(w)} 个窗口 | 净中位 ${w['net'].median():,.0f} "
              f"(均值 ${w['net'].mean():,.0f}, P10 ${w['net'].quantile(.1):,.0f} ~ "
              f"P90 ${w['net'].quantile(.9):,.0f}) | 负收益窗口 {100*(w['net'] < 0).mean():.1f}% "
              f"| 零提款窗口 {100*(w['payouts'] == 0).mean():.1f}% | "
              f"提款中位 {w['payouts'].median():.0f} 次")

    # ---- 提款策略对比 ----
    print("\n[5] 提款策略对比 (官方规则, 全样本, 考14/资7):")
    print(f"{'策略':<30} | {'净$':>9} | {'提款':>4} | {'funded爆':>7} | {'费用$':>7} | 期末")
    print("-" * 80)
    for pol, buf, tag in (("max", 0.0, "条件满足就提满 (README 纪律)"),
                          ("buffer", 1000.0, "提款后缓冲 <$1,000 不提"),
                          ("buffer", 2000.0, "提款后缓冲 <$2,000 不提"),
                          ("buffer", 4000.0, "提款后缓冲 <$4,000 不提")):
        r = run_account(s, 0, n - 1, 14, 7, rules="official",
                        payout_policy=pol, buffer_floor_after_payout=buf)
        sm = summarize(r, dates, 0, n - 1)
        print(f"{tag:<30} | {sm['net']:>9,.0f} | {sm['payouts']:>4} | {sm['fu_blow']:>7} | "
              f"{sm['fees']:>7,.0f} | {sm['end_state']}/{sm['end_result']}")
    print("\n明细已存 results/lucid_rolling_official_matrix.csv、lucid_rolling_legacy_matrix.csv、"
          "lucid_rolling_recent.csv")


    # ---- 规则消融: 官方规则里哪一条在吃掉旧数字 ----
    print("\n[6] 规则消融 (考14/资7, 全样本): 逐条把官方规则放回旧假设, 看每条的代价")
    print(f"{'变体':<44} | {'净$':>9} | {'提款':>4} | {'funded爆':>7} | {'费用$':>7}")
    print("-" * 86)
    variants = [
        ("官方全套规则 ($140/$95 费)", {}),
        ("官方规则 + 旧费用 $92", dict(fee_new=92.0, fee_reset=92.0)),
        ("官方规则但去掉 5 次提款上限", dict(fu_max_payouts=10 ** 6)),
        ("官方规则但单次 cap 回到 $3,000", dict(fu_payout_cap=3000.0)),
        ("官方规则但去掉「5 个 ≥$150 日」", dict(fu_win_days_req=0)),
        ("官方规则但 payout 后地板重置 -$2,000", dict(fu_floor_reset=True)),
        ("旧模型全套 (README §7.6)", dict(rules="legacy")),
    ]
    ab_rows = []
    for tag, kw in variants:
        r = run_account(s, 0, n - 1, 14, 7, **kw)
        sm = summarize(r, dates, 0, n - 1)
        print(f"{tag:<44} | {sm['net']:>9,.0f} | {sm['payouts']:>4} | {sm['fu_blow']:>7} | "
              f"{sm['fees']:>7,.0f}")
        ab_rows.append(dict(variant=tag, **sm))
    pd.DataFrame(ab_rows).to_csv(HERE / "results" / "lucid_rolling_ablation.csv", index=False)

    # ---- 单账号连续路径: 从最近 3/4 年的某个起点开始跑一遍 ----
    print("\n[7] 单账号连续路径样本 (考14/资7, 官方规则): 从起点一路跑到样本尾")
    for ym, tag in (((2022, 9), "2022-09 起"), ((2023, 9), "2023-09 起"),
                    ((2024, 9), "2024-09 起"), ((2025, 9), "2025-09 起")):
        i0 = idx_from(*ym)
        r = run_account(s, i0, n - 1, 14, 7, rules="official")
        sm = summarize(r, dates, i0, n - 1)
        print(f"\n  ▸ {tag} ({dates[i0]} → {dates[-1]}, {sm['months']:.1f} 月): "
              f"净 ${sm['net']:,.0f} (月净 ${sm['monthly_net']:,.0f}) | "
              f"提款 {sm['payouts']} 次 / 到手 ${sm['taken']:,.0f} | 费用 ${sm['fees']:,.0f}")
        print(f"    考核 {sm['ch_rounds']} 轮 (过 {sm['ch_pass']} / 爆 {sm['ch_blow']}) | "
              f"funded {sm['fu_rounds']} 轮 (爆 {sm['fu_blow']}, 满5次转移 {sm['live']}) | "
              f"期末 {sm['end_state']}/{sm['end_result']}")
        print_journey(r, dates, f"({tag})")


if __name__ == "__main__":
    main()
