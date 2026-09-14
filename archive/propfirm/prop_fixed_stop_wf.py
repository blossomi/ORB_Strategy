# -*- coding: utf-8 -*-
"""
prop_fixed_stop_wf.py  (propfirm/)
===================================
给固定止损补上转正前的两块验证 (主线 ATR 7.5% 有 6/6 walk-forward 背书, 它没有):

[1] Walk-Forward (2019 起, 3训1测 × 5 窗口滚动)
    对齐 GLM_working/stage_c_wf.py 的精神 (训练全网格按 Calmar 选 θ* → 测试段
    全网格跑「固定组合 OOS 路径」), 两点适配:
      - 样本 2019 起 7.6 年 → 训练窗口 3 年滚动 (主线 5 年, 样本短一档);
      - 选参标准用 **R 口径 Calmar** (总R / |累计R最大回撤|) —— prop 实际是固定
        手数不复利, 1R 美元恒定, 累计 R 回撤直接对应美元回撤; 主线金额 Calmar
        服务复利仓位, 口径不同但精神一致 (右偏长尾策略降权 Sharpe)。
    族内候选: 固定止损 9 档 {10..30 含中间档} vs ATR 自适应 {5%, 7.5%, 10%}。
    引擎只跑全样本 (每参数 1 次), 训练/测试指标全部从逐笔 R 序列切片计算 ——
    策略无跨日状态 (ATR/区间来自历史), 切片 = 单独跑该窗口 (warm-up 用全历史 parquet)。

[2] 边界敏感性 (全档全样本)
    9 档的 总R/胜率/连亏/R回撤/Calmar-R + 相邻档变化 + q=2 考核通过率:
    相邻档跳变 = 尖峰过拟合; 平滑 = 稳健平台。

用法: python prop_fixed_stop_wf.py     # ATR 5%/10% 两次引擎 (~30s) + 全部切片计算
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import orb_core_v84 as core  # noqa: E402
import prop_sim as ps  # noqa: E402
from prop_be_variants import MULT, STOP_FRAC, run_and_extract  # noqa: E402

RESULTS = HERE / "results"
FIXED_STOPS = (10, 12.5, 15, 17.5, 20, 22.5, 25, 27.5, 30)
ATR_FRACS = (0.05, 0.075, 0.10)
MIN_TRADES = 100

# 3训1测滚动 × 5 窗口 (2019 起 7.6 年; 末段测试到 2026-08-30)
WINDOWS = [
    dict(id=1, tr=("2019-01-01", "2021-12-31"), te=("2022-01-01", "2022-12-31")),
    dict(id=2, tr=("2020-01-01", "2022-12-31"), te=("2023-01-01", "2023-12-31")),
    dict(id=3, tr=("2021-01-01", "2023-12-31"), te=("2024-01-01", "2024-12-31")),
    dict(id=4, tr=("2022-01-01", "2024-12-31"), te=("2025-01-01", "2025-12-31")),
    dict(id=5, tr=("2023-01-01", "2025-12-31"), te=("2026-01-01", "2026-08-30")),
]


def load_series() -> dict[str, pd.DataFrame]:
    """载入全部参数的逐笔序列: {参数标签: df(date, pnl_pc, stop_pt, r)}。"""
    series = {}
    for pt in FIXED_STOPS:
        f = RESULTS / f"prop_trades_fixed{pt:g}pt_2019.csv"
        series[f"固定{pt:g}pt"] = ps.load_trades_engine(str(f))
    series["ATR 5%"] = ps.load_trades_engine(str(RESULTS / "prop_trades_atr5_2019.csv"))
    series["ATR 7.5%(主线)"] = ps.load_trades_engine(
        str(RESULTS / "prop_trades_be5_2019.csv"))
    series["ATR 10%"] = ps.load_trades_engine(str(RESULTS / "prop_trades_atr10_2019.csv"))
    for v in series.values():
        v["date"] = pd.to_datetime(v["date"]).dt.date
    return series


def slice_metrics(df: pd.DataFrame, lo: str, hi: str) -> dict:
    """R 口径切片指标: 总R / 胜率 / 笔数 / 最大连亏 / 累计R最大回撤 / Calmar-R。"""
    d = df[(df["date"] >= pd.Timestamp(lo).date())
           & (df["date"] <= pd.Timestamp(hi).date())]
    n = len(d)
    if n == 0:
        return dict(n_trades=0, tot_r=0.0, maxdd_r=0.0, calmar_r=0.0, win=0.0, streak=0)
    day = d.groupby("date")["r"].sum()
    cum = day.cumsum()
    mdd = float((cum - cum.cummax()).min())
    signs = (d["r"] > 0).to_numpy()
    streak = best = 0
    for s in signs:
        streak = 0 if s else streak + 1
        best = max(best, streak)
    tot = float(cum.iloc[-1])
    return dict(n_trades=n, tot_r=tot, maxdd_r=mdd,
                calmar_r=tot / abs(mdd) if mdd < 0 else float("inf"),
                win=float((d["r"] > 0).mean()), streak=best)


def run_atr_engine() -> None:
    """跑 ATR 5% / 10% 两档引擎 (7.5% 复用 prop_be_variants 已有产出)。"""
    core.configure(multiplier=MULT, slippage_ticks=1.0, capital=25_000)
    data = core.build_data("2019-01-01", "2026-08-30")
    core.ensure_bars(data)
    for frac in (0.05, 0.10):
        out = RESULTS / f"prop_trades_atr{int(frac * 100)}_2019.csv"
        if out.exists():
            print(f"  ATR {frac:.1%} 已有 -> {out.name}")
            continue
        df = run_and_extract(5.0, data, stop_frac=frac)
        df.to_csv(out, index=False)
        print(f"  ATR {frac:.1%}: {len(df)} 笔 -> {out.name}")


def walk_forward(series: dict[str, pd.DataFrame], family: dict[str, list[str]]) -> None:
    """family = {族名: [参数标签]}。训练 Calmar-R 选 θ*, 测试段全网格 OOS。"""
    print("\n" + "=" * 84)
    print("[1] Walk-Forward: 3训1测 × 5 窗口, 训练段按 Calmar-R 选 θ* (n_trades≥100)")
    print("=" * 84)
    oos_rows = []
    for fam_name, labels in family.items():
        print(f"\n--- {fam_name} (候选: {', '.join(labels)}) ---")
        print(f"{'窗口':>3} | {'训练段':<12} | {'θ*':>13} | {'训练CalmarR':>9} | "
              f"{'OOS总R':>7} {'OOS R回撤':>8} {'OOS笔数':>5}")
        print("-" * 78)
        for w in WINDOWS:
            train = {}
            for lab in labels:
                m = slice_metrics(series[lab], *w["tr"])
                train[lab] = m
            valid = {k: v for k, v in train.items() if v["n_trades"] >= MIN_TRADES}
            if not valid:
                print(f"{w['id']:>3} | {w['tr'][0][:4]}~{w['tr'][1][:4]} | "
                      f"(训练段无可用参数)")
                continue
            theta = max(valid, key=lambda k: valid[k]["calmar_r"])
            om = slice_metrics(series[theta], *w["te"])
            print(f"{w['id']:>3} | {w['tr'][0][:4]}~{w['tr'][1][:4]} | {theta:>13} | "
                  f"{valid[theta]['calmar_r']:>9.2f} | {om['tot_r']:>7.1f} "
                  f"{om['maxdd_r']:>8.1f} {om['n_trades']:>5}")
            oos_rows.append(dict(family=fam_name, window=w["id"], te=w["te"][0][:4],
                                 theta=theta, **om))
        # 固定组合 OOS 路径 (每个候选在 5 段测试年, 不逐年调参)
        print(f"  固定组合 OOS 路径 ({fam_name}, 不调参):")
        print(f"  {'参数':>13} |" + "".join(f" {w['te'][0][:4]:>12}" for w in WINDOWS)
              + " |     汇总")
        for lab in labels:
            cells, totrs = [], []
            for w in WINDOWS:
                m = slice_metrics(series[lab], *w["te"])
                totrs.append(m["tot_r"])
                cells.append(f"{m['tot_r']:>7.1f}R/{m['maxdd_r']:>4.1f}")
            n_pos = sum(1 for t in totrs if t > 0)
            med = sorted(totrs)[len(totrs) // 2]
            print(f"  {lab:>13} |" + "".join(f" {c:>12}" for c in cells)
                  + f" | {n_pos}/5 正, R中位 {med:+.1f}")
    pd.DataFrame(oos_rows).to_csv(RESULTS / "fixed_stop_wf_oos.csv", index=False)
    print(f"\nθ* 样本外明细已存 results/fixed_stop_wf_oos.csv")


def boundary_sensitivity(series: dict[str, pd.DataFrame]) -> None:
    print("\n" + "=" * 84)
    print("[2] 边界敏感性: 固定止损 9 档全样本 (2019-2026) + q=2 考核通过率")
    print("=" * 84)
    labels = [f"固定{pt:g}pt" for pt in FIXED_STOPS]
    rows = []
    for lab in labels:
        m = slice_metrics(series[lab], "2019-01-01", "2026-08-30")
        res = ps.prop_run(series[lab], MULT, qty_ladder=(2,),
                          scenarios={s["name"]: s for s in (ps.REAL_FIRM,)})
        m["pass_q2"] = float(res[res["mode"] == "eod"]["pass_rate"].iloc[0])
        rows.append(dict(档位=lab, **m))
    df = pd.DataFrame(rows)
    df["ΔCalmarR(相邻)"] = df["calmar_r"].diff().round(2)
    print(f"{'档位':>10} | {'总R':>7} {'胜率':>5} {'连亏':>4} {'R回撤':>7} "
          f"{'CalmarR':>7} {'Δ相邻':>6} | {'q=2通过%':>7}")
    print("-" * 72)
    for _, r in df.iterrows():
        print(f"{r['档位']:>10} | {r['tot_r']:>7.1f} {r['win'] * 100:>4.0f}% "
              f"{r['streak']:>4} {r['maxdd_r']:>7.1f} {r['calmar_r']:>7.2f} "
              f"{r['ΔCalmarR(相邻)']:>6} | {r['pass_q2']:>7.1f}")
    df.to_csv(RESULTS / "fixed_stop_boundary_9tiers.csv", index=False)
    print("\n已存 results/fixed_stop_boundary_9tiers.csv")
    # 平滑性判定
    cm = df["calmar_r"].to_numpy()
    jumps = np.abs(np.diff(cm)) / np.maximum(np.abs(cm[:-1]), 1e-9)
    print(f"相邻档 CalmarR 最大相对跳变: {jumps.max() * 100:.0f}% "
          f"(>30% = 尖峰过拟合风险; 平滑平台 = 稳健)")


def main() -> None:
    print("[0/2] 跑 ATR 5% / 10% 对照引擎 ...")
    run_atr_engine()
    series = load_series()
    family = {
        "固定止损族": [f"固定{pt:g}pt" for pt in FIXED_STOPS],
        "ATR自适应族": [f"ATR {f * 100:g}%" if f != 0.075 else "ATR 7.5%(主线)"
                        for f in ATR_FRACS],
    }
    walk_forward(series, family)
    boundary_sensitivity(series)


if __name__ == "__main__":
    main()
