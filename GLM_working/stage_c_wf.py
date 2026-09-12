# -*- coding: utf-8 -*-
"""
stage_c_wf.py  (GLM_working)
==============================
Stage C: 合适区间内 Walk-Forward 滚动验证 —— 筛出最终参数组合。

方法 (对齐 ORB_strategy/walk_forward.py 的 5训1测 × 6 窗口框架, 两点完善):
  1) 成本含滑点 ($0.5 + 2 tick/手/边; 原框架只算手续费, 硬规则 6);
  2) 每窗口除「训练选 θ* → 样本外验证」外, 还把**整个网格**在每段测试年上跑一遍,
     得到「固定组合 OOS 路径表」—— 回答「选定单一组合, 6 段样本外是否都稳」,
     而不只是「每段各自最优参数的样本外」(后者隐含每年重调参, 实盘未必做得到)。

选参口径: 训练段 Calmar 最大 (右偏长尾 + 复利策略, Calmar 比 Sharpe 稳健;
         heatmap_v8_6 已证明两口径结论不同, 以 Calmar 为主、Sharpe 为辅)。
过滤: 训练段 n_trades >= 100。

用法:
  ../.venv/bin/python stage_c_wf.py --stops 0.0075,0.01,0.015 --bes 3,5,8
"""
import argparse
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CAPITAL = 250_000
MIN_TRADES = 100
WORKERS = 6

# 6 个滚动窗口 (训练 5 年 → 测试 1 年; 末段测试到 2026-08-30), 同 walk_forward.py
WINDOWS = [
    dict(id=1, tr_s="2016-01-01", tr_e="2020-12-31", te_s="2021-01-01", te_e="2021-12-31"),
    dict(id=2, tr_s="2017-01-01", tr_e="2021-12-31", te_s="2022-01-01", te_e="2022-12-31"),
    dict(id=3, tr_s="2018-01-01", tr_e="2022-12-31", te_s="2023-01-01", te_e="2023-12-31"),
    dict(id=4, tr_s="2019-01-01", tr_e="2023-12-31", te_s="2024-01-01", te_e="2024-12-31"),
    dict(id=5, tr_s="2020-01-01", tr_e="2024-12-31", te_s="2025-01-01", te_e="2025-12-31"),
    dict(id=6, tr_s="2021-01-01", tr_e="2025-12-31", te_s="2026-01-01", te_e="2026-08-30"),
]


def run_window(job):
    """一个窗口: 训练段跑全网格选 θ* (Calmar), 测试段跑全网格 (固定组合 OOS 路径)。"""
    import orb_core_v84 as core
    core.configure(job["mult"], job["slip"], job["capital"])
    stops, bes, risk = job["stops"], job["bes"], job["risk"]
    grid = [(s, b) for s in stops for b in bes]

    tr = core.build_data(job["tr_s"], job["tr_e"])
    train = {}
    for s, b in grid:
        m = core.run_backtest(s, b, risk, tr, job["capital"])
        train[(s, b)] = m

    valid = {k: m for k, m in train.items()
             if m["n_trades"] >= MIN_TRADES and m["mdd"] > -0.70}
    theta = max(valid, key=lambda k: valid[k]["calmar"]) if valid else None

    te = core.build_data(job["te_s"], job["te_e"])
    test = {}
    for s, b in grid:   # 整个网格都跑测试段 → 固定组合 OOS 路径
        m = core.run_backtest(s, b, risk, te, job["capital"])
        test[(s, b)] = m

    return dict(id=job["id"], tr_s=job["tr_s"], tr_e=job["tr_e"],
                te_s=job["te_s"], te_e=job["te_e"],
                theta=theta, train={f"{s}|{b}": m for (s, b), m in train.items()},
                test={f"{s}|{b}": m for (s, b), m in test.items()})


def fmt_be(b):
    return "无" if b is None else f"{b:g}R"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stops", required=True, help="逗号分隔, 如 0.0075,0.01,0.015")
    p.add_argument("--bes", required=True, help="逗号分隔, -1 表示不拉保本, 如 3,5,8,-1")
    p.add_argument("--risk", type=float, default=0.007)
    p.add_argument("--slip", type=float, default=2.0, help="每手每边滑点 tick 数")
    p.add_argument("--capital", type=float, default=250_000)
    p.add_argument("--mult", type=float, default=20.0, help="每点乘数: NQ=20, MNQ=2")
    args = p.parse_args()

    stops = [float(x) for x in args.stops.split(",")]
    bes = [None if float(x) < 0 else float(x) for x in args.bes.split(",")]
    grid = [(s, b) for s in stops for b in bes]
    mult_name = "MNQ" if args.mult <= 2 else "NQ"

    print(f"Stage C Walk-Forward: 网格 {len(stops)} 止损 × {len(bes)} 保本 = {len(grid)} 组合, "
          f"risk={args.risk:.1%}, 本金 ${args.capital:,.0f}, {mult_name} ${args.mult:g}/点, "
          f"滑点 {args.slip:g} tick, 训练段按 Calmar 选 θ* (n_trades>={MIN_TRADES}), "
          f"{WORKERS} 进程 × 6 窗口", flush=True)

    jobs = [dict(w, stops=stops, bes=bes, risk=args.risk, mult=args.mult,
                 slip=args.slip, capital=args.capital) for w in WINDOWS]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        results = sorted(ex.map(run_window, jobs), key=lambda r: r["id"])
    print(f"全部完成, 墙钟 {time.time()-t0:.0f}s\n", flush=True)

    # ---- [1] 每窗口 θ* 及其样本外表现 ----
    print("[1] 每窗口训练选出的 θ* 与样本外表现")
    print(f"{'窗口':>3} | {'测试段':<11} | {'θ*':>16} | {'训练Calmar':>9} {'训练Sharpe':>9} | "
          f"{'OOS年化':>8} {'OOS MDD':>8} {'OOS Sharpe':>9} {'OOS笔数':>6}")
    print("-" * 95)
    for r in results:
        if r["theta"] is None:
            print(f"{r['id']:>3} | {r['te_s']}~{r['te_e']} | (训练段无可用参数)")
            continue
        k = f"{r['theta'][0]}|{r['theta'][1]}"
        tm = r["train"][k]
        om = r["test"][k]
        theta_label = f"{r['theta'][0]:.2%}×{fmt_be(r['theta'][1])}"
        print(f"{r['id']:>3} | {r['te_s'][:4]} | {theta_label:>16} | "
              f"{tm['calmar']:>9.2f} {tm['sharpe']:>9.2f} | "
              f"{om['annual']*100:>7.1f}% {om['mdd']*100:>7.1f}% {om['sharpe']:>9.2f} "
              f"{om['n_trades']:>6}")

    # ---- [2] 固定组合 OOS 路径表 ----
    print("\n[2] 固定组合 OOS 路径 (每个组合在 6 段测试年上的表现, 不逐年调参)")
    header = f"{'组合':>16} |" + "".join(f" {r['te_s'][:4]:>14}" for r in results) + " |  汇总"
    print(header)
    print(f"{'':>16} |" + "".join(f" {'年化/MDD/Sharpe':>14}" for _ in results) + " |")
    print("-" * len(header))
    summary_rows = []
    for (s, b) in grid:
        cells, anns, mdds, shps = [], [], [], []
        for r in results:
            m = r["test"][f"{s}|{b}"]
            anns.append(m["annual"])
            mdds.append(m["mdd"])
            shps.append(m["sharpe"])
            cells.append(f"{m['annual']*100:>5.0f}/{m['mdd']*100:>4.0f}%/{m['sharpe']:>4.2f}")
        n_pos = sum(1 for a in anns if a > 0)
        med_ann = sorted(anns)[len(anns) // 2]
        worst = min(anns)
        label = f"{s:.2%}×{fmt_be(b)}"
        summary = f"{n_pos}/6 正, 年化中位 {med_ann*100:.0f}%, 最差 {worst*100:.0f}%"
        print(f"{label:>16} |" + "".join(f" {c:>14}" for c in cells) + f" |  {summary}")
        summary_rows.append(dict(stop=s, be=b, n_pos=n_pos, med_annual=med_ann,
                                 worst_annual=worst, med_sharpe=sorted(shps)[len(shps)//2],
                                 worst_mdd=min(mdds)))

    # ---- [3] 写 CSV ----
    os.makedirs("results", exist_ok=True)
    suffix = f"_slip{args.slip:g}_cap{args.capital/1000:g}k_{mult_name.lower()}"
    out_csv = f"results/stage_c_wf_oos{suffix}.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["stop", "be", "n_pos", "med_annual",
                                          "worst_annual", "med_sharpe", "worst_mdd"])
        w.writeheader()
        w.writerows(summary_rows)
    print(f"\n已写 {out_csv}")

    # ---- [4] 数据健康检查 (硬规则 2) ----
    cant = [(r["id"], k, m["n_cant_afford"])
            for r in results for k, m in r["test"].items()
            if m["n_cant_afford"] > 0]
    if cant:
        print(f"\n⚠ 有 {len(cant)} 个 (窗口,组合) 样本外出现「买不起 1 手」天, 结果受样本缺口污染:")
        for wid, k, n in sorted(cant):
            print(f"   窗口{wid} {k}: {n} 天")
    else:
        print("\n✓ 全部窗口×组合样本外无「买不起 1 手」天 (硬规则 2 通过)")

    # ---- [5] 结论 ----
    print("\n[3] 结论要点")
    best_fixed = max(summary_rows, key=lambda r: (r["n_pos"], r["med_annual"]))
    print(f"   固定组合最优 (样本外盈利段数优先, 其次年化中位): "
          f"{best_fixed['stop']:.2%} × {fmt_be(best_fixed['be'])} — "
          f"{best_fixed['n_pos']}/6 段为正, 年化中位 {best_fixed['med_annual']*100:.1f}%, "
          f"最差段 {best_fixed['worst_annual']*100:.1f}%, 最差 MDD {best_fixed['worst_mdd']*100:.1f}%")


if __name__ == "__main__":
    main()
