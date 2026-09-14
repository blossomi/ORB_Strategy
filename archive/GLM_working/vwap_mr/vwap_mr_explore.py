# -*- coding: utf-8 -*-
"""
vwap_mr_explore.py  (GLM_working/vwap_mr)
=========================================
VWAP 均值回归·上午场 —— 描述统计 + 粗版策略期望。只读数据, 不改任何主线代码。

数据: ORB_strategy/nq_continuous_1m.parquet (2010-06 ~ 2026-08, 1min, UTC), 样本取 2019 起 (主线口径)。

口径
  - VWAP 主锚 = ETH 开盘 18:00 ET (前一日), 即期货平台标准"日内 VWAP"; 敏感性锚 = RTH 9:30 ET。
    session 归属: sess = (ts_ET + 6h).date()  →  18:00 起算次日, RTH 全部归属当日。
  - σ(t) = sqrt(Σ v·tp²/Σv - vwap²)  (成交量加权的 tp 对 vwap 标准差, 标准 VWAP bands)
  - 偏离 dev = (close - vwap) / σ, 单位 σ; 点数偏离另折 ATR14 (前一日, 与 orb_core_v84 同口径)。
  - 描述统计: 窗口内首次 |dev|≥k 后, 价格首次触碰 VWAP 的概率 (窗口结束前 / +1h / 收盘前) 与发生时间。
  - 粗版策略: 窗口内首破 kσ → 反向入场 (bar close 成交), 目标 = 触碰 VWAP (按 VWAP 价成交),
    止损 = entry ± s·σ(入场时), 15:55 前都没触发则按最后 bar 收盘平 (节假日早收自动适配)。
    同一 bar 同时碰止损和目标 → 按止损计 (保守)。成本: MNQ 口径往返 1.0 pt
    (每边 $0.5 佣金 + 1 tick 滑点 = $1.0, ÷ $2/点 = 0.5 pt/边)。
  - 已知乐观假设: 止损按触发价成交、目标按 VWAP 价成交 (与主线回测同一假设, 见 v8.4 体检)。
  - Sharpe: 每日 R 序列 (无交易日=0) 年化 √252, 与主线 ORB 的日线口径 Sharpe 可直接对比;
    t 值 = 笔级 mean/(std/√n), 对照失效监控框架的 t=4.35 停机线。

CLI 参数 (2026-09-15 二轮: 窗口平移 + 改进变体)
  --win-start HH:MM / --win-end HH:MM   入场观察窗口 (默认 10:00 / 11:00)
  --time-stop HH:MM                     时间止损: 到点仍未出场按当根收盘平 (需晚于窗口结束)
  --target vwap|half                    half = 目标改为入场价与 VWAP 的中点 (路径减半)
  --drive-filter X                      趋势日过滤: |close(窗口首根)-open(9:30)| > X×ATR14 的日子不做
  --tag S / --no-save                   CSV 文件名后缀 / 只打印不写文件

输出: 终端报告 + results/vwap_mr_{reversion,grid,byyear}[_tag].csv
"""
from __future__ import annotations

import argparse
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import zoneinfo

HERE = Path(__file__).resolve().parent
ORB_DIR = HERE.parent.parent / "ORB_strategy"
RESULTS = HERE / "results"

ET = zoneinfo.ZoneInfo("America/New_York")
SAMPLE_START = "2019-01-01"

T_WIN_START = dtime(10, 0)
T_WIN_END = dtime(11, 0)
T_EOD = dtime(15, 55)          # 收盘截止: 只用 <15:55 的 bar (避开结算/维护窗口)
T_RTH_OPEN = dtime(9, 30)
T_STOP: dtime | None = None    # 时间止损 (CLI)

# 品种配置: cost_pt = 往返成本(点) = 2×($0.5 佣金 + 1tick 滑点) ÷ 每点乘数
INSTRUMENTS = {
    "nq": dict(data="nq_continuous_1m.parquet", atr="nq_5min_rth.parquet",
               cost_pt=2 * (0.5 + 0.25 * 2.0) / 2.0),    # MNQ $2/点 → 1.0 pt
    "es": dict(data="es_continuous_1m.parquet", atr="es_5min_rth.parquet",
               cost_pt=2 * (0.5 + 0.25 * 50.0) / 50.0),  # ES $50/点 → 0.52 pt
}
DATA_PATH = ORB_DIR / INSTRUMENTS["nq"]["data"]
ATR_PATH = ORB_DIR / INSTRUMENTS["nq"]["atr"]
COST_PT = INSTRUMENTS["nq"]["cost_pt"]


def set_instrument(name: str) -> None:
    global DATA_PATH, ATR_PATH, COST_PT
    cfg = INSTRUMENTS[name]
    DATA_PATH = ORB_DIR / cfg["data"]
    ATR_PATH = ORB_DIR / cfg["atr"]
    COST_PT = cfg["cost_pt"]

K_BUCKETS = [(1.0, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, np.inf)]
K_GRID = [1.0, 1.5, 2.0, 2.5]
S_GRID = [1.0, 1.5, 2.0]
HEADLINE = (1.5, 1.5)


# ---------------------------------------------------------------------------
# 数据与指标
# ---------------------------------------------------------------------------
def build_atr_map() -> dict:
    """14 日 ATR (前一日, Wilder) — 与 orb_core_v84.build_atr_map 同口径, 内联以免拖 nautilus 依赖。"""
    df = pd.read_parquet(ATR_PATH).tz_convert(ET)
    day = (
        df.resample("1D")
        .agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
        .dropna()
    )
    prev_close = day["close"].shift(1)
    tr = pd.concat(
        [day["high"] - day["low"],
         (day["high"] - prev_close).abs(),
         (day["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean().shift(1)
    return {d.date(): float(v) for d, v in atr.dropna().items()}


def anchored_vwap(df: pd.DataFrame, mask: pd.Series | None):
    """按 session 累计 VWAP 与 σ。mask=None → ETH 锚 (全 bar); mask → 只累计命中的 bar (RTH 锚)。"""
    pv = ((df["high"] + df["low"] + df["close"]) / 3 * df["volume"])
    wss = ((df["high"] + df["low"] + df["close"]) / 3) ** 2 * df["volume"]
    if mask is not None:
        pv, wss = pv.where(mask, 0.0), wss.where(mask, 0.0)
        vol = df["volume"].where(mask, 0.0)
    else:
        vol = df["volume"]
    g = df["sess"]
    cv = vol.groupby(g).cumsum().replace(0, np.nan)
    vwap = pv.groupby(g).cumsum() / cv
    var = (wss.groupby(g).cumsum() / cv - vwap**2).clip(lower=0.0)
    return vwap, np.sqrt(var)


def load() -> pd.DataFrame:
    df = pd.read_parquet(DATA_PATH)
    df = df.tz_convert(ET).loc[SAMPLE_START:]
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    df["sess"] = (df.index + pd.Timedelta(hours=6)).normalize()   # 18:00 起算次日
    vwap_e, sig_e = anchored_vwap(df, None)                       # ETH 锚 (主)
    rth_mask = pd.Series((df.index.time >= T_RTH_OPEN) & (df.index.time < dtime(17, 0)), index=df.index)
    vwap_r, sig_r = anchored_vwap(df, rth_mask)                   # RTH 锚 (敏感性)
    df["vwap_e"], df["sig_e"] = vwap_e, sig_e
    df["vwap_r"], df["sig_r"] = vwap_r, sig_r
    return df


# ---------------------------------------------------------------------------
# 逐日遍历
# ---------------------------------------------------------------------------
def first_breach(dev: np.ndarray, ts_t: list, k: float) -> int | None:
    """窗口 [win_start,win_end) 内首个 |dev|≥k 的 bar 下标。
    注意 session 含前一晚 18:00 起的 bar, 不能按 t≥win_end 提前 break (会在 18:00 首根就跳出)。"""
    for i, t in enumerate(ts_t):
        if T_WIN_START <= t < T_WIN_END and np.isfinite(dev[i]) and abs(dev[i]) >= k:
            return i
    return None


def first_touch(day: dict, i0: int, sign: int, upto: int) -> int | None:
    """i0 之后首个触碰 VWAP 的 bar (sign=+1 价格在上方→找 low≤vwap; sign=-1 → high≥vwap)。"""
    lo, hi, vw = day["low"], day["high"], day["vwap_e"]
    for j in range(i0 + 1, upto):
        if sign > 0 and lo[j] <= vw[j]:
            return j
        if sign < 0 and hi[j] >= vw[j]:
            return j
    return None


def walk_trade(day: dict, i0: int, sign: int, s: float, ts_t: list,
               target: str = "vwap") -> tuple[float, str, int]:
    """粗版策略: 返回 (净R, 出场原因, 持仓bar数)。sign=+1 做空 (价格在 VWAP 上方), -1 做多。
    出场优先级 (同 bar 双触保守): 止损 > 目标 > 时间止损 > 收盘平。"""
    c, h, l, vw, sig = day["close"], day["high"], day["low"], day["vwap_e"], day["sig_e"]
    entry = c[i0]
    r_pt = s * sig[i0]
    if not np.isfinite(r_pt) or r_pt <= 0:
        return np.nan, "bad_sig", 0
    stop_px = entry + sign * r_pt
    if target == "half":
        tgt_level = (entry + vw[i0]) / 2          # 固定中点
    a, b = i0 + 1, day["upto"]
    if a >= b:
        return (entry - c[b - 1]) / r_pt if sign > 0 else (c[b - 1] - entry) / r_pt, "eod", 0
    if sign > 0:   # 做空
        stop_hit = h[a:b] >= stop_px
        tgt_hit = (l[a:b] <= tgt_level) if target == "half" else (l[a:b] <= vw[a:b])
    else:          # 做多
        stop_hit = l[a:b] <= stop_px
        tgt_hit = (h[a:b] >= tgt_level) if target == "half" else (h[a:b] >= vw[a:b])
    i_stop = int(np.argmax(stop_hit)) if stop_hit.any() else 10**9
    i_tgt = int(np.argmax(tgt_hit)) if tgt_hit.any() else 10**9
    i_ts = 10**9
    if T_STOP is not None:
        mask_ts = np.array([ts_t[k] >= T_STOP for k in range(a, b)])
        i_ts = int(np.argmax(mask_ts)) if mask_ts.any() else 10**9
    if i_stop < 10**9 and i_stop <= i_tgt and i_stop <= i_ts:
        exit_px, reason, j = stop_px, "stop", a + i_stop
    elif i_tgt < 10**9 and i_tgt <= i_ts:
        exit_px, reason, j = (tgt_level if target == "half" else vw[a + i_tgt]), "target", a + i_tgt
    elif i_ts < 10**9:
        exit_px, reason, j = c[a + i_ts], "tstop", a + i_ts
    else:
        exit_px, reason, j = c[b - 1], "eod", b - 1
    pnl_pt = (entry - exit_px) if sign > 0 else (exit_px - entry)
    return pnl_pt / r_pt - COST_PT / r_pt, reason, j - i0


def run(args):
    global T_WIN_START, T_WIN_END, T_STOP, SAMPLE_START
    T_WIN_START = dtime(*map(int, args.win_start.split(":")))
    T_WIN_END = dtime(*map(int, args.win_end.split(":")))
    T_STOP = dtime(*map(int, args.time_stop.split(":"))) if args.time_stop else None
    set_instrument(args.instrument)
    SAMPLE_START = args.start
    h1, h2 = T_WIN_END, (datetime.combine(pd.Timestamp.now().date(), T_WIN_END) + timedelta(hours=1)).time()

    RESULTS.mkdir(exist_ok=True)
    df = load()
    atr_map = build_atr_map()

    # ---- 逐日收集 ----
    dev10_sigma, dev10_atr, dev10_sign = [], [], []
    maxdev_win = []                      # 窗口内最大 |dev| (σ)
    sig10_pt = []
    rev_rows = []                        # 描述统计: 首破≥1σ 的回归
    rev2_times = []                      # 回归时间样本 (带 |dev|)
    rth_sens = []                        # RTH 锚敏感性: 首破≥1.5σ 后收盘前是否回归
    trades_grid = {(k, s): [] for k in K_GRID for s in S_GRID}
    daily_grid = {(k, s): [] for k in K_GRID for s in S_GRID}   # 每日净R (无交易=0) → 日线 Sharpe
    trades_head = []                     # (year, netR, reason)
    n_days = 0

    for sess, grp in df.groupby("sess"):
        ts_t = list(grp.index.time)
        d = sess.date()
        o = grp["open"].to_numpy()
        c = grp["close"].to_numpy(); h = grp["high"].to_numpy(); l = grp["low"].to_numpy()
        vw = grp["vwap_e"].to_numpy(); vw_r = grp["vwap_r"].to_numpy()
        sig = grp["sig_e"].to_numpy(); sig_r = grp["sig_r"].to_numpy()
        dev = (c - vw) / np.where(sig > 0, sig, np.nan)
        dev_r = (c - vw_r) / np.where(sig_r > 0, sig_r, np.nan)
        valid = np.array([x < T_EOD for x in ts_t])
        if valid.sum() < 2:
            continue
        # 有效 bar (<15:55) 不是数组前缀 (前一晚 18:00 起的 bar 在数组头部且无效),
        # 上界必须用最后一个有效 bar 的实际下标+1, 不能用计数。
        upto = int(np.max(np.where(valid)[0])) + 1
        day = dict(close=c, high=h, low=l, vwap_e=vw, sig_e=sig, upto=upto)
        n_days += 1

        i10 = next((i for i, x in enumerate(ts_t) if x >= T_WIN_START and x < T_EOD), None)
        # 趋势日过滤 (CLI): 开盘 drive = |窗口首根 close - 9:30 open| / ATR14
        drive = np.nan
        if i10 is not None:
            i930 = next((i for i, x in enumerate(ts_t) if x >= T_RTH_OPEN and x < T_EOD), None)
            atr = atr_map.get(d)
            if i930 is not None and atr:
                drive = abs(c[i10] - o[i930]) / atr
        drive_ok = not (args.drive_filter is not None and np.isfinite(drive) and drive > args.drive_filter)

        # 窗口首根偏离
        if i10 is not None and np.isfinite(dev[i10]):
            dev10_sigma.append(abs(dev[i10]))
            dev10_sign.append(1 if dev[i10] > 0 else -1)
            sig10_pt.append(sig[i10])
            atr = atr_map.get(d)
            if atr:
                dev10_atr.append(abs(c[i10] - vw[i10]) / atr)
        # 窗口内最大偏离
        w = [i for i, x in enumerate(ts_t) if T_WIN_START <= x < T_WIN_END]
        if w:
            wd = np.array([dev[i] for i in w if np.isfinite(dev[i])])
            if wd.size:
                maxdev_win.append(np.abs(wd).max())

        if drive_ok:
            # 首破 ≥1σ (主锚) → 描述统计
            i0 = first_breach(dev, ts_t, 1.0)
            if i0 is not None:
                sign = 1 if dev[i0] > 0 else -1
                j = first_touch(day, i0, sign, upto)
                dev0 = abs(dev[i0])
                bucket = next((b for b in K_BUCKETS if b[0] <= dev0 < b[1]), K_BUCKETS[-1])
                if j is None:
                    rev_rows.append((bucket, dev_r[i0] if np.isfinite(dev_r[i0]) else np.nan, np.nan, np.nan, np.nan))
                else:
                    tj = ts_t[j]
                    rev_rows.append((bucket, dev_r[i0] if np.isfinite(dev_r[i0]) else np.nan,
                                     1 if tj < h1 else 0, 1 if tj < h2 else 0, 1.0))
                    rev2_times.append((dev0, tj))
            # RTH 锚敏感性: 首破≥1.5σ 后收盘前回归
            i0r = first_breach(dev_r, ts_t, 1.5)
            if i0r is not None:
                signr = 1 if dev_r[i0r] > 0 else -1
                dayr = dict(low=l, high=h, vwap_e=vw_r, upto=upto)
                jr = first_touch(dayr, i0r, signr, upto)
                rth_sens.append(1.0 if jr is not None else 0.0)

            # 粗版策略网格
            for k in K_GRID:
                ik = first_breach(dev, ts_t, k)
                for s in S_GRID:
                    if ik is None:
                        daily_grid[(k, s)].append(0.0)
                        continue
                    sign = 1 if dev[ik] > 0 else -1
                    net, reason, hold = walk_trade(day, ik, sign, s, ts_t, args.target)
                    if np.isfinite(net):
                        trades_grid[(k, s)].append((d.year, net, reason, hold))
                        daily_grid[(k, s)].append(net)
                        if (k, s) == HEADLINE:
                            trades_head.append((d.year, net, reason, hold))
                    else:
                        daily_grid[(k, s)].append(0.0)
        else:
            for key in daily_grid:
                daily_grid[key].append(0.0)

    # ---- 汇总输出 ----
    P = lambda a, qs: np.percentile(np.array(a), qs)
    print("=" * 84)
    print(f"[{args.instrument.upper()}] VWAP 均值回归 ({T_WIN_START.strftime('%H:%M')}-{T_WIN_END.strftime('%H:%M')} ET)  "
          f"样本 {df.index[0].date()} ~ {df.index[-1].date()}, {n_days} 个交易日, 往返成本 {COST_PT:.2f} pt")
    extras = []
    if args.target != "vwap":
        extras.append(f"target={args.target}")
    if T_STOP:
        extras.append(f"time-stop={T_STOP.strftime('%H:%M')}")
    if args.drive_filter is not None:
        extras.append(f"drive-filter<={args.drive_filter}×ATR")
    if extras:
        print("变体: " + ", ".join(extras))
    print("主锚: ETH 18:00 ET (平台标准 VWAP);  敏感性: RTH 9:30 锚")
    print("=" * 84)

    print(f"\n--- 窗口首根 ({T_WIN_START.strftime('%H:%M')}) 偏离 |close-VWAP| ---")
    print(f"单位 σ   : p50={P(dev10_sigma,[50])[0]:.2f}  p75={P(dev10_sigma,[75])[0]:.2f}  "
          f"p90={P(dev10_sigma,[90])[0]:.2f}  p95={P(dev10_sigma,[95])[0]:.2f}  p99={P(dev10_sigma,[99])[0]:.2f}")
    if dev10_atr:
        q = P(dev10_atr, [50, 90, 95, 99])
        print(f"单位 ATR%: p50={q[0]:.1%}  p90={q[1]:.1%}  p95={q[2]:.1%}  p99={q[3]:.1%}")
    up = np.mean(np.array(dev10_sign) > 0)
    print(f"方向: 价格在 VWAP 上方 {up:.0%} / 下方 {1-up:.0%}")
    q = P(sig10_pt, [50])[0]
    print(f"σ(首根) 中位 = {q:.1f} pt  (VWAP band 的 1σ 宽度)")

    q = P(maxdev_win, [50, 75, 90, 95, 99])
    print(f"\n--- 窗口内最大偏离 max|dev| (σ) ---")
    print(f"p50={q[0]:.2f}  p75={q[1]:.2f}  p90={q[2]:.2f}  p95={q[3]:.2f}  p99={q[4]:.2f}")

    # 回归概率表
    rows = pd.DataFrame(rev_rows, columns=["bucket", "dev_rth_anchor", "by_h1", "by_h2", "by_close"])
    print(f"\n--- 首破≥1σ 后的 VWAP 回归概率 (主锚, 按首次突破档位) ---")
    print(f"{'档位(σ)':<12}{'n':>5}{f'P({h1.strftime("%H%M")}前)':>11}{f'P({h2.strftime("%H%M")}前)':>11}{'P(收盘前)':>10}")
    out_rows = []
    for b in K_BUCKETS:
        sub = rows[rows["bucket"] == b]
        if len(sub) == 0:
            continue
        label = f"{b[0]:.1f}-{b[1]:.1f}" if np.isfinite(b[1]) else f">={b[0]:.1f}"
        print(f"{label:<12}{len(sub):>5}{sub['by_h1'].mean():>11.1%}{sub['by_h2'].mean():>11.1%}"
              f"{sub['by_close'].mean():>10.1%}")
        out_rows.append(dict(bucket=label, n=len(sub), p_by_h1=sub["by_h1"].mean(),
                             p_by_h2=sub["by_h2"].mean(), p_by_close=sub["by_close"].mean()))
    if rth_sens:
        print(f"(敏感性: RTH 9:30 锚, 首破≥1.5σ 后收盘前回归概率 = {np.mean(rth_sens):.1%}, n={len(rth_sens)})")

    # 回归时间分布
    print(f"\n--- 回归 (首次触碰 VWAP) 发生时间分布 ---")
    for name, cond in [("首破≥1.0σ", lambda dv: dv >= 1.0), ("首破≥2.0σ", lambda dv: dv >= 2.0)]:
        times = [tj for dv, tj in rev2_times if cond(dv)]
        if not times:
            continue
        n_all = len([dv for dv, _ in rev2_times if cond(dv)])
        b = pd.Series([f"{t.strftime('%H')}-{int(t.strftime('%H'))+1}" if t < dtime(14, 0)
                       else "14-15:55" for t in times]).value_counts()
        seg = {s: b.get(s, 0) for s in ["10-11", "11-12", "12-13", "13-14", "14-15:55"]}
        print(f"{name}: n={n_all}  " + "  ".join(f"{k}:{v/n_all:.0%}" for k, v in seg.items())
              + f"  未回归:{(n_all-len(times))/n_all:.0%}")

    # 粗版策略网格
    print(f"\n--- 粗版策略: 窗口首破 kσ 反向入场, 止损 s·σ (净R已扣往返 {COST_PT:.2f}pt; Sharpe=日线口径年化) ---")
    print(f"{'k':>4}{'s':>4}{'n':>5}{'胜率':>7}{'均R净':>8}{'日Sharpe':>9}{'t(笔)':>7}{'p05':>7}{'最差R':>8}"
          f"{'止损率':>7}{'时停率':>7}{'EOD率':>7}{'中位持仓m':>9}")
    g_rows = []
    for k in K_GRID:
        for s in S_GRID:
            tr = trades_grid[(k, s)]
            rd = np.array(daily_grid[(k, s)])
            if not tr:
                print(f"{k:>4.1f}{s:>4.1f}{0:>5}")
                continue
            r = np.array([x[1] for x in tr])
            reasons = [x[2] for x in tr]
            holds = [x[3] for x in tr]
            sharpe = r.mean() / r.std(ddof=1) * np.sqrt(252) if r.std(ddof=1) > 0 else np.nan
            tstat = r.mean() / (r.std(ddof=1) / np.sqrt(len(r))) if r.std(ddof=1) > 0 else np.nan
            print(f"{k:>4.1f}{s:>4.1f}{len(r):>5}{(r>0).mean():>7.1%}{r.mean():>8.2f}{sharpe:>9.2f}{tstat:>7.2f}"
                  f"{np.percentile(r,5):>7.2f}{r.min():>8.1f}"
                  f"{reasons.count('stop')/len(r):>7.1%}{reasons.count('tstop')/len(r):>7.1%}"
                  f"{reasons.count('eod')/len(r):>7.1%}{np.median(holds):>9.0f}")
            g_rows.append(dict(k=k, s=s, n=len(r), win=(r > 0).mean(), avg_net_r=r.mean(),
                               daily_sharpe=sharpe, t_stat=tstat, med_r=np.median(r),
                               p05=np.percentile(r, 5), worst=r.min(),
                               stop_rate=reasons.count("stop") / len(r),
                               tstop_rate=reasons.count("tstop") / len(r),
                               eod_rate=reasons.count("eod") / len(r)))
    if not args.no_save:
        sfx = f"_{args.tag}" if args.tag else ""
        pd.DataFrame(g_rows).to_csv(RESULTS / f"vwap_mr_grid{sfx}.csv", index=False)
        pd.DataFrame(out_rows).to_csv(RESULTS / f"vwap_mr_reversion{sfx}.csv", index=False)

    # 分年 (headline)
    kh, sh = HEADLINE
    print(f"\n--- 分年表现 (k={kh}σ, s={sh}σ) ---")
    hy = pd.DataFrame(trades_head, columns=["year", "net", "reason", "hold"])
    for y, sub in hy.groupby("year"):
        r = sub["net"]
        print(f"{y}: n={len(sub):>3}  均R净={r.mean():>6.2f}  胜率={(r>0).mean():>5.1%}  "
              f"最差R={r.min():>6.1f}  止损率={(sub['reason']=='stop').mean():>5.1%}")
    if not args.no_save and len(hy):
        sfx = f"_{args.tag}" if args.tag else ""
        hy.to_csv(RESULTS / f"vwap_mr_byyear{sfx}.csv", index=False)
        print(f"\nCSV → {RESULTS}/vwap_mr_*{sfx or ''}.csv")


def parse_args():
    ap = argparse.ArgumentParser(description="VWAP 均值回归·上午场快测")
    ap.add_argument("--instrument", choices=list(INSTRUMENTS), default="nq",
                    help="品种: nq=NQ/MNQ 口径, es=ES 口径 (默认 nq)")
    ap.add_argument("--start", default="2019-01-01", help="样本起始日 YYYY-MM-DD (默认 2019-01-01)")
    ap.add_argument("--win-start", default="10:00", help="入场窗口开始 HH:MM ET (默认 10:00)")
    ap.add_argument("--win-end", default="11:00", help="入场窗口结束 HH:MM ET (默认 11:00)")
    ap.add_argument("--time-stop", default=None, help="HH:MM 时间止损: 到点仍未出场按当根收盘平 (须晚于窗口结束)")
    ap.add_argument("--target", choices=["vwap", "half"], default="vwap",
                    help="half = 目标改为入场价与 VWAP 的中点 (路径减半)")
    ap.add_argument("--drive-filter", type=float, default=None,
                    help="趋势日过滤: |close(窗口首根)-open(9:30)| > X×ATR14 的日子不做")
    ap.add_argument("--no-save", action="store_true", help="只打印, 不写 CSV")
    ap.add_argument("--tag", default="", help="CSV 文件名后缀")
    return ap.parse_args()


if __name__ == "__main__":
    run(parse_args())
