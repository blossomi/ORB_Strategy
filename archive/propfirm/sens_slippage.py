# -*- coding: utf-8 -*-
"""滑点敏感性: $1.9M 里有多少是"1 tick 滑点"假设撑起来的 (唯一变量 = 每边滑点 tick 数)。"""
import sys
from pathlib import Path
import numpy as np
import pandas as pd

HERE = Path("/Users/blossomx/workspaces/Deepseek_Harness/propfirm")
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))
import orb_core_v84 as core
import prop_be_variants as pbv

CAP = 25_000.0
CACHE = HERE / "results"


def equity_path(pnl_pc, stop_pt, cap=200, risk=0.007):
    """复利权益路径: q = floor(risk×E/(stop×2)), 上限 cap。"""
    eq, out, qs = CAP, [], []
    for p, s in zip(pnl_pc, stop_pt):
        q = max(1, min(cap, int(risk * eq / (s * 2))))
        eq += q * p
        out.append(eq)
        qs.append(q)
    return np.array(out), np.array(qs)


for slip in (1.0, 2.0, 3.0):
    f = CACHE / f"sens_slip{slip:g}.csv"
    if not f.exists():
        core.configure(multiplier=2.0, slippage_ticks=slip, capital=CAP)
        data = core.build_data("2019-01-01", "2026-08-30")
        core.ensure_bars(data)
        df = pbv.run_and_extract(5.0, data)
        df.to_csv(f, index=False)
        print(f"  引擎跑完 slip={slip:g}: {len(df)} 笔 -> {f.name}", flush=True)
    d = pd.read_csv(f)
    eq, qs = equity_path(d["pnl_pc"].to_numpy(), d["stop_pt"].to_numpy())
    dd = ((eq - np.maximum.accumulate(eq)) / np.maximum.accumulate(eq)).min()
    yrs = (pd.Timestamp(d["date"].iloc[-1]) - pd.Timestamp(d["date"].iloc[0])).days / 365.25
    fee = d["stop_pt"] * 2  # 占位
    print(f"[滑点 {slip:g} tick/边] 期末 ${eq[-1]:>11,.0f} | 总收益 {eq[-1]/CAP-1:>8.1%} | "
          f"年化 {(eq[-1]/CAP)**(1/yrs)-1:>6.1%} | MDD {dd:>6.1%} | 手数中位 {np.median(qs):.0f} | "
          f"R合计 {d['pnl_pc'].div(d['stop_pt']*2).sum():.0f}R | 每笔均R {d['pnl_pc'].div(d['stop_pt']*2).mean():.3f}",
          flush=True)
