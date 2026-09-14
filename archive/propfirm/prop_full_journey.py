# -*- coding: utf-8 -*-
"""
prop_full_journey.py  (propfirm/)
=================================
「如果 2019-01-02 从零开始玩 prop 循环游戏, 一直玩到 2026-08, 总账是什么样?」

与 prop_sim / prop_funded_sim 的区别:
  - 那两个是「起点分布」: 每个历史交易日都当一次独立起点, 回答 P(通过)/月均中位;
  - 这个是**单一连续路径**: 2019 年起一段历史只跑一次, 状态机在
    CHALLENGE(考核) → FUNDED(资金号) → 爆 → 重新考核 之间循环到样本尾,
    输出这条路径的完整旅程账本 (通过几轮 / 爆几轮 / payout 总额 / 费用 / 净)。

状态机 (EOD trailing, freeze=+DD, 相对起始权益):
  CHALLENGE: DD2000 / 目标3000 / 日亏1200 / consistency50% (真实 firm 规则)
             通过 → FUNDED; 爆 → 下一交易日重开 (费用+1)
  FUNDED   : Topstep XFA (payout 50% cap5000, ≥3 个 $150+ 赢利日 + 14 天节奏)
             或 LucidFlex (payout 50% cap3000, 7 天节奏); 90% 分成;
             payout 后余额/地板重置; 爆 → 回 CHALLENGE
  样本尾   : 记「进行中」(右删失)

费用模型 (量级参数, 按你实际折扣改): Topstep 考核 $165/月 (按考核阶段的日历月数);
LucidFlex 一次性 $92/次进考核。funded 阶段 0 月费。

用法: python prop_full_journey.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import prop_sim as ps  # noqa: E402
from prop_funded_sim import DD, FUNDED_SCENARIOS, MULT, SPLIT  # noqa: E402

# 免激活路径 = 同一套 XFA funded 规则, 只是付费方式不同
FUNDED_SCENARIOS = dict(FUNDED_SCENARIOS)
FUNDED_SCENARIOS["Topstep XFA $50K 免激活"] = FUNDED_SCENARIOS["Topstep XFA $50K"]

QTY_LADDER = (2, 3)
# 2026-09 官价 (Topstep help center "Pricing and Payment Questions"):
#   Standard $49/月 + 通过时 $149 激活费; 免激活路径 $95/月;
#   每月订阅含 1 次免费 reset → 同月重开计 $0 (模拟无同月双爆);
#   funded 后 0 月费。LucidFlex 按次 ~$92 (促销价, 以后台为准)。
FEE_MODELS = {
    "Topstep XFA $50K": dict(kind="monthly", monthly=49.0, activation=149.0,
                             reset=0.0, name="Topstep Standard"),
    "Topstep XFA $50K 免激活": dict(kind="monthly", monthly=95.0, activation=0.0,
                                    reset=0.0, name="Topstep 免激活"),
    "LucidFlex $50K":   dict(kind="per_attempt", per=92.0, name="LucidFlex $50K"),
}


def run_journey(day_pnl: pd.Series, dates: list, q: int,
                firm: str, fee: dict, ch_dd: float = None, ch_target: float = None,
                ch_daily_loss: float = None, ch_consistency: float = 0.5,
                fu_pnl: np.ndarray = None) -> dict:
    sc = FUNDED_SCENARIOS[firm]
    n = len(day_pnl)
    # 考核阶段参数 (默认真实 firm 口径 = prop_sim.REAL_FIRM; 大账号等比场景可覆盖)
    dd = ch_dd if ch_dd is not None else DD
    funded_dd = dd                # funded 阶段回撤带与考核带同宽 (等比规格假设)
    target = ch_target if ch_target is not None else 3000.0
    daily_loss = ch_daily_loss if ch_daily_loss is not None else 1200.0
    consistency = ch_consistency
    if fu_pnl is None:            # 两阶段异构手数: funded 阶段用独立盈亏序列
        fu_pnl = day_pnl

    state = "CHALLENGE"
    eq = 0.0
    floor = -dd
    peak = 0.0
    frozen = False
    best_day = 0.0
    days_since_payout = 0
    win_days_since = 0

    journeys = []          # 每段 (state, start_i, end_i, result, payout_total)
    cur = dict(state=state, start=0, payouts=[])
    fees = 0.0
    fee_months = set()     # (年,月) 考核月集合 (Topstep 月费按月去重)
    reset_count = 0        # 同月爆掉重开次数 (Topstep reset 费)
    ledger = []            # 逐笔现金流 (日期, 类型, 金额) —— 季度分解用
    if fee["kind"] == "per_attempt":
        fees += fee["per"]          # 初始进考核也要买一次
        ledger.append((dates[0], "考核费", fee["per"]))

    def enter_challenge(after_day: int, ym: tuple) -> None:
        nonlocal state, eq, floor, peak, frozen, best_day, cur, fees, reset_count
        if fee["kind"] == "per_attempt":
            fees += fee["per"]      # 每次重新进考核, 一次性费用
            ledger.append((dates[min(after_day, n - 1)], "考核费", fee["per"]))
        elif ym in fee_months:
            reset_count += 1        # 同月重开: reset 费 (新月的月费由主循环记)
            fees += fee["reset"]
            ledger.append((dates[min(after_day, n - 1)], "reset费", fee["reset"]))
        state, eq, floor, peak, frozen, best_day = \
            "CHALLENGE", 0.0, -dd, 0.0, False, 0.0
        cur = dict(state=state, start=after_day, payouts=[])

    for d in range(n):
        ym = (dates[d].year, dates[d].month)
        if state == "CHALLENGE":
            if fee["kind"] == "monthly" and ym not in fee_months:
                fee_months.add(ym)
                fees += fee["monthly"]
                ledger.append((dates[d], "考核月费", fee["monthly"]))
            day_start = eq
            eq += day_pnl[d]
            best_day = max(best_day, day_pnl[d])
            if not frozen:
                peak = max(peak, eq, day_start)
                floor = max(floor, peak - dd)
                if peak >= dd:      # 冻结线 = +DD (Topstep 形)
                    frozen = True
            if eq <= floor or day_start - eq >= daily_loss:
                journeys.append(dict(state="考核", start=cur["start"], end=d,
                                     result="爆", payout=0.0))
                enter_challenge(d + 1, ym)
                continue
            if eq >= target and best_day <= consistency * eq:
                journeys.append(dict(state="考核", start=cur["start"], end=d,
                                     result="通过", payout=0.0))
                if fee.get("activation"):
                    fees += fee["activation"]       # 每个资金号激活一次
                    ledger.append((dates[d], "激活费", fee["activation"]))
                state, eq, floor, peak, frozen, best_day = \
                    "FUNDED", 0.0, -funded_dd, 0.0, False, 0.0
                days_since_payout, win_days_since = 0, 0
                cur = dict(state=state, start=d + 1, payouts=[])
                continue
        else:  # FUNDED
            eq += fu_pnl[d]
            days_since_payout += 1
            if day_pnl[d] >= sc["win_day"] > 0:
                win_days_since += 1
            if not frozen:
                peak = max(peak, eq)
                floor = max(floor, peak - funded_dd)
                if floor >= 0:
                    frozen = True
            if eq <= floor:
                journeys.append(dict(state="funded", start=cur["start"], end=d,
                                     result="爆", payout=float(sum(cur["payouts"]))))
                enter_challenge(d + 1, ym)
                continue
            if eq > 0 and days_since_payout >= sc["payout_every"] \
                    and win_days_since >= sc["min_win_days"]:
                take = min(eq * sc["payout_frac"], sc["payout_cap"])
                cur["payouts"].append(take * SPLIT)
                ledger.append((dates[d], "payout", take * SPLIT))
                eq -= take
                floor, peak, frozen = -funded_dd, 0.0, False
                days_since_payout, win_days_since = 0, 0

    # 样本尾右删失
    journeys.append(dict(state="考核" if state == "CHALLENGE" else "funded",
                         start=cur["start"], end=n - 1,
                         result="进行中", payout=float(sum(cur["payouts"]))))
    return dict(journeys=journeys, fees=fees, dates=dates, ledger=ledger)


def report(res: dict, firm: str, q: int) -> None:
    dates = res["dates"]
    j = pd.DataFrame(res["journeys"])
    j["days"] = j["end"] - j["start"] + 1
    j["year"] = [dates[s].year for s in j["start"]]

    ch = j[j["state"] == "考核"]
    fu = j[j["state"] == "funded"]
    passed = ch[ch["result"] == "通过"]
    ch_blew = ch[ch["result"] == "爆"]
    fu_blew = fu[fu["result"] == "爆"]
    total_payout = fu["payout"].sum()
    fees = res["fees"]

    print(f"\n=== {firm} × q={q} | 2019-01-02 → 2026-08-30 连续循环 ===")
    print(f"  交易日 {len(dates)} | 考核 {len(ch)} 轮 (通过 {len(passed)} / "
          f"爆 {len(ch_blew)} / 进行中 {len(ch) - len(passed) - len(ch_blew)}) "
          f"| funded {len(fu)} 轮 (爆 {len(fu_blew)})")
    if len(passed):
        print(f"  通过轮考核用时: 中位 {passed['days'].median():.0f} 天 "
              f"(P90 {passed['days'].quantile(0.9):.0f})")
    if len(fu):
        print(f"  funded 寿命: 中位 {fu['days'].median():.0f} 天 | "
              f"最长 {fu['days'].max()} 天")
    print(f"  payout 总额 (90% 到手): ${total_payout:,.0f} | "
          f"考核费用: -${fees:,.0f} | 净: ${total_payout - fees:,.0f}")
    yr = fu.groupby("year")["payout"].sum()
    yr_days = fu.groupby("year")["days"].sum()
    ch_days = ch.groupby("year")["days"].sum()
    print("  年份 | payout | funded天数 | 考核天数")
    for y in sorted(set(yr.index) | set(ch_days.index)):
        print(f"  {y} | ${yr.get(y, 0):>8,.0f} | {yr_days.get(y, 0):>6.0f} "
              f"| {ch_days.get(y, 0):>6.0f}")
    # 时间线 (紧凑)
    tl = "  时间线: " + " ".join(
        f"{'C✓' if r.state == '考核' and r.result == '通过' else
          'C✗' if r.state == '考核' else 'F'}({r.days})" for r in j.itertuples())
    print(tl)


def main() -> None:
    m = ps.load_trades_mainline(str(HERE.parent / "ORB_strategy" / "html_output"
                                    / "v8_4_trades.csv"))
    for firm in FUNDED_SCENARIOS:
        fee = FEE_MODELS[firm]
        for q in QTY_LADDER:
            day_pnl, _, _, n = ps._daily_arrays(m, q, MULT)
            dates = sorted(m["date"].unique())
            res = run_journey(day_pnl, dates, q, firm, fee)
            report(res, firm, q)
    print("\n注: 单一历史路径 (2019 起只跑一次), 结果含路径运气成分; "
          "「任选一天开始」的分布口径见 README §4-§7。费用为量级参数, "
          "Topstep 按考核阶段日历月计, LucidFlex 按次。")


if __name__ == "__main__":
    main()
