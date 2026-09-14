# -*- coding: utf-8 -*-
"""
stage_b_risk.py  (GLM_working)
==============================
Stage B: 在 Stage A 选出的头部 (止损, 保本) 组合上扫仓位风险 risk_per_trade。

原理: Sharpe/Sortino/PF 对仓位风险近似不变 (同一段交易按比例放大),
风险只影响 年化(复利放大) / MDD(回撤放大) / 是否爆仓、买得起 1 手、被 MAX_QTY 压制。
→ 仓位参数的「合适区间」= 满足回撤约束下年化最大的那一段。

用法:
  ../.venv/bin/python stage_b_risk.py --combo 0.01,5 --combo 0.015,3 ...
  (--combo 止损,保本R; 保本填 -1 表示不拉保本; 默认风险网格 0.3%~1.5%)
"""
import argparse
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

START, END = "2016-01-01", "2026-08-30"
CAPITAL = 250_000
WORKERS = 5
RISKS = [0.003, 0.005, 0.007, 0.010, 0.015]


def run_chunk(jobs):
    """jobs: [(stop, be, risk), ...] 同一 (stop,be) 的风险档在 worker 内顺序跑。"""
    import orb_core_v84 as core
    data = core.build_data(START, END)
    out = []
    for stop, be, risk in jobs:
        m = core.run_backtest(stop, be, risk, data, CAPITAL)
        out.append(m)
        be_label = "无" if be is None else f"{be:g}R"
        print(f"  [done] stop={stop:.2%} be={be_label} risk={risk:.1%} "
              f"年化={m['annual']*100:6.1f}% MDD={m['mdd']*100:6.1f}% "
              f"Calmar={m['calmar']:.2f} Sharpe={m['sharpe']:.2f} "
              f"买不起={m['n_cant_afford']} 被压={m['n_capped']}", flush=True)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--combo", action="append", required=True,
                   help="止损,保本R (如 0.01,5; 保本 -1 = 不拉保本)")
    p.add_argument("--risks", default=",".join(f"{r:g}" for r in RISKS))
    args = p.parse_args()

    combos = []
    for c in args.combo:
        s, b = c.split(",")
        combos.append((float(s), None if float(b) < 0 else float(b)))
    risks = [float(x) for x in args.risks.split(",")]

    jobs = [(s, b, r) for (s, b) in combos for r in risks]
    print(f"Stage B 仓位扫描: {len(combos)} 组合 × {len(risks)} 风险档 = {len(jobs)} 次, "
          f"{WORKERS} 进程, 全样本 {START}~{END}, 本金 ${CAPITAL:,}", flush=True)

    chunks = [jobs[i::WORKERS] for i in range(WORKERS)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        results = []
        for part in ex.map(run_chunk, chunks):
            results.extend(part)
    print(f"全部完成, 墙钟 {time.time()-t0:.0f}s", flush=True)

    os.makedirs("results", exist_ok=True)
    out_csv = "results/stage_b_risk.csv"
    cols = ["stop_frac", "be_r", "risk_per_trade", "annual", "mdd", "calmar",
            "sharpe", "sortino", "winrate", "pf", "n_trades", "n_capped",
            "n_cant_afford", "final_equity"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for m in results:
            row = dict(m)
            row["pf"] = 99.0 if m["pf"] == float("inf") else m["pf"]
            w.writerow(row)
    print(f"已写 {out_csv}")

    for s, b in combos:
        rows = sorted([m for m in results if m["stop_frac"] == s and m["be_r"] == b],
                      key=lambda m: m["risk_per_trade"])
        be_label = "无" if b is None else f"{b:g}R"
        print(f"\n===== stop={s:.2%} × be={be_label} =====")
        print(f"{'风险':>7} | {'年化':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>7} "
              f"{'Sortino':>8} | {'最终权益':>12} {'买不起':>6} {'被压':>5}")
        for m in rows:
            print(f"{m['risk_per_trade']:>7.2%} | {m['annual']*100:>7.1f}% "
                  f"{m['mdd']*100:>7.1f}% {m['calmar']:>8.2f} {m['sharpe']:>7.2f} "
                  f"{m['sortino']:>8.2f} | ${m['final_equity']:>11,.0f} "
                  f"{m['n_cant_afford']:>6} {m['n_capped']:>5}")


if __name__ == "__main__":
    main()
