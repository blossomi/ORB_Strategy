# -*- coding: utf-8 -*-
"""
vwap_trend_backtest.py — VWAP Trend Trading 回测 (Zarattini & Aziz 2023 论文复现 + 网格)
====================================================================================
策略决策全部在 vwap_fsm.VwapFsm; 本文件 = 数据管道 + 回测适配层 + 网格 runner。

与 orb (v5.0) 框架的关系:
  - 同一架构分层: FSM 决策核 (vwap_fsm.py, 无引擎依赖) + 薄适配层; 统计/CSV/
    成本口径与 v5.0/orb_backtest.py 对齐 (commission+滑点按 tick, MNQ $2/点)。
  - 引擎差异: VWAP 策略全部信号与成交都在 bar 收盘时刻确定性发生 (收盘穿 VWAP
    反手, 无挂单), 不需要 nautilus 的事件引擎 —— 网格要跑上百配置, 直接用
    numpy 数组 + FSM 热循环 (单配置全样本 ~1-2s)。
  - 价格口径: 1m 连续合约是**加法回溯调整** (同合约期内 close−close_raw 恒定,
    跳变只发生在 18:00 ET 换月开盘)。本管道逐 bar 还原为**原始价格**再算 VWAP/
    PnL —— 加法调整下逐笔 PnL 与原始价格完全一致, 且名义定仓 (paper sizing)
    不被调整漂移污染。check 模式对该断言做全量验证。

用法:
  cd VWAP && ../.venv/bin/python vwap_trend_backtest.py --run check
  ../.venv/bin/python vwap_trend_backtest.py --run single --anchor eth --tag eth_base
  ../.venv/bin/python vwap_trend_backtest.py --run grid --grid stage1
  ../.venv/bin/python vwap_trend_backtest.py --run grid --grid wf
"""
import argparse
import csv
import time as walltime
from datetime import date as ddate
from datetime import time as dtime
from math import sqrt
from pathlib import Path

import numpy as np
import pandas as pd
import zoneinfo

from vwap_fsm import (ANCHOR_ETH, ANCHOR_RTH, LONG, SZ_FIXED, SZ_PAPER,
                      SZ_RISK, VwapCommands, VwapEnv, VwapFsm, VwapParams)

# ===========================================================================
# 路径与常量 (与 v5.0 主线同口径)
# ===========================================================================
HERE = Path(__file__).resolve().parent
DATA_1M = HERE.parent / "archive" / "ORB_strategy" / "nq_continuous_1m.parquet"
DATA_5M_RTH = HERE.parent / "v5.0" / "data" / "nq_5min_rth.parquet"   # 仅用于 ATR(14) 与 5m 交叉验证
OUT_DIR = HERE / "results"

ET = zoneinfo.ZoneInfo("America/New_York")

TICK = 0.25
MULTIPLIER = 2.0                     # MNQ $/点 (与 v5.0 主线一致)
START_DATE = "2018-01-01"
END_DATE = "2026-08-30"
STARTING_CAPITAL = 25000.0
COMMISSION_PER_CONTRACT = 0.5        # 每手每边 (v5.0 主线口径)

T_RTH_START_MIN = 9 * 60 + 30        # 570
T_RTH_END_MIN = 16 * 60              # 960; EOD 价 = 15:59(1m) 收盘 = 16:00 墙钟价
T_MIDDAY_START = dtime(12, 0)
T_MIDDAY_END = dtime(15, 0)

MAX_QTY = 200
MAX_NOTIONAL_LEV = 10.0
EXPECTED_BARS_PER_DAY = {1: 390, 5: 78}   # RTH 全日 (左标签 9:30..15:59 / 9:30..15:55)


def p(*args, **kw):
    print(*args, **kw, flush=True)


# ===========================================================================
# 数据管道
# ===========================================================================
def load_1m_pair() -> tuple[pd.DataFrame, pd.Series]:
    """读 1m 连续合约 → (原始价格 df, D=close−close_raw)。

    校验: 同 contract 期内 D 必须恒定 (加法调整的定义), 否则 raise ——
    这是"还原 = 原始价格"断言的前提。
    """
    df = pd.read_parquet(DATA_1M)[["contract", "open", "high", "low", "close",
                                   "volume", "close_raw"]]
    d_adj = df["close"] - df["close_raw"]
    grp = d_adj.groupby(df["contract"]).nunique()
    bad = grp[grp > 1]
    if len(bad):
        raise AssertionError(f"加法调整断言失败: {len(bad)} 个合约期内 D 不恒定: "
                             f"{bad.index[:5].tolist()}")
    raw = df.drop(columns=["contract", "close_raw"]).copy()
    for col in ("open", "high", "low", "close"):
        raw[col] = raw[col] - d_adj
    raw.index = raw.index.tz_convert(ET)
    raw = raw.sort_index()
    return raw, d_adj


def resample_5m(df: pd.DataFrame) -> pd.DataFrame:
    """1m → 5m (左标签左闭), 丢空桶 (17:00-18:00 停盘段)。"""
    return df.resample("5min", label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum")).dropna()


class Dataset:
    """预计算全部数组: RTH bar 序列 + 两种锚的 VWAP + ATR 映射 + EOD 索引。

    引擎热循环只吃 python list / int64 数组, 不碰 pandas。
    """

    def __init__(self, bar_min: int, load_cache: dict | None = None):
        self.bar_min = bar_min
        t0 = walltime.perf_counter()
        if load_cache is not None and "pair" in load_cache:
            raw, d_adj = load_cache["pair"]
        else:
            raw, d_adj = load_1m_pair()
            if load_cache is not None:
                load_cache["pair"] = (raw, d_adj)
        df = resample_5m(raw) if bar_min == 5 else raw

        t_all = df.index.hour * 60 + df.index.minute
        rth_mask = (t_all >= T_RTH_START_MIN) & (t_all < T_RTH_END_MIN)
        rth = df[rth_mask]
        eth_key_all = (df.index + pd.Timedelta(hours=6)).normalize()  # 18:00 起算次日 session

        # --- RTH 锚 VWAP: 日内 (仅 RTH bar) 累计 ---
        rth_typ = (rth["high"] + rth["low"] + rth["close"]) / 3.0
        rth_day = rth.index.normalize()
        vwap_rth = ((rth_typ * rth["volume"]).groupby(rth_day).cumsum()
                    / rth["volume"].groupby(rth_day).cumsum())

        # --- ETH 锚 VWAP: session (18:00→次日 17:00) 累计, 隔夜 bar 计入 ---
        typ_all = (df["high"] + df["low"] + df["close"]) / 3.0
        vwap_eth_all = ((typ_all * df["volume"]).groupby(eth_key_all).cumsum()
                        / df["volume"].groupby(eth_key_all).cumsum())

        # --- ATR(14) 日线 (与 orb build_atr_map 同公式, 用 5m RTH parquet; D 漂移不影响 TR) ---
        day5 = pd.read_parquet(DATA_5M_RTH)
        day5.index = day5.index.tz_convert(ET)
        d1 = day5.resample("1D").agg(high=("high", "max"), low=("low", "min"),
                                     close=("close", "last")).dropna()
        prev = d1["close"].shift(1)
        tr = pd.concat([d1["high"] - d1["low"], (d1["high"] - prev).abs(),
                        (d1["low"] - prev).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / 14, adjust=False).mean().shift(1)
        self.atr_map: dict[ddate, float] = {k.date(): float(v)
                                            for k, v in atr.dropna().items()}

        # --- 引擎数组 (RTH bar 序列, 时间升序) ---
        days = rth_day.date
        self.ts_ns = rth.index.asi8
        self.d_list = list(days)
        self.t_min = (rth.index.hour * 60 + rth.index.minute).tolist()
        self.c = rth["close"].tolist()
        self.vwap_rth = vwap_rth.tolist()
        self.vwap_eth = vwap_eth_all.reindex(rth.index).tolist()

        day_last: dict[ddate, int] = {}
        for i, d in enumerate(days):
            day_last[d] = i
        cnt: dict[ddate, int] = {}
        bar_idx = np.empty(len(days), dtype=np.int32)
        for i, d in enumerate(days):
            cnt[d] = cnt.get(d, -1) + 1
            bar_idx[i] = cnt[d]
        self.bar_idx = bar_idx
        self.is_eod = [i == day_last[d] for i, d in enumerate(days)]

        # 每日 bar 根数断言 (清单 A6): 异常日 (半日市/熔断) 计数警告, 不阻断
        n_by_day = pd.Series(1, index=days).groupby(level=0).sum()
        self.n_days = int(len(n_by_day))
        self.anom_days = n_by_day[n_by_day != EXPECTED_BARS_PER_DAY[bar_min]]
        p(f"[数据] 1m {len(raw):,} 行 → bar{bar_min} RTH {len(rth):,} 根 / "
          f"{self.n_days:,} 天 (根数异常日 {len(self.anom_days)})  "
          f"加载 {walltime.perf_counter() - t0:.1f}s")
        if len(self.anom_days):
            head = [(str(k), int(v)) for k, v in self.anom_days.head(8).items()]
            p(f"[数据] ⚠️ 根数≠{EXPECTED_BARS_PER_DAY[bar_min]} 的日子 "
              f"(半日市等, EOD 按实际最后 bar 收口): {head} ...")

    def vwap(self, anchor: str) -> list:
        return self.vwap_rth if anchor == ANCHOR_RTH else self.vwap_eth

    def et_time(self, ns: int):
        return pd.Timestamp(ns, tz="UTC").tz_convert(ET)


_DS_CACHE: dict[int, Dataset] = {}
_PAIR_CACHE: dict = {}


def get_ds(bar_min: int) -> Dataset:
    if bar_min not in _DS_CACHE:
        _DS_CACHE[bar_min] = Dataset(bar_min, load_cache=_PAIR_CACHE)
    return _DS_CACHE[bar_min]


def slice_bounds(ds: Dataset, start: str, end: str) -> tuple[int, int]:
    s = pd.Timestamp(start, tz=ET).value
    e = (pd.Timestamp(end, tz=ET) + pd.Timedelta(days=1)).value
    return int(np.searchsorted(ds.ts_ns, s)), int(np.searchsorted(ds.ts_ns, e))


# ===========================================================================
# 回测适配层 (VwapEnv + VwapCommands; 成交 = bar 收盘确定性同步)
# ===========================================================================
class Backtest(VwapEnv, VwapCommands):
    def __init__(self, params: VwapParams, ds: Dataset, start_i: int, end_i: int,
                 commission: float = COMMISSION_PER_CONTRACT, slip_ticks: int = 1):
        self.p = params
        self.ds = ds
        self.sl, self.el = start_i, end_i
        self.cost_side = commission + slip_ticks * TICK * MULTIPLIER
        self.equity_val = STARTING_CAPITAL
        self.fsm = VwapFsm(params, env=self, cmds=self)
        # trade 行: [entry_ts, exit_ts, side, qty, epx, xpx, pnl, stop_dist, r, reason, dur_min]
        self.trades: list[list] = []
        self._open: list | None = None
        self._cur_ns = 0
        self._entry_ns = 0

    # ---- VwapEnv ----
    def equity(self) -> float:
        return self.equity_val

    def atr_for(self, d: ddate):
        return self.ds.atr_map.get(d)

    # ---- VwapCommands ----
    def enter_market(self, side: str, qty: int, px: float, reason: str) -> None:
        self.equity_val -= self.cost_side * qty
        self._entry_ns = self._cur_ns
        self._open = [pd.Timestamp(self._cur_ns, tz="UTC").tz_convert(ET), None,
                      side, qty, px, None, None, self.fsm.entry_stop_dist,
                      None, reason, None]

    def exit_market(self, px: float, reason: str) -> None:
        tr = self._open
        assert tr is not None, "exit 无持仓 (FSM 状态机漏洞)"
        qty, epx = tr[3], tr[4]
        sign = 1.0 if tr[2] == LONG else -1.0
        pnl = (px - epx) * sign * MULTIPLIER * qty
        self.equity_val += pnl - self.cost_side * qty
        xts = pd.Timestamp(self._cur_ns, tz="UTC").tz_convert(ET)
        tr[1], tr[5], tr[6], tr[10] = xts, px, pnl, (xts - tr[0]).total_seconds() / 60
        sd = tr[7]
        tr[8] = pnl / (qty * sd * MULTIPLIER) if sd and sd > 0 else None
        self.trades.append(tr)
        self._open = None

    # ---- 主循环 ----
    def run(self) -> "Backtest":
        fsm = self.fsm
        on_bar = fsm.on_bar
        ds = self.ds
        sl, el = self.sl, self.el
        d_list = ds.d_list[sl:el]
        t_min = ds.t_min[sl:el]
        c = ds.c[sl:el]
        vwap = ds.vwap(self.p.anchor)[sl:el]
        is_eod = ds.is_eod[sl:el]
        bar_idx = ds.bar_idx[sl:el]
        ts_ns = ds.ts_ns[sl:el]

        cur_day = None
        for i in range(el - sl):
            d = d_list[i]
            if d != cur_day:            # 值比较 (date 对象不保证同一实例)
                cur_day = d
                fsm.on_new_day()
            self._cur_ns = ts_ns[i]
            on_bar(d, t_min[i], int(bar_idx[i]), c[i], vwap[i], is_eod[i])
        assert self._open is None, "收盘仍有未平持仓 (EOD 收口漏洞)"
        return self

    # ---- 指标 ----
    def metrics(self) -> dict:
        tr = self.trades
        n = len(tr)
        eq = self.equity_val
        pnl_arr = np.array([t[6] for t in tr]) if n else np.array([0.0])
        r_arr = np.array([t[8] for t in tr if t[8] is not None])
        # 日度: 全部持仓隔夜为零 → 当日 realize (含成本) = 当日 PnL
        day_pnl: dict[ddate, float] = {}
        for t in tr:
            day_pnl[t[0].date()] = day_pnl.get(t[0].date(), 0.0) + t[6] \
                - 2 * self.cost_side * t[3]
        days = sorted(day_pnl)
        daily = (pd.Series([day_pnl[d] for d in days], index=pd.DatetimeIndex(days))
                 if days else pd.Series(dtype=float))
        eq_daily = STARTING_CAPITAL + daily.cumsum()
        peak = eq_daily.cummax()
        mdd = float(((eq_daily - peak) / peak).min()) if len(daily) else 0.0

        ret = (daily / STARTING_CAPITAL).to_numpy()   # 固定本金口径 (orb 同式)
        sharpe = float(ret.mean() / ret.std() * sqrt(252)) \
            if len(ret) > 1 and ret.std() > 0 else 0.0
        downside = np.minimum(ret, 0.0)
        dstd = float(np.sqrt(np.mean(downside ** 2)))
        sortino = float(ret.mean() / dstd * sqrt(252)) if dstd > 0 else 0.0

        years = (pd.Timestamp(END_DATE) - pd.Timestamp(START_DATE)).days / 365.25
        annual = (eq / STARTING_CAPITAL) ** (1.0 / years) - 1.0 if eq > 0 else -1.0
        wins = pnl_arr[pnl_arr > 0].sum()
        losses = abs(pnl_arr[pnl_arr <= 0].sum())
        pf = float(wins / losses) if losses > 0 else float("inf")
        top5 = float(np.sort(pnl_arr)[-5:].sum()) if n >= 5 else float(pnl_arr.sum())

        f = self.fsm
        return dict(
            n_trades=n, final_eq=eq, tot_ret=eq / STARTING_CAPITAL - 1,
            annual=annual, sharpe=sharpe, sortino=sortino, mdd=mdd,
            winrate=float((pnl_arr > 0).mean()) if n else 0.0, pf=pf,
            avg_r=float(r_arr.mean()) if len(r_arr) else float("nan"),
            med_r=float(np.median(r_arr)) if len(r_arr) else float("nan"),
            top5_share=top5 / pnl_arr.sum() if pnl_arr.sum() > 0 else float("nan"),
            n_entries=f.n_entries, n_cross=f.n_exits_cross, n_eod=f.n_exits_eod,
            n_midday=f.n_exits_midday, skip_tight=f.n_skip_tight_stop,
            skip_afford=f.n_skip_cant_afford, cap_lev=f.n_capped_lev,
            cap_qty=f.n_capped_qty,
        )

    def yearly_table(self) -> pd.DataFrame:
        by_year: dict[int, list] = {}
        for t in self.trades:
            by_year.setdefault(t[0].year, []).append(t)
        rows = []
        for y in sorted(by_year):
            ts = by_year[y]
            pnl = np.array([t[6] for t in ts])
            rs = [t[8] for t in ts if t[8] is not None]
            rows.append([y, len(ts), float(pnl.sum()) - len(ts) * 0,
                         float((pnl > 0).mean()) if len(pnl) else 0.0,
                         float(np.mean(rs)) if rs else float("nan")])
        return pd.DataFrame(rows, columns=["年", "笔数", "净PnL$", "胜率", "平均R"])


def export_trades_csv(bt: Backtest, path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entry_time_et", "exit_time_et", "side", "qty", "entry_price",
                    "exit_price", "pnl_usd", "stop_dist_pt", "r_multiple",
                    "exit_reason", "duration_min"])
        for tr in bt.trades:
            w.writerow([tr[0].strftime("%Y-%m-%d %H:%M:%S"),
                        tr[1].strftime("%Y-%m-%d %H:%M:%S"),
                        tr[2], tr[3], round(tr[4], 2), round(tr[5], 2),
                        round(tr[6], 2), round(tr[7], 2) if tr[7] else "",
                        round(tr[8], 3) if tr[8] is not None else "",
                        tr[9], round(tr[10], 1)])
    return len(bt.trades)


def print_stats(bt: Backtest, m: dict, header: str, slip: int,
                start: str = START_DATE, end: str = END_DATE):
    pr = bt.p
    p(f"\n===== {header} =====")
    p(f"口径: {start}~{end} | bar{bt.ds.bar_min}m VWAP锚={pr.anchor} | "
      f"仓位={pr.sizing}" + (f"(lev{pr.paper_lev})" if pr.sizing == SZ_PAPER else "")
      + (f"(risk{pr.risk_pct * 100:g}%)" if pr.sizing == SZ_RISK else "")
      + (f"(固定{pr.fixed_qty}手)" if pr.sizing == SZ_FIXED else "")
      + f" | 进出场={pr.mode} delay={pr.entry_delay_bars} buf={pr.buffer_ticks:g}tick"
      + (f" | 午间空仓" if pr.midday_flat else "")
      + (f" | 只多" if pr.side == "long_only" else "")
      + f" | 滑点{slip}tick+${COMMISSION_PER_CONTRACT}/手/边 | 本金${STARTING_CAPITAL:,.0f}")
    p(f"交易 {m['n_trades']:,} 笔 (入场 {m['n_entries']:,} | 反手出场 {m['n_cross']:,} | "
      f"EOD {m['n_eod']:,} | 午间 {m['n_midday']:,})")
    p(f"跳过: 紧止损 {m['skip_tight']:,} | 买不起 {m['skip_afford']:,} | "
      f"杠杆帽 {m['cap_lev']:,} | 手数帽 {m['cap_qty']:,}")
    p(f"最终权益: ${m['final_eq']:,.2f}  总收益: {m['tot_ret'] * 100:,.1f}%  "
      f"年化: {m['annual'] * 100:,.1f}%")
    pf_str = "∞" if np.isinf(m["pf"]) else f"{m['pf']:.2f}"
    p(f"Sharpe: {m['sharpe']:.2f}  Sortino: {m['sortino']:.2f}  MDD: {m['mdd'] * 100:.1f}%  "
      f"胜率: {m['winrate'] * 100:.1f}%  PF: {pf_str}")
    if not np.isnan(m["avg_r"]):
        p(f"R 口径: 平均 {m['avg_r']:.3f}R  中位 {m['med_r']:.3f}R  "
          f"前5大赢单占净利: {m['top5_share'] * 100:.0f}%")
    p(f"年度:\n{bt.yearly_table().to_string(index=False)}")


# ===========================================================================
# 参数构造
# ===========================================================================
def make_params(a: argparse.Namespace, **over) -> VwapParams:
    over = dict(over)
    alias = {"entry_mode": "mode", "buffer": "buffer_ticks", "delay": "entry_delay_bars",
             "min_stop_atr": "min_stop_atr_frac"}
    for k, v in alias.items():          # 网格覆盖键与 CLI 同名, FSM 字段是全名
        if k in over:
            over[v] = over.pop(k)
    kw = dict(
        tick=TICK, multiplier=MULTIPLIER, anchor=a.anchor,
        entry_delay_bars=a.delay, mode=a.entry_mode, buffer_ticks=a.buffer,
        sizing=a.sizing, fixed_qty=a.fixed_qty, paper_lev=a.paper_lev,
        risk_pct=a.risk_pct, min_stop_atr_frac=a.min_stop_atr,
        max_qty=MAX_QTY, max_notional_lev=MAX_NOTIONAL_LEV,
        midday_flat=a.midday_flat, side=a.side,
        t_midday_start=T_MIDDAY_START, t_midday_end=T_MIDDAY_END,
    )
    kw.update(over)
    return VwapParams(**kw)


# ===========================================================================
# 独立第二链路 (清单 C2): always_in/buffer=0/无午间/双边/固定手数 基线族
#   pos_i = 当日截至上一 bar 的最后非零 sign(c−vwap); pnl = Σ pos_i×Δc_{i+1} − 成本
# ===========================================================================
def vector_check(ds: Dataset, params: VwapParams, start_i: int, end_i: int,
                 cost_side: float) -> dict:
    """独立第二链路: 逐 bar 重算持仓路径与逐 bar PnL (与 FSM 的逐笔法不同路径)。

    复刻语义: 收盘穿 VWAP 反手 (含 FSM 的 tight-stop 跳过: |c−vwap| < 1 tick
    时平仓不反手); EOD/换日强制空仓。毛利 = Σ pos×Δc × multiplier (美元)。
    """
    c = np.asarray(ds.c[start_i:end_i])
    vw = np.asarray(ds.vwap(params.anchor)[start_i:end_i])
    bidx = ds.bar_idx[start_i:end_i]
    d_ord = np.fromiter((d.toordinal() for d in ds.d_list[start_i:end_i]), dtype=np.int64)
    eod = np.asarray(ds.is_eod[start_i:end_i])

    gross_pts = 0.0
    n_entries = 0
    cur = 0.0
    prev_d = d_ord[0] if len(d_ord) else 0
    for i in range(len(c)):
        if d_ord[i] != prev_d:            # 换日: 隔夜必平, 方向清零
            prev_d = d_ord[i]
            cur = 0.0
        s = (1.0 if c[i] > vw[i] else -1.0) if c[i] != vw[i] else 0.0
        if s != 0 and s != cur and not eod[i]:   # EOD bar 只平不进 (FSM 同语义)
            if abs(c[i] - vw[i]) >= params.tick:
                n_entries += 1
                cur = s
            else:
                cur = 0.0                 # 止损距离 <1 tick: 平仓不反手 (FSM 同语义)
        if i + 1 < len(c) and not eod[i]:
            gross_pts += cur * (c[i + 1] - c[i])
        else:
            cur = 0.0                     # EOD 收口
    gross = gross_pts * params.multiplier
    return dict(gross=gross, entries=n_entries,
                fills=2 * n_entries, net=gross - 2 * n_entries * cost_side)


# ===========================================================================
# 单跑 / 网格
# ===========================================================================
GRID_COLS = ["tag", "anchor", "bar", "sizing", "paper_lev", "risk_pct", "mode",
             "delay", "buffer", "min_stop_atr", "midday", "side", "slip",
             "n_trades", "final_eq", "tot_ret", "annual", "sharpe", "sortino",
             "mdd", "winrate", "pf", "avg_r", "med_r", "top5_share",
             "skip_tight", "skip_afford", "cap_lev", "cap_qty"]
_CFG_SLIP: dict = {}


def grid_row(tag: str, pr: VwapParams, bar_min: int, m: dict,
             slip: int | None = None) -> dict:
    return dict(tag=tag, anchor=pr.anchor, bar=bar_min, sizing=pr.sizing,
                paper_lev=pr.paper_lev, risk_pct=pr.risk_pct, mode=pr.mode,
                delay=pr.entry_delay_bars, buffer=pr.buffer_ticks,
                min_stop_atr=pr.min_stop_atr_frac, midday=pr.midday_flat,
                side=pr.side,
                slip=slip if slip is not None else _CFG_SLIP.get(tag, -1),
                **{k: m[k] for k in GRID_COLS[13:]})


def run_one(ds: Dataset, params: VwapParams, tag: str | None, slip: int,
            start: str, end: str, save_csv: bool = True) -> tuple[Backtest, dict]:
    sl, el = slice_bounds(ds, start, end)
    bt = Backtest(params, ds, sl, el, slip_ticks=slip).run()
    m = bt.metrics()
    print_stats(bt, m, f"单跑 {tag or ''}", slip, start, end)
    if save_csv and tag:
        out = OUT_DIR / f"vwap_trades_{tag}.csv"
        n = export_trades_csv(bt, out)
        p(f"逐笔 CSV: {out} ({n:,} 笔)")
    return bt, m


def run_grid(ds: Dataset, configs: list[tuple[str, VwapParams]], name: str,
             slip: int, start: str, end: str, verbose: bool = True) -> pd.DataFrame:
    rows = []
    t0 = walltime.perf_counter()
    for i, (tag, pr) in enumerate(configs):
        sl, el = slice_bounds(ds, start, end)
        cfg_slip = _CFG_SLIP.get(tag, slip)   # 配置可携带独立滑点档 (stage1 的鲁棒性维度)
        bt = Backtest(pr, ds, sl, el, slip_ticks=cfg_slip).run()
        m = bt.metrics()
        rows.append(grid_row(tag, pr, ds.bar_min, m, slip=cfg_slip))
        if verbose:
            p(f"[{i + 1}/{len(configs)}] {tag} (slip{cfg_slip}): 笔数={m['n_trades']:,} "
              f"终值=${m['final_eq']:,.0f} 年化={m['annual'] * 100:.1f}% "
              f"Sharpe={m['sharpe']:.2f} MDD={m['mdd'] * 100:.1f}% "
              f"胜率={m['winrate'] * 100:.1f}% ({walltime.perf_counter() - t0:.0f}s)")
    df = pd.DataFrame(rows)[GRID_COLS]
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"vwap_grid_{name}.csv"
    df.to_csv(out, index=False)
    p(f"\n网格结果 → {out}  (共 {len(df)} 组, {walltime.perf_counter() - t0:.0f}s)")
    return df


# ===========================================================================
# walk-forward: 滚动 3 年训 / 2 年测, 4 折; 训练集选 Sharpe 最优, 报 OOS
# ===========================================================================
WF_FOLDS = [("2018-01-01", "2020-12-31", "2021-01-01", "2022-12-31"),
            ("2019-01-01", "2021-12-31", "2022-01-01", "2023-12-31"),
            ("2020-01-01", "2022-12-31", "2023-01-01", "2024-12-31"),
            ("2021-01-01", "2023-12-31", "2024-01-01", "2026-08-30")]


def wf_configs(a: argparse.Namespace) -> list[tuple[str, dict]]:
    """核心网格 (经 stage3/4 筛过的两个家族 × 仓位 × 锚), 16 组/折。"""
    cfgs = []
    for anchor in (ANCHOR_RTH, ANCHOR_ETH):
        for sizing, fixed_qty, risk_pct in ((SZ_FIXED, 1, 0.0), (SZ_RISK, 1, 0.01)):
            for em, ms, buf in (("first_only", 0.0, 0), ("first_only", 0.10, 0),
                                ("always_in", 0.10, 0), ("always_in", 0.10, 4)):
                sfx = "f1" if sizing == SZ_FIXED else "r1"
                tag = f"{anchor}_{sfx}_{em[:4]}_ms{ms:g}_b{buf:g}"
                cfgs.append((tag, dict(anchor=anchor, sizing=sizing,
                                       fixed_qty=fixed_qty, risk_pct=risk_pct,
                                       entry_mode=em, min_stop_atr=ms, buffer=buf)))
    return cfgs


def run_walk_forward(a: argparse.Namespace, bar_min: int):
    ds = get_ds(bar_min)
    cfgs = wf_configs(a)
    slip = a.slip
    oos_rows, picks = [], []
    for tr0, tr1, te0, te1 in WF_FOLDS:
        best_tag, best_pr, best_sharpe = None, None, -1e9
        for tag, over in cfgs:
            pr = make_params(a, **over)
            sl, el = slice_bounds(ds, tr0, tr1)
            m = Backtest(pr, ds, sl, el, slip_ticks=slip).run().metrics()
            if m["sharpe"] > best_sharpe:
                best_tag, best_pr, best_sharpe = tag, pr, m["sharpe"]
        sl, el = slice_bounds(ds, te0, te1)
        bt = Backtest(best_pr, ds, sl, el, slip_ticks=slip).run()
        m = bt.metrics()
        picks.append([tr0[:4], best_tag, round(best_sharpe, 2)])
        oos_rows.append(grid_row(f"wf_f{tr0[:4]}_{best_tag}", best_pr, bar_min, m)
                        | dict(win_year=f"{te0[:4]}-{te1[:4]}"))
        p(f"[WF 训 {tr0[:4]}-{tr1[:4]}] 选 {best_tag} (训 Sharpe {best_sharpe:.2f}) → "
          f"测试 {te0[:4]}-{te1[:4]}: 笔数={m['n_trades']:,} 终值=${m['final_eq']:,.0f} "
          f"年化={m['annual'] * 100:.1f}% Sharpe={m['sharpe']:.2f} MDD={m['mdd'] * 100:.1f}%")
    df = pd.DataFrame(oos_rows)
    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"vwap_wf_b{bar_min}.csv"
    df.to_csv(out, index=False)
    p(f"\nWF OOS 明细 → {out}")
    p(f"训练期选择记录: {picks}")
    cols = ["annual", "sharpe", "mdd", "winrate", "n_trades"]
    p("OOS 汇总 (4 折合并视角, 各折独立):")
    p(df[["win_year", "tag"] + cols].to_string(index=False))


# ===========================================================================
# check 模式: 数据断言 + 双链路交叉验证 + 5m 重采样对账 + 确定性
# ===========================================================================
def mode_check(a: argparse.Namespace):
    ds1 = get_ds(1)                 # 触发 1m 加载 + 加法调整断言 (合约期内 D 恒定)
    raw, d_adj = _PAIR_CACHE["pair"]
    p("== check 1/4: 加法调整断言 (同合约期 D 恒定) — load_1m_pair 内全量通过 ✓")
    p(f"== check 2/4: 双链路交叉验证 (FSM vs 独立向量化复算), 基线族 = "
      f"always_in/buf0/无午间/双边/固定手数 ==")
    for bar_min in (1, 5):
        ds = get_ds(bar_min)
        for anchor in (ANCHOR_RTH, ANCHOR_ETH):
            pr = make_params(a, anchor=anchor, sizing=SZ_FIXED, buffer_ticks=0,
                             entry_mode="always_in", midday_flat=False, side="both")
            sl, el = slice_bounds(ds, a.start, a.end)
            bt = Backtest(pr, ds, sl, el, slip_ticks=a.slip).run()
            m = bt.metrics()
            cost_side = COMMISSION_PER_CONTRACT + a.slip * TICK * MULTIPLIER
            gross_fsm = m["final_eq"] - STARTING_CAPITAL + 2 * m["n_entries"] * cost_side
            vc = vector_check(ds, pr, sl, el, cost_side)
            ok_g = abs(vc["gross"] - gross_fsm) < 0.01
            ok_f = vc["entries"] == m["n_entries"]
            p(f"[check] bar{bar_min} {anchor}: FSM 毛 {gross_fsm:,.2f}/{m['n_entries']:,} 入场 | "
              f"向量化 {vc['gross']:,.2f}/{vc['entries']:,} 入场 → "
              f"毛利{'✓' if ok_g else '✗✗✗'} 入场数{'✓' if ok_f else '✗✗✗'}")
            if not (ok_g and ok_f):
                raise AssertionError("双链路交叉验证失败")

    p("== check 3/4: 5m 重采样 vs v5.0 nq_5min_rth.parquet (调整价对账) ==")
    ds5 = get_ds(5)
    ref = pd.read_parquet(DATA_5M_RTH)
    ref.index = ref.index.tz_convert(ET)
    raw = raw[["open", "high", "low", "close", "volume"]]
    rth5 = resample_5m(raw)
    t5 = rth5.index.hour * 60 + rth5.index.minute
    rth5 = rth5[(t5 >= T_RTH_START_MIN) & (t5 < T_RTH_END_MIN)]   # 只对账 RTH 窗口
    for d in ("2021-06-01", "2023-03-15", "2025-11-04"):
        dts = pd.Timestamp(d, tz=ET)     # naive Timestamp 与 tz-aware 索引比较恒为 False
        ours = rth5[rth5.index.normalize() == dts]
        refd = ref[ref.index.normalize() == dts]
        m1 = raw.index.normalize() == dts
        if not len(ours) or not len(refd) or not m1.any():
            p(f"  {d}: 样本缺失 ({len(ours)}/{len(refd)}) 跳过")
            continue
        dd = d_adj[m1].iloc[-1]
        diff = (refd["close"] - ours["close"]).round(2)
        ok = bool((diff == round(float(diff.iloc[0]), 2)).all()
                  and abs(float(diff.iloc[0]) - float(dd)) < 0.26)
        p(f"  {d}: bar 数 {len(ours)}/{len(refd)}, close 差唯一值 {diff.unique()[:3]} "
          f"(该日 D={float(dd):.2f}) → {'✓' if ok else '✗ 需人工检查'}")

    p("== check 4/4: 确定性 (同参重跑终值逐位一致) ==")
    ds1 = get_ds(1)
    pr = make_params(a)
    sl, el = slice_bounds(ds1, a.start, a.end)
    b1 = Backtest(pr, ds1, sl, el, slip_ticks=a.slip).run().metrics()
    b2 = Backtest(pr, ds1, sl, el, slip_ticks=a.slip).run().metrics()
    p(f"  终值 {b1['final_eq']} vs {b2['final_eq']} → "
      f"{'✓' if b1['final_eq'] == b2['final_eq'] else '✗'}")


# ===========================================================================
# CLI
# ===========================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="VWAP Trend Trading backtest")
    ap.add_argument("--run", default="single", choices=["single", "grid", "check"],
                    help="运行模式 (与策略 entry-mode 无关)")
    ap.add_argument("--anchor", default=ANCHOR_RTH, choices=[ANCHOR_RTH, ANCHOR_ETH])
    ap.add_argument("--bar-min", type=int, default=1, choices=[1, 5])
    ap.add_argument("--start", default=START_DATE)
    ap.add_argument("--end", default=END_DATE)
    ap.add_argument("--sizing", default=SZ_FIXED, choices=[SZ_FIXED, SZ_PAPER, SZ_RISK])
    ap.add_argument("--fixed-qty", type=int, default=1)
    ap.add_argument("--paper-lev", type=float, default=2.0)
    ap.add_argument("--risk-pct", type=float, default=0.01)
    ap.add_argument("--delay", type=int, default=1, help="第 N 根 RTH bar 收盘首次入场")
    ap.add_argument("--entry-mode", default="always_in", choices=["always_in", "first_only"])
    ap.add_argument("--buffer", type=float, default=0.0, help="出场缓冲 (tick)")
    ap.add_argument("--min-stop-atr", type=float, default=0.0,
                    help="最小入场止损距离 = frac×ATR14, 0=不过滤")
    ap.add_argument("--midday-flat", action="store_true", help="12:00-15:00 ET 强制空仓")
    ap.add_argument("--side", default="both", choices=["both", "long_only"])
    ap.add_argument("--slip", type=int, default=1, help="每边滑点 (tick)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--grid", default=None,
                    help="stage1|stage1b5|stage2|stage3|wf")
    ap.add_argument("--no-save", action="store_true")
    return ap


def build_grids(a: argparse.Namespace) -> tuple[str, list[tuple[str, VwapParams]]]:
    global _CFG_SLIP
    _CFG_SLIP = {}
    g = a.grid or "stage1"
    cfgs: list[tuple[str, VwapParams]] = []
    if g == "stage1":            # Q1: RTH vs ETH 锚 × 滑点鲁棒性 (1m)
        for anchor in (ANCHOR_RTH, ANCHOR_ETH):
            for slip in (0, 1, 2):
                tag = f"{anchor}_s{slip}"
                cfgs.append((tag, make_params(a, anchor=anchor, sizing=SZ_FIXED,
                                              fixed_qty=1, entry_mode="always_in")))
                _CFG_SLIP[tag] = slip
    elif g == "stage1b5":        # Q1 的 5min 版
        for anchor in (ANCHOR_RTH, ANCHOR_ETH):
            for slip in (0, 1, 2):
                tag = f"{anchor}_b5_s{slip}"
                cfgs.append((tag, make_params(a, anchor=anchor, sizing=SZ_FIXED,
                                              fixed_qty=1, entry_mode="always_in")))
                _CFG_SLIP[tag] = slip
    elif g == "stage2":          # Q2: 仓位法对比 (--anchor 选优锚)
        best = a.anchor
        cfgs.append((f"fixed1_{best}", make_params(a, anchor=best, sizing=SZ_FIXED)))
        for lev in (1.0, 2.0, 3.0):
            cfgs.append((f"paper_lev{lev:g}_{best}",
                         make_params(a, anchor=best, sizing=SZ_PAPER, paper_lev=lev)))
        for rp in (0.005, 0.01, 0.02):
            cfgs.append((f"risk{rp * 100:g}pct_{best}",
                         make_params(a, anchor=best, sizing=SZ_RISK, risk_pct=rp)))
    elif g == "stage3":          # 发散: OFAT (基线 = 固定1手, stage1/2 的幸存口径)
        base = dict(sizing=SZ_FIXED, fixed_qty=1)
        for em in ("always_in", "first_only"):
            for buf in (0, 2, 4):
                cfgs.append((f"mode{em[:4]}_buf{buf:g}",
                             make_params(a, entry_mode=em, buffer=buf, **base)))
        for d in (1, 3, 6):
            cfgs.append((f"delay{d}", make_params(a, delay=d, **base)))
        cfgs.append(("midday_flat", make_params(a, midday_flat=True, **base)))
        cfgs.append(("long_only", make_params(a, side="long_only", **base)))
        for ms in (0.05, 0.10):
            cfgs.append((f"minstop{ms:g}atr", make_params(a, min_stop_atr=ms, **base)))
    elif g == "stage4":          # 联合格: first_only × minstop × delay + always_in × minstop × buffer
        base = dict(sizing=SZ_FIXED, fixed_qty=1)
        for anchor in (ANCHOR_RTH, ANCHOR_ETH):
            for ms in (0.0, 0.05, 0.10):
                for dly in (1, 3):
                    cfgs.append((f"first_{anchor}_ms{ms:g}_d{dly}",
                                 make_params(a, anchor=anchor, entry_mode="first_only",
                                             min_stop_atr=ms, delay=dly, **base)))
        for anchor in (ANCHOR_RTH, ANCHOR_ETH):
            for ms in (0.05, 0.10):
                for buf in (0, 4):
                    cfgs.append((f"alwa_{anchor}_ms{ms:g}_b{buf:g}",
                                 make_params(a, anchor=anchor, entry_mode="always_in",
                                             min_stop_atr=ms, buffer=buf, **base)))
    else:
        raise SystemExit(f"未知 grid: {g}")
    return g, cfgs


def main():
    a = build_parser().parse_args()
    if a.run == "check":
        mode_check(a)
        return
    if a.run == "grid" and a.grid == "wf":
        for bar_min in (a.bar_min,):
            run_walk_forward(a, bar_min)
        return

    ds = get_ds(a.bar_min)
    if a.run == "single":
        run_one(ds, make_params(a), a.tag, a.slip, a.start, a.end,
                save_csv=not a.no_save)
        return
    name, configs = build_grids(a)
    if a.run == "grid" and a.grid != "wf":
        name = f"{name}_{a.anchor}_b{a.bar_min}"   # 多次运行不互相覆盖
    run_grid(ds, configs, name, a.slip, a.start, a.end)


if __name__ == "__main__":
    main()
