# -*- coding: utf-8 -*-
"""
heatmap_v8_6.py
===============
v8.6 单窗口 2D 热力图 (方案 A: 全样本选参)。

在 2016-01-01 ~ 2026-08-30 全样本上, 跑 仓位风险(0.3%-0.8%) x 保本倍数(1R-5R)
共 6x5=30 个组合 (止损距离固定 7.5% x 14日ATR), 输出三张矩阵
(年化% / MDD% / Sharpe) + 最优标注 + 邻域(高原/孤峰)判断。

并行: ProcessPoolExecutor(max_workers=3), 每个 worker 预加载一次数据后复用
(engine.add_data 内部拷贝 bars, 不消耗原列表)。

用法: cd ORB_strategy && ../.venv/bin/python heatmap_v8_6.py
"""
from concurrent.futures import ProcessPoolExecutor
import csv
import os

import orb_backtes_v8_6 as V

START = "2016-01-01"
END = "2026-08-30"
WORKERS = 3
OUT_CSV = "html_output/v8_6_heatmap.csv"

# 子进程全局缓存 (spawn 下每个 worker 独立, 由 initializer 填充)
_WORKER = {}


def _init_worker(start, end):
    _WORKER["data"] = V.build_data(start, end)


def _run_combo(args):
    risk, be = args
    m = V.run_backtest(risk, be, _WORKER["data"])
    return risk, be, m


def _fmt_table(matrix, rows, cols, fmt, title):
    print(f"\n===== {title} =====")
    header = "Risk\\BE " + " ".join(f"{c:>10}" for c in cols)
    print(header)
    for a in rows:
        line = f"{a*100:>5.1f}% "
        for b in cols:
            v = matrix.get((a, b))
            line += f"{fmt(v):>10}" if v is not None else f"{'--':>10}"
        print(line)


def main():
    rows = V.RISK_FRACS
    cols = V.BE_RS
    combos = [(a, b) for a in rows for b in cols]
    print(f"单窗口热力图  {START} ~ {END}  |  止损固定 {V.ATR_STOP_FRACTION*100:.1f}%ATR  |  "
          f"组合 {len(combos)} 个  |  并行 {WORKERS} 进程")

    with ProcessPoolExecutor(max_workers=WORKERS, initializer=_init_worker,
                             initargs=(START, END)) as ex:
        results = list(ex.map(_run_combo, combos))

    # 汇总
    res = {(a, b): m for a, b, m in results}

    # 三张矩阵
    annual = {(a, b): res[(a, b)]["annual"] * 100 for a, b in res}
    mdd = {(a, b): res[(a, b)]["mdd"] * 100 for a, b in res}
    sharpe = {(a, b): res[(a, b)]["sharpe"] for a, b in res}
    _fmt_table(annual, rows, cols, lambda v: f"{v:9.1f}%", "年化收益率 (%)")
    _fmt_table(mdd, rows, cols, lambda v: f"{v:9.1f}%", "最大回撤 MDD (%)")
    _fmt_table(sharpe, rows, cols, lambda v: f"{v:9.2f}", "Sharpe")

    # ---- 选参: 排除爆仓(MDD<-70%)后按 Sharpe 最高 ----
    valid = {(a, b): m for (a, b), m in res.items() if m["mdd"] > -0.70}
    if not valid:
        print("\n⚠️ 所有组合 MDD 都 < -70% (爆仓), 无可用参数。")
        return
    best_key = max(valid, key=lambda k: valid[k]["sharpe"])
    best = res[best_key]
    print("\n===== 全局最优 (按 Sharpe, 排除 MDD<-70% 爆仓) =====")
    print(f"风险={best_key[0]*100:.1f}%  BE={best_key[1]:.0f}R  ->  "
          f"年化 {best['annual']*100:.1f}%  MDD {best['mdd']*100:.1f}%  "
          f"Sharpe {best['sharpe']:.2f}  交易 {best['n_entries']:,} 笔")

    # ---- 邻域判断: 高原 vs 孤峰 ----
    best_s = best["sharpe"]
    plateau = [(a, b) for (a, b), m in valid.items() if m["sharpe"] >= 0.9 * best_s]
    if plateau:
        risk_rng = (min(a for a, _ in plateau), max(a for a, _ in plateau))
        be_rng = (min(b for _, b in plateau), max(b for _, b in plateau))
        print("\n===== 稳健性 (高原 vs 孤峰) =====")
        print(f"Sharpe >= 最优 90% 的组合: {len(plateau)} 个")
        print(f"覆盖 风险 {risk_rng[0]*100:.1f}%~{risk_rng[1]*100:.1f}%  x  "
              f"BE {be_rng[0]:.0f}R~{be_rng[1]:.0f}R")
        if len(plateau) >= 5 and (risk_rng[1] > risk_rng[0]) and (be_rng[1] > be_rng[0]):
            print("→ 高原: 参数不敏感, 区间内任意一档都可用, edge 较可信。")
        else:
            print("→ 孤峰/窄峰: 最优是局部尖峰, 疑似过拟合, 勿直接采信单点。")

    # ---- 导出 CSV ----
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["risk_per_trade", "be_r", "annual", "mdd", "sharpe", "final_equity", "n_entries"])
        for (a, b), m in sorted(res.items()):
            w.writerow([a, b, round(m["annual"], 6), round(m["mdd"], 6),
                        round(m["sharpe"], 4), round(m["final_equity"], 2), m["n_entries"]])
    print(f"\n已导出原始结果: {OUT_CSV}")


if __name__ == "__main__":
    main()
