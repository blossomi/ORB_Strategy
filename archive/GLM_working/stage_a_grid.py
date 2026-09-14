# -*- coding: utf-8 -*-
"""
stage_a_grid.py  (GLM_working)
==============================
Stage A: 止损 × 保本 全样本粗扫 —— 找各参数的合适区间。

设计:
  - 固定 risk_per_trade=0.7% (Sharpe/Sortino/PF 对仓位风险近似不变量, 仓位留给 Stage B 扫);
  - 止损网格: 0.5%~1% (用户指定区间, 3 档) + 1.5%/2.5%/5%/7.5% 锚点 (验证区间外是否存在更优,
    7.5% 为 v8_6 现行基准);
  - 保本网格: 1R~12R (6 档) + 不拉保本 (None, 纯持有到收盘 = v8.3 锚点);
  - 全样本 2016-01-01 ~ 2026-08-30, $25万 本金 (保证最宽止损+最低风险也买得起 1 手, 硬规则 2);
  - 成本: $0.5/手/边 + 2 tick 滑点 (硬规则 6);
  - 并行: 5 进程, 每进程构建一次数据跑 ~10 组合。

用法: cd GLM_working && ../.venv/bin/python stage_a_grid.py
输出: results/stage_a_grid.csv + 控制台边际表/排行榜
"""
import csv
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

START, END = "2016-01-01", "2026-08-30"
RISK = 0.007
CAPITAL = 250_000
WORKERS = 5

STOPS = [0.005, 0.0075, 0.01, 0.015, 0.025, 0.05, 0.075]
BES = [1, 2, 3, 5, 8, 12, None]     # None = 不拉保本


def run_chunk(combos):
    """worker 进程: 构建一次数据, 顺序跑完分到的组合。"""
    import orb_core_v84 as core
    data = core.build_data(START, END)
    out = []
    for stop, be in combos:
        t0 = time.time()
        m = core.run_backtest(stop, be, RISK, data, CAPITAL)
        m["secs"] = round(time.time() - t0, 1)
        out.append(m)
        print(f"  [done] stop={stop:.3%} be={be if be is not None else '无'} "
              f"年化={m['annual']*100:6.1f}% MDD={m['mdd']*100:6.1f}% "
              f"Sharpe={m['sharpe']:.2f} Calmar={m['calmar']:.2f} "
              f"({m['secs']}s, {m['n_entries']}笔)", flush=True)
    return out


def main():
    combos = [(s, b) for s in STOPS for b in BES]
    print(f"Stage A 粗扫: {len(STOPS)} 止损 × {len(BES)} 保本 = {len(combos)} 组合, "
          f"{WORKERS} 进程, 全样本 {START}~{END}, risk={RISK:.1%}, 本金 ${CAPITAL:,}, "
          f"成本 ${0.5 + 2*0.25*20}/手/边", flush=True)

    chunks = [combos[i::WORKERS] for i in range(WORKERS)]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        results = []
        for part in ex.map(run_chunk, chunks):
            results.extend(part)
    print(f"\n全部完成, 墙钟 {time.time()-t0:.0f}s", flush=True)

    # ---- 写 CSV ----
    os.makedirs("results", exist_ok=True)
    out_csv = "results/stage_a_grid.csv"
    cols = ["stop_frac", "be_r", "risk_per_trade", "annual", "mdd", "calmar",
            "sharpe", "sortino", "winrate", "pf", "n_trades", "n_entries",
            "n_stopped", "n_be_exits", "n_eod", "n_be_moves", "n_capped",
            "n_cant_afford", "n_no_trade", "final_equity", "secs"]
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for m in results:
            row = dict(m)
            row["pf"] = 99.0 if m["pf"] == float("inf") else m["pf"]
            w.writerow(row)
    print(f"已写 {out_csv}")

    # ---- 边际表: 每个止损档 (对保本取中位) / 每个保本档 (对止损取中位) ----
    import statistics as st

    def med(vals):
        return st.median(vals)

    print("\n===== 止损边际 (每个止损档: 对全部保本档取中位) =====")
    print(f"{'止损':>8} | {'年化中位':>8} {'MDD中位':>8} {'Calmar中位':>10} "
          f"{'Sharpe中位':>10} {'Sortino中位':>11} {'胜率中位':>8}")
    for s in STOPS:
        rows = [m for m in results if m["stop_frac"] == s]
        print(f"{s:>8.2%} | {med([m['annual'] for m in rows])*100:>7.1f}% "
              f"{med([m['mdd'] for m in rows])*100:>7.1f}% "
              f"{med([m['calmar'] for m in rows]):>10.2f} "
              f"{med([m['sharpe'] for m in rows]):>10.2f} "
              f"{med([m['sortino'] for m in rows]):>11.2f} "
              f"{med([m['winrate'] for m in rows])*100:>7.1f}%")

    print("\n===== 保本边际 (每个保本档: 对全部止损档取中位) =====")
    print(f"{'保本':>8} | {'年化中位':>8} {'MDD中位':>8} {'Calmar中位':>10} "
          f"{'Sharpe中位':>10} {'Sortino中位':>11} {'胜率中位':>8}")
    for b in BES:
        rows = [m for m in results if m["be_r"] == b]
        label = "无" if b is None else f"{b:g}R"
        print(f"{label:>8} | {med([m['annual'] for m in rows])*100:>7.1f}% "
              f"{med([m['mdd'] for m in rows])*100:>7.1f}% "
              f"{med([m['calmar'] for m in rows]):>10.2f} "
              f"{med([m['sharpe'] for m in rows]):>10.2f} "
              f"{med([m['sortino'] for m in rows]):>11.2f} "
              f"{med([m['winrate'] for m in rows])*100:>7.1f}%")

    # ---- 排行榜 ----
    for key, name in [("calmar", "Calmar"), ("sharpe", "Sharpe")]:
        top = sorted(results, key=lambda m: m[key], reverse=True)[:10]
        print(f"\n===== Top 10 by {name} =====")
        print(f"{'止损':>8} {'保本':>6} | {'年化':>8} {'MDD':>8} {'Calmar':>8} "
              f"{'Sharpe':>7} {'Sortino':>8} {'胜率':>7} {'笔数':>6} {'保本触发':>8} {'被压/买不起':>10}")
        for m in top:
            be_label = "无" if m["be_r"] is None else f"{m['be_r']:g}R"
            print(f"{m['stop_frac']:>8.2%} {be_label:>6} | {m['annual']*100:>7.1f}% "
                  f"{m['mdd']*100:>7.1f}% {m['calmar']:>8.2f} {m['sharpe']:>7.2f} "
                  f"{m['sortino']:>8.2f} {m['winrate']*100:>6.1f}% {m['n_trades']:>6} "
                  f"{m['n_be_moves']:>8} {m['n_capped']}/{m['n_cant_afford']:>4}")

    # ---- 数据健康检查 ----
    bad = [m for m in results if m["n_cant_afford"] > 0 or m["n_capped"] > 0]
    if bad:
        print("\n⚠ 以下组合存在买不起 1 手 / 被 MAX_QTY 压制 (结果可能失真):")
        for m in bad:
            be_label = "无" if m["be_r"] is None else f"{m['be_r']:g}R"
            print(f"  stop={m['stop_frac']:.2%} be={be_label}: "
                  f"买不起={m['n_cant_afford']} 被压={m['n_capped']}")
    else:
        print("\n✓ 全部 49 组合无「买不起 1 手」、无「被上限压制」(硬规则 2 通过)")


if __name__ == "__main__":
    main()
