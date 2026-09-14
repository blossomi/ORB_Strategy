# -*- coding: utf-8 -*-
"""
prop_sim.py  (propfirm/)
========================
把 ORB v8.4 的逐笔交易序列放进 prop firm 考核规则里, 回答一个问题:
  「给定固定手数 q, 在 (利润目标 / 回撤带宽度 / trailing 冻结 / 日亏上限 /
   consistency 规则) 下, 历史路径上有多少比例的『挑战尝试』能通过?」

目标函数已经变了:
  - 回测主线最大化长期复利 (年化/MDD)。
  - prop 考核是竞速: 在一条窄回撤带里, 先碰到目标(+consistency达标) = 通过,
    先碰到回撤带/日亏上限 = 爆。
  - 所以这里看的指标是 P(通过) 和 通过所需天数, 不是年化。

规则形态 (全部参数化, 使用时替换成目标 firm 的真实数字):
  - dd$         : 回撤带宽度 (相对起始权益)
  - target$     : 利润目标
  - freeze$     : trailing 冻结线 —— 权益达到 start+freeze$ 后地板不再上移
                  (主流 EOD trailing firm = +DD, 冻结后地板 ≈ start)
  - daily_loss$ : 当日亏损上限 —— 当日从日初最大回撤(含盘中 -1.05R 近似)超限即爆
  - consistency : 单日利润/总利润 上限 (0.5 = 50%) —— 触及目标但单日占比超标时,
                  继续交易等稀释 (地板继续 trailing), 占比达标那天才算通过
  - mode        : 'eod'      地板只按日收盘校验 (EOD trailing 型 firm / 乐观下界)
                  'intraday' 有交易的日子额外校验「日初权益 - 1.05×当日1R」
                  (实时 trailing 型 firm 的悲观近似; 对 EOD firm 是过度悲观的上界)

手数口径: 固定手数 q (考核账号不复利)。每笔美元盈亏 = 每手美元盈亏 × q。
  - 主线 CSV: pnl_usd/qty = 每手实际盈亏 (含成本), 直接缩放。
  - 引擎变体 CSV: pnl_pc (每手净盈亏, 已扣佣金+滑点)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_CAP = 120          # 单次尝试最长 120 个交易日 (~6 个月) 记为超时
INTRADAY_HEADROOM = 1.05        # 实时 trailing 近似: 持仓日盘中最低探到 -1.05R


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------
def load_trades_mainline(csv_path: str) -> pd.DataFrame:
    """主线 orb_backtes_v8_4 产物: pnl_usd/qty = 每手实际盈亏 (含成本)。"""
    df = pd.read_csv(csv_path)
    df["date"] = pd.to_datetime(df["entry_time_et"]).dt.date
    df["pnl_pc"] = df["pnl_usd"] / df["qty"]
    df["stop_pt"] = df["stop_dist_pt"]
    df["r"] = df["r_multiple"]
    return df[["date", "pnl_pc", "stop_pt", "r", "exit_reason"]]


def load_trades_engine(csv_path: str, multiplier: float = 2.0) -> pd.DataFrame:
    """引擎变体 (prop_be_variants.py / prop_fixed_stop.py 产出)。"""
    df = pd.read_csv(csv_path)
    df["r"] = df["pnl_pc"] / (df["stop_pt"] * multiplier)
    return df[["date", "pnl_pc", "stop_pt", "r", "exit_reason"]]


def summarize_series(df: pd.DataFrame, title: str) -> None:
    """序列画像: 连亏 / 累计 R 回撤 / 日分布 —— 决定考核难度的三个原生参数。"""
    day = df.groupby("date")["r"].sum()
    cum = day.cumsum()
    dd_R = float((cum - cum.cummax()).min())
    signs = (df["r"] > 0).to_numpy()
    streak = best = 0
    for s in signs:
        streak = 0 if s else streak + 1
        best = max(best, streak)
    print(f"\n[{title}]")
    print(f"  笔数 {len(df)} | 有交易日 {len(day)} | 每笔均R {df['r'].mean():+.3f} | "
          f"胜率 {(df['r'] > 0).mean() * 100:.1f}%")
    print(f"  日R: P95 {day.quantile(0.95):+.2f} / 最大 {day.max():+.2f} / "
          f"最差 {day.min():+.2f} | 最大连亏 {best} 笔 | 累计R最大回撤 {dd_R:.1f}R")


# ---------------------------------------------------------------------------
# 考核模拟
# ---------------------------------------------------------------------------
def _daily_arrays(df: pd.DataFrame, q: int, mult: float):
    """按交易日索引聚合: 日盈亏($, q 手) / 日1R风险($, q 手) / 是否有交易。"""
    dates = sorted(df["date"].unique())
    pos = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    day_pnl = np.zeros(n)
    day_risk = np.zeros(n)
    has_trade = np.zeros(n, dtype=bool)
    for d, grp in df.groupby("date"):
        i = pos[d]
        day_pnl[i] = float(grp["pnl_pc"].sum()) * q
        # 日风险 = 当日止损距离 × 乘数 × q (赢家盘中也先探 -1R, 用同一口径)
        day_risk[i] = float(grp["stop_pt"].max()) * mult * q
        has_trade[i] = True
    return day_pnl, day_risk, has_trade, n


def simulate_attempts(day_pnl: np.ndarray, day_risk: np.ndarray,
                      has_trade: np.ndarray, n_days: int,
                      dd: float, target: float, freeze: float,
                      mode: str = "eod", cap: int = TRADING_DAYS_CAP,
                      stride: int = 1, daily_loss: float = None,
                      consistency: float = None) -> dict:
    """逐起点跑考核路径。

    daily_loss   : 当日亏损上限 ($) —— 当日从日初的最大回撤(含盘中 -1.05R 近似)超限即爆
    consistency  : 单日利润/总利润 上限 (0.5 = 50%) —— 触及目标但单日占比超标时,
                   继续交易等稀释 (地板继续 trailing), 占比达标那天才算通过
    返回: 通过率(含consistency) / 触标率(不看consistency) / 爆仓率 / 超时率 / 天数。
    """
    n_starts = max(0, n_days - cap)
    outcomes = np.full(max(n_starts, 1), -1, dtype=np.int8)   # 1=过 0=爆 -1=超时
    days_to = np.zeros(max(n_starts, 1))
    dd_used = np.zeros(max(n_starts, 1))
    consist = np.zeros(max(n_starts, 1))
    hit_target = np.zeros(max(n_starts, 1), dtype=bool)

    for s in range(0, n_starts, stride):
        eq = 0.0                    # 相对起始权益
        floor = -dd
        peak = 0.0
        frozen = False
        best_day = 0.0
        min_eq = 0.0
        done = False
        for d in range(s, min(s + cap, n_days)):
            day_start = eq
            eq += day_pnl[d]
            min_eq = min(min_eq, eq)
            best_day = max(best_day, day_pnl[d])
            # 实时 trailing 悲观校验: 持仓日盘中先探 -1.05×当日1R
            worst = eq
            if mode == "intraday" and has_trade[d]:
                worst = min(worst, day_start - min(day_risk[d] * INTRADAY_HEADROOM, dd))
            if not frozen:
                peak = max(peak, eq, day_start)
                floor = max(floor, peak - dd)
                if peak >= freeze:
                    frozen = True
            if worst <= floor or eq <= floor:
                outcomes[s] = 0
                days_to[s] = d - s + 1
                done = True
                break
            if daily_loss is not None and day_start - worst >= daily_loss:
                outcomes[s] = 0
                days_to[s] = d - s + 1
                done = True
                break
            if eq >= target:
                hit_target[s] = True
                if consistency is None or best_day <= consistency * eq:
                    outcomes[s] = 1
                    days_to[s] = d - s + 1
                    dd_used[s] = min(1.0, max(0.0, (min_eq + dd) / dd))
                    consist[s] = best_day / eq if eq > 0 else 0
                    done = True
                    break
                # 单日占比超标: 继续交易等稀释 (不 break, 地板继续 trailing)
        if not done:
            outcomes[s] = -1
            days_to[s] = cap

    passed, blew = outcomes == 1, outcomes == 0
    return dict(
        pass_rate=passed.mean() * 100,
        target_hit_rate=hit_target.mean() * 100,
        blow_rate=blew.mean() * 100,
        timeout_rate=(outcomes == -1).mean() * 100,
        days_median=float(np.median(days_to[passed])) if passed.any() else float("nan"),
        days_p90=float(np.percentile(days_to[passed], 90)) if passed.any() else float("nan"),
        dd_use_median=float(np.median(dd_used[passed])) if passed.any() else float("nan"),
        consist_max=float(np.max(consist[passed])) if passed.any() else float("nan"),
    )


# ---------------------------------------------------------------------------
# 场景批量跑
# ---------------------------------------------------------------------------
# 主口径: 用户提供的真实目标 firm (2026-09-13)
REAL_FIRM = dict(
    name="目标firm $50K EOD: DD2000/目标3000/日亏1200/cons50%/冻结+DD",
    dd=2000, target=3000, freeze=2000, daily_loss=1200, consistency=0.5)
# 对照: 同 firm 但全程 trailing (冻结线=+目标) —— freeze 规则需对照 firm 文档确认
REAL_FIRM_FULLTRAIL = dict(
    name="目标firm同参数,全程trailing(冻结=+3000)",
    dd=2000, target=3000, freeze=3000, daily_loss=1200, consistency=0.5)
# 对照: Apex 形态 (宽带/冻结早/无 consistency) —— 之前结论的锚点
APEX_LIKE = dict(
    name="对照 Apex形: DD2500/目标3000/冻结+100/无consistency",
    dd=2500, target=3000, freeze=100, daily_loss=None, consistency=None)

DEFAULT_SCENARIOS = {s["name"]: s for s in (REAL_FIRM, REAL_FIRM_FULLTRAIL, APEX_LIKE)}
QTY_LADDER = (1, 2, 3, 5, 8)


def prop_run(df: pd.DataFrame, mult: float, qty_ladder=QTY_LADDER,
             scenarios=DEFAULT_SCENARIOS, modes=("eod", "intraday"),
             stride: int = 1) -> pd.DataFrame:
    rows = []
    for sc_name, sc in scenarios.items():
        for q in qty_ladder:
            day_pnl, day_risk, has_trade, n = _daily_arrays(df, q, mult)
            for mode in modes:
                r = simulate_attempts(day_pnl, day_risk, has_trade, n,
                                      dd=sc["dd"], target=sc["target"],
                                      freeze=sc["freeze"], mode=mode, stride=stride,
                                      daily_loss=sc.get("daily_loss"),
                                      consistency=sc.get("consistency"))
                rows.append(dict(scenario=sc_name, qty=q, mode=mode, **r))
    return pd.DataFrame(rows)


def print_table(res: pd.DataFrame, mode: str) -> None:
    sub = res[res["mode"] == mode]
    print(f"\n=== mode={mode} "
          f"({'EOD trailing 乐观下界' if mode == 'eod' else '实时 trailing 悲观上界'}) ===")
    for sc in sub["scenario"].unique():
        s = sub[sub["scenario"] == sc]
        print(f"\n  {sc}")
        print("   q | 通过% | 触标% | 爆%  | 超时% | 通过天数中位/P90 | 回撤带占用")
        for _, r in s.iterrows():
            print(f"   {int(r['qty']):<2}| {r['pass_rate']:5.1f} | {r['target_hit_rate']:5.1f} "
                  f"| {r['blow_rate']:4.1f} | {r['timeout_rate']:5.1f} | "
                  f"{r['days_median']:6.0f} /{r['days_p90']:5.0f}      | "
                  f"{r['dd_use_median'] * 100:5.1f}%")
