# -*- coding: utf-8 -*-
"""
orb_reverse2.py  (GLM_working/)
================================
假突破(fake breakout)确认后反向 —— 比"闭眼反向"更高质的镜像。

规则:
  区间 = 9:00-9:29 六根 5min 高/低。
  突破 = 9:30-10:10 第一根收盘越界 (上行: close>区间高; 下行: close<区间低)。
  确认 = 突破后 (到 11:00 前) 第一根**收盘回到区间内** → 判定为假突破。
  入场 = 确认 bar 收盘价, 方向反向。
  止损 = 假突破过程中的极值 ± 缓冲 (扫描)  或  入场 ± k×区间宽。
  目标 = 区间对面; 未及则收盘平。
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
MULT = 2.0

T_RANGE = (pd.Timestamp("1970-01-01 09:00").time(), pd.Timestamp("1970-01-01 09:29").time())
T_WIN = (pd.Timestamp("1970-01-01 09:30").time(), pd.Timestamp("1970-01-01 10:10").time())
T_CONF = pd.Timestamp("1970-01-01 11:00").time()   # 确认窗口截止


def load_days():
    df = pd.read_parquet(ETH_PARQUET).tz_convert(ET).loc["2019-01-01":"2026-08-30"]
    return {d.date(): g for d, g in df.groupby(df.index.normalize())}


def load_orb():
    t = pd.read_csv(ORB_TRADES)
    t["date"] = pd.to_datetime(t["entry_time_et"]).dt.date
    t["pts"] = t["pnl_usd"] / t["qty"] / MULT
    return t.groupby("date")["pts"].sum().to_dict()


def fake_breakout(grp, stop_mode="extreme", k=0.5):
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
    # 突破 bar
    brk = None
    for ts, row in win.iterrows():
        c = float(row["close"])
        if c > r_hi:
            brk = (ts, "up"); break
        if c < r_lo:
            brk = (ts, "down"); break
    if brk is None:
        return None
    bts, side = brk
    # 确认窗口内找收盘回区间内的 bar
    seg = grp[(grp.index > bts) & (t <= T_CONF)]
    if len(seg) == 0:
        return None
    conf = None
    extreme = None
    for ts, row in seg.iterrows():
        c = float(row["close"])
        if side == "up":
            extreme = max(extreme, float(row["high"])) if extreme is not None else float(row["high"])
            if c < r_hi:
                conf = (ts, c, extreme); break
        else:
            extreme = min(extreme, float(row["low"])) if extreme is not None else float(row["low"])
            if c > r_lo:
                conf = (ts, c, extreme); break
    if conf is None:
        return None
    cts, entry, extreme = conf
    after = grp[grp.index > cts]
    if len(after) == 0:
        return None
    if side == "up":   # 假突破上行 → 反向做空
        stop = max(entry + 0.5, extreme) if stop_mode == "extreme" else entry + max(k * rw, 0.5)
        target = r_lo
        exit_px, reason = float(after["close"].iloc[-1]), "eod"
        for h, l in zip(after["high"].to_numpy(), after["low"].to_numpy()):
            if h >= stop:
                exit_px, reason = stop, "stop"; break
            if l <= target:
                exit_px, reason = target, "target"; break
        pnl = entry - exit_px
    else:
        stop = min(entry - 0.5, extreme) if stop_mode == "extreme" else entry - max(k * rw, 0.5)
        target = r_hi
        exit_px, reason = float(after["close"].iloc[-1]), "eod"
        for h, l in zip(after["high"].to_numpy(), after["low"].to_numpy()):
            if l <= stop:
                exit_px, reason = stop, "stop"; break
            if h >= target:
                exit_px, reason = target, "target"; break
        pnl = exit_px - entry
    return dict(side=side, pnl=pnl, stop_pts=abs(stop - entry),
                target_pts=abs(entry - target), reason=reason, rw=rw, conf_at=str(cts))


def stats(pts):
    n = len(pts)
    gross = pts[pts > 0].sum(); loss = -pts[pts < 0].sum()
    return dict(n=n, win=100*(pts > 0).mean(), pf=gross/loss if loss else float("inf"),
                mean=pts.mean(), total=pts.sum(),
                dd=(np.cumsum(pts)-np.maximum.accumulate(np.cumsum(pts))).min())


def sharpe(d):
    return d.mean()/d.std()*np.sqrt(252) if d.std() > 0 else 0


def main():
    days = load_days(); orb = load_orb()
    dates = sorted(days)
    orb_pts = np.array([orb.get(d, 0.0) for d in dates])
    s_o = sharpe(orb_pts)
    print(f"交易日 {len(dates)} | ORB Sharpe {s_o:.2f}")
    print("=" * 96)
    for stop_mode, ks in (("extreme", [None]), ("range_frac", [0.25, 0.5, 1.0])):
        for k in ks:
            tag = stop_mode + ("" if k is None else f" k={k}")
            fp = np.zeros(len(dates)); meta = []
            for i, d in enumerate(dates):
                r = fake_breakout(days[d], stop_mode, k or 0.0)
                if r is not None:
                    fp[i] = r["pnl"]; meta.append(r)
            m = stats(fp[fp != 0])
            corr = float(np.corrcoef(orb_pts, fp)[0, 1])
            s_f = sharpe(fp)
            best = (0.0, s_o)
            for w in np.arange(0, 2.01, 0.1):
                sc = sharpe(orb_pts + w * fp)
                if sc > best[1]:
                    best = (w, sc)
            print(f"[{tag:<16}] 笔数 {m['n']:>4} | 胜率 {m['win']:>5.1f}% | PF {m['pf']:>5.2f} | "
                  f"均 {m['mean']:>+6.2f}pt | 总 {m['total']:>+8.1f}pt | 回撤 {m['dd']:>8.1f}pt")
            print(f"               ρ(ORB) = {corr:+.3f} | fade Sharpe {s_f:>5.2f} | "
                  f"最优组合 w={best[0]:.1f} → Sharpe {best[1]:.2f}")
    # 出场分布 (extreme)
    print("\n假突破[extreme] 出场分布:")
    meta = [fake_breakout(days[d], "extreme", 0.0) for d in dates]
    meta = [x for x in meta if x is not None]
    rs = pd.Series([x["reason"] for x in meta]).value_counts()
    for r, v in rs.items():
        sub = [x["pnl"] for x in meta if x["reason"] == r]
        print(f"  {r:<8} {v:>4} 笔 | 合计 {sum(sub):>+8.1f}pt | 均 {np.mean(sub):>+6.2f}pt")
    print(f"  平均止损 {np.mean([x['stop_pts'] for x in meta]):.2f}pt | "
          f"平均目标 {np.mean([x['target_pts'] for x in meta]):.2f}pt | "
          f"区间宽 {np.mean([x['rw'] for x in meta]):.2f}pt")
    # 年度
    yr = {}
    for d in dates:
        r = fake_breakout(days[d], "extreme", 0.0)
        if r is not None:
            yr.setdefault(d.year, []).append(r["pnl"])
    print("年度 (extreme):")
    for y in sorted(yr):
        v = yr[y]
        print(f"  {y}: {len(v):>3}笔 总 {sum(v):>+7.1f}pt 均 {np.mean(v):>+6.2f}pt "
              f"胜率 {100*np.mean([x>0 for x in v]):.0f}%")


if __name__ == "__main__":
    main()
