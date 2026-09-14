# -*- coding: utf-8 -*-
"""
orb_reverse.py  (GLM_working/)
==============================
案例4: ORB 的反向策略 —— 实体突破后做反向 (fade the breakout)。

回答两个问题:
  1. fade 的逐日盈亏与 ORB 是否负相关 (能不能当对冲腿)?
  2. 反向入场后目标=区间对面, 止损该放哪 (参数扫描)?

信号 (与 ORB 完全同源、同方向触发, 只把方向反过来):
  区间 = 9:00-9:29 六根 5min 的高/低。
  突破 = 9:30-10:10 内第一根「收盘价」越出区间的 bar (与 v8.4 收盘价判断一致)。
  ORB   = 突破方向顺向入场, ATR 止损 + 5R 保本 + 收盘平仓 (直接用 v8_4_trades.csv 的逐日盈亏)。
  FADE  = 突破 bar 收盘价**反向**入场, 止损 = 入场 + k×区间宽 (扫描 k), 目标 = 区间对面, 未及则收盘平。

数据: nq_5min_eth.parquet (ETH 全时段 5min), 2019-01-02 ~ 2026-08-28。
口径: MNQ $2/点, 本脚本以「点」计 (×$2 = 每手美元); 成本暂按 0 (先看相关性, 成本单独再算)。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

ETH_PARQUET = HERE.parent / "ORB_strategy" / "nq_5min_eth.parquet"
ORB_TRADES = HERE.parent / "ORB_strategy" / "html_output" / "v8_4_trades.csv"
ET = "America/New_York"

T_RANGE = (pd.Timestamp("1970-01-01 09:00").time(), pd.Timestamp("1970-01-01 09:29").time())
T_WIN = (pd.Timestamp("1970-01-01 09:30").time(), pd.Timestamp("1970-01-01 10:10").time())
MULT = 2.0   # MNQ $/点


def load_days() -> dict:
    df = pd.read_parquet(ETH_PARQUET)
    df = df.tz_convert(ET)
    df = df.loc["2019-01-01":"2026-08-30"]
    days = {}
    for d, grp in df.groupby(df.index.normalize()):
        days[d.date()] = grp
    return days


def load_orb_daily_points() -> dict:
    t = pd.read_csv(ORB_TRADES)
    t["date"] = pd.to_datetime(t["entry_time_et"]).dt.date
    t["pts"] = t["pnl_usd"] / t["qty"] / MULT          # 每手净盈亏 → 点
    return t.groupby("date")["pts"].sum().to_dict()


def simulate_day(grp: pd.DataFrame, k: float, stop_mode: str = "range_frac"):
    """返回 (触发?, 方向, fade 点数, 止损点数, 目标点数, 出场方式)。
    k: 止损 = 入场 + k×区间宽 (range_frac) 或 突破bar极值 (extreme)。
    返回 None 若无突破。"""
    t = grp.index.time
    rng = grp[(t >= T_RANGE[0]) & (t < T_RANGE[1])]
    if len(rng) == 0:
        return None
    r_hi, r_lo = float(rng["high"].max()), float(rng["low"].min())
    rw = r_hi - r_lo
    if rw <= 0:
        return None

    win = grp[(t >= T_WIN[0]) & (t < T_WIN[1])]
    if len(win) == 0:
        return None
    # 第一根收盘越界的 bar
    brk = None
    for ts, row in win.iterrows():
        c = float(row["close"])
        if c > r_hi:
            brk = (ts, "up", c, float(row["high"]))
            break
        if c < r_lo:
            brk = (ts, "down", c, float(row["low"]))
            break
    if brk is None:
        return None
    ts, side, entry, extreme = brk
    after = grp[grp.index > ts]
    if len(after) == 0:
        return None

    if side == "up":          # 假突破上行 → 反向做空
        if stop_mode == "extreme":
            stop = max(entry + 0.5, extreme)          # 突破bar高点, 至少 2 tick
        else:
            stop = entry + max(k * rw, 0.5)
        target = r_lo
        hi = after["high"].to_numpy()
        lo = after["low"].to_numpy()
        close = float(after["close"].iloc[-1])
        exit_px, reason = close, "eod"
        for h, l in zip(hi, lo):
            if h >= stop:
                exit_px, reason = stop, "stop"
                break
            if l <= target:
                exit_px, reason = target, "target"
                break
        pnl = entry - exit_px            # 空头: 跌了赚
    else:                     # 假突破下行 → 反向做多
        if stop_mode == "extreme":
            stop = min(entry - 0.5, extreme)
        else:
            stop = entry - max(k * rw, 0.5)
        target = r_hi
        hi = after["high"].to_numpy()
        lo = after["low"].to_numpy()
        close = float(after["close"].iloc[-1])
        exit_px, reason = close, "eod"
        for h, l in zip(hi, lo):
            if l <= stop:
                exit_px, reason = stop, "stop"
                break
            if h >= target:
                exit_px, reason = target, "target"
                break
        pnl = exit_px - entry            # 多头: 涨了赚
    return dict(side=side, pnl=pnl, stop_pts=abs(stop - entry),
                target_pts=abs(entry - target), reason=reason, rw=rw)


def stats(pts: np.ndarray) -> dict:
    n = len(pts)
    win = (pts > 0).mean() * 100
    gross = pts[pts > 0].sum()
    loss = -pts[pts < 0].sum()
    pf = gross / loss if loss > 0 else float("inf")
    cum = np.cumsum(pts)
    dd = (cum - np.maximum.accumulate(cum)).min()
    return dict(n=n, win=win, pf=pf, mean=pts.mean(), total=pts.sum(), dd=dd)


def sharpe(daily: np.ndarray) -> float:
    return daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0


def main() -> None:
    days = load_days()
    orb = load_orb_daily_points()
    print(f"数据: {len(days)} 个交易日 (2019-2026), ORB 有交易 {len(orb)} 天")
    print("=" * 96)

    # 逐日 ORB 点数 (对齐到交易日)
    all_dates = sorted(days)
    orb_pts = np.array([orb.get(d, 0.0) for d in all_dates])

    results = {}
    for stop_mode, label in (("range_frac", "k×区间宽"), ("extreme", "突破bar极值")):
        for k in ([0.25, 0.5, 1.0] if stop_mode == "range_frac" else [None]):
            tag = f"{label}" + (f" k={k}" if k else "")
            fade_pts = np.zeros(len(all_dates))
            meta = []
            for i, d in enumerate(all_dates):
                r = simulate_day(days[d], k or 0.0, stop_mode)
                if r is not None:
                    fade_pts[i] = r["pnl"]
                    meta.append(r)
            m = stats(fade_pts[fade_pts != 0])
            corr = float(np.corrcoef(orb_pts, fade_pts)[0, 1])
            s_o = sharpe(orb_pts)
            s_f = sharpe(fade_pts)
            # 组合: ORB + w×fade, 扫 w 取最优
            best = (0.0, s_o)
            for w in np.arange(0.0, 2.01, 0.1):
                sc = sharpe(orb_pts + w * fade_pts)
                if sc > best[1]:
                    best = (w, sc)
            results[tag] = dict(meta=meta, m=m, corr=corr, s_f=s_f, best_w=best[0], best_s=best[1])
            print(f"[{tag:<16}] 笔数 {m['n']:>4} | 胜率 {m['win']:>5.1f}% | PF {m['pf']:>5.2f} | "
                  f"每笔均 {m['mean']:>+6.2f}pt | 总 {m['total']:>+8.1f}pt | 回撤 {m['dd']:>8.1f}pt")
            print(f"               vs ORB 日盈亏 相关系数 ρ = {corr:+.3f} | fade Sharpe {s_f:>5.2f} | "
                  f"ORB Sharpe {s_o:.2f} | 最优组合 w={best[0]:.1f} → Sharpe {best[1]:.2f}")

    # 出场方式分布 (以 k=0.5 为例) + 年度分解
    print("\n出场方式分布 (k=0.5):")
    meta = results["k×区间宽 k=0.5"]["meta"]
    reasons = pd.Series([r["reason"] for r in meta]).value_counts()
    for r, v in reasons.items():
        sub = [x["pnl"] for x in meta if x["reason"] == r]
        print(f"  {r:<8} {v:>4} 笔 | 合计 {sum(sub):>+8.1f}pt | 均 {np.mean(sub):>+6.2f}pt")
    print(f"  平均止损距离 {np.mean([r['stop_pts'] for r in meta]):.2f}pt | "
          f"平均目标距离 {np.mean([r['target_pts'] for r in meta]):.2f}pt | "
          f"平均区间宽 {np.mean([r['rw'] for r in meta]):.2f}pt")

    print("\n年度分解 (fade k=0.5, 点数):")
    yr = {}
    for i, d in enumerate(all_dates):
        r = simulate_day(days[d], 0.5, "range_frac")
        if r is not None:
            yr.setdefault(d.year, []).append(r["pnl"])
    for y in sorted(yr):
        v = yr[y]
        print(f"  {y}: {len(v):>3} 笔 | 总 {sum(v):>+7.1f}pt | 均 {np.mean(v):>+6.2f}pt | "
              f"胜率 {100*np.mean([x>0 for x in v]):.0f}%")


if __name__ == "__main__":
    main()
