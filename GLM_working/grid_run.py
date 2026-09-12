# -*- coding: utf-8 -*-
"""
grid_run.py  (GLM_working)
==========================
通用全样本网格跑批: 任意 (止损, 保本) 组合列表 → 追加写 results/grid_extra.csv 并打印表格。
Stage A 的补充锚点 (3% / 10%) 与任何后续复测都用它。

用法:
  ../.venv/bin/python grid_run.py --stops 0.03,0.10 --bes 1,2,3,5,8,12,-1
  (-1 = 不拉保本; --risk 默认 0.007; --append 追加而非覆盖)
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


def run_chunk(jobs):
    import orb_core_v84 as core
    core.configure(jobs["mult"], jobs["slip"], jobs["capital"])
    data = core.build_data(START, END)
    out = []
    for stop, be, risk in jobs["combos"]:
        m = core.run_backtest(stop, be, risk, data, jobs["capital"])
        out.append(m)
        be_label = "无" if be is None else f"{be:g}R"
        print(f"  [done] stop={stop:.2%} be={be_label} 年化={m['annual']*100:6.1f}% "
              f"MDD={m['mdd']*100:6.1f}% Calmar={m['calmar']:.2f} Sharpe={m['sharpe']:.2f} "
              f"买不起={m['n_cant_afford']}", flush=True)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stops", required=True)
    p.add_argument("--bes", required=True, help="-1 = 不拉保本")
    p.add_argument("--risk", type=float, default=0.007)
    p.add_argument("--slip", type=float, default=2.0, help="每手每边滑点 tick 数")
    p.add_argument("--capital", type=float, default=250_000)
    p.add_argument("--mult", type=float, default=20.0, help="每点乘数: NQ=20, MNQ=2")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    stops = [float(x) for x in args.stops.split(",")]
    bes = [None if float(x) < 0 else float(x) for x in args.bes.split(",")]
    combos = [(s, b, args.risk) for s in stops for b in bes]
    mult_name = "MNQ" if args.mult <= 2 else "NQ"
    out_csv = args.out or f"results/grid_slip{args.slip:g}_cap{args.capital/1000:g}k_{mult_name.lower()}.csv"

    print(f"网格: {len(combos)} 组合, {WORKERS} 进程, 全样本 {START}~{END}, "
          f"risk={args.risk:.1%}, 本金 ${args.capital:,.0f}, {mult_name} ${args.mult:g}/点, "
          f"滑点 {args.slip:g} tick/手/边", flush=True)
    # 按 round-robin 分配组合到进程
    chunks = []
    for i in range(WORKERS):
        part = combos[i::WORKERS]
        if part:
            chunks.append(dict(combos=part, mult=args.mult, slip=args.slip,
                               capital=args.capital))
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        results = []
        for part in ex.map(run_chunk, chunks):
            results.extend(part)
    print(f"全部完成, 墙钟 {time.time()-t0:.0f}s\n", flush=True)

    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    cols = ["stop_frac", "be_r", "risk_per_trade", "annual", "mdd", "calmar",
            "sharpe", "sortino", "winrate", "pf", "n_trades", "n_be_moves",
            "n_stopped", "n_be_exits", "n_eod", "n_capped", "n_cant_afford",
            "final_equity"]
    exists = os.path.exists(out_csv)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for m in results:
            row = dict(m)
            row["pf"] = 99.0 if m["pf"] == float("inf") else m["pf"]
            w.writerow(row)
    print(f"已写入 {out_csv}")

    results.sort(key=lambda m: (m["stop_frac"], m["be_r"] if m["be_r"] is not None else 1e9))
    print(f"\n{'止损':>8} {'保本':>6} | {'年化':>8} {'MDD':>8} {'Calmar':>8} {'Sharpe':>7} "
          f"{'Sortino':>8} {'胜率':>7} {'笔数':>6} {'保本触发':>8}")
    for m in results:
        be_label = "无" if m["be_r"] is None else f"{m['be_r']:g}R"
        print(f"{m['stop_frac']:>8.2%} {be_label:>6} | {m['annual']*100:>7.1f}% "
              f"{m['mdd']*100:>7.1f}% {m['calmar']:>8.2f} {m['sharpe']:>7.2f} "
              f"{m['sortino']:>8.2f} {m['winrate']*100:>6.1f}% {m['n_trades']:>6} "
              f"{m['n_be_moves']:>8}")


if __name__ == "__main__":
    main()
