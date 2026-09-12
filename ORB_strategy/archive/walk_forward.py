# -*- coding: utf-8 -*-
"""
walk_forward.py
===============
v8.6 滚动 Walk-Forward 验证 (方案 B)。

固定「训练 5 年 → 测试 1 年」滚动 6 个窗口。每个窗口独立:
  1) 训练段跑 仓位风险(0.3%-0.8%) x BE(1R-5R) 共 30 组合 (止损固定 7.5%ATR), 选出最优参数 θ*;
  2) 用 θ* 在测试段 (样本外) 跑一次;
最后汇总生成结论: θ* 漂移表 + 样本外表 + 稳健区间(优秀区域交集) + 过拟合判断。

并行: ProcessPoolExecutor(max_workers=3) 窗口级并行 (每窗口一个进程,
进程内 36 次回测复用一次读入的数据)。

用法: cd ORB_strategy && ../.venv/bin/python walk_forward.py
"""
from concurrent.futures import ProcessPoolExecutor

import orb_backtes_v8_6 as V

WORKERS = 3
MIN_TRADES = 100          # 选参时要求训练段至少 N 笔 (排除"几乎不交易"的极端参数)
BLOWUP_MDD = -0.70        # 选参时排除 MDD < -70% (爆仓)
PLATEAU_FRAC = 0.90       # "优秀区域"阈值: Sharpe >= 最优的 90%

# 6 个滚动窗口 (训练 5 年 → 测试 1 年, 末段测试只到 2026-08-30)
WINDOWS = [
    dict(id=1, tr_s="2016-01-01", tr_e="2020-12-31", te_s="2021-01-01", te_e="2021-12-31"),
    dict(id=2, tr_s="2017-01-01", tr_e="2021-12-31", te_s="2022-01-01", te_e="2022-12-31"),
    dict(id=3, tr_s="2018-01-01", tr_e="2022-12-31", te_s="2023-01-01", te_e="2023-12-31"),
    dict(id=4, tr_s="2019-01-01", tr_e="2023-12-31", te_s="2024-01-01", te_e="2024-12-31"),
    dict(id=5, tr_s="2020-01-01", tr_e="2024-12-31", te_s="2025-01-01", te_e="2025-12-31"),
    dict(id=6, tr_s="2021-01-01", tr_e="2025-12-31", te_s="2026-01-01", te_e="2026-08-30"),
]


def _select_theta(train_res: dict):
    """从训练段 35 组合里选 θ*, 返回 (key, metrics, plateau 集合)。"""
    valid = {(a, b): m for (a, b), m in train_res.items()
             if m["mdd"] > BLOWUP_MDD and m["n_entries"] >= MIN_TRADES}
    if not valid:
        return None, None, []
    best_key = max(valid, key=lambda k: valid[k]["sharpe"])
    best_s = valid[best_key]["sharpe"]
    plateau = [(a, b) for (a, b), m in valid.items() if m["sharpe"] >= PLATEAU_FRAC * best_s]
    return best_key, valid[best_key], plateau


def run_window(job):
    """一个窗口的完整任务 (在 worker 进程内执行)。"""
    # 训练段: 构建一次数据, 复用跑 30 组合
    tr = V.build_data(job["tr_s"], job["tr_e"])
    train_res = {}
    for a in V.RISK_FRACS:
        for b in V.BE_RS:
            train_res[(a, b)] = V.run_backtest(a, b, tr)

    theta, train_best, plateau = _select_theta(train_res)

    # 测试段: 用 θ* 跑样本外
    if theta is None:
        return dict(id=job["id"], tr_s=job["tr_s"], tr_e=job["tr_e"],
                    te_s=job["te_s"], te_e=job["te_e"],
                    theta=None, train_best=None, plateau=[], test=None)

    te = V.build_data(job["te_s"], job["te_e"])
    test_m = V.run_backtest(theta[0], theta[1], te)

    return dict(id=job["id"], tr_s=job["tr_s"], tr_e=job["tr_e"],
                te_s=job["te_s"], te_e=job["te_e"],
                theta=theta, train_best=train_best, plateau=plateau, test=test_m)


def _fmt_pct(x):
    return f"{x*100:6.1f}%"


def main():
    print("=" * 100)
    print("ORB v8.6 滚动 Walk-Forward 验证  (训练 5 年 → 测试 1 年, 共 6 窗口)")
    print(f"搜索范围: 风险 {V.RISK_FRACS[0]*100:.1f}%-{V.RISK_FRACS[-1]*100:.1f}%  x  "
          f"BE {V.BE_RS[0]:.0f}R-{V.BE_RS[-1]:.0f}R  (止损固定 {V.ATR_STOP_FRACTION*100:.1f}%ATR)  |  "
          f"并行 {WORKERS} 进程")
    print("=" * 100)

    with ProcessPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(run_window, WINDOWS))
    results.sort(key=lambda r: r["id"])

    # ---------- [1] θ* 漂移 + 样本外表 ----------
    print("\n[1] 每窗口选出的最优参数 θ* 与样本外表现")
    print(f"{'窗口':>4} | {'训练段':<22} {'测试段':<22} | {'θ*风险':>7} {'θ* BE':>6} | "
          f"{'训练Sharpe':>9} | {'测试年化':>9} {'测试MDD':>8} {'测试Sharpe':>9}")
    print("-" * 100)
    for r in results:
        if r["theta"] is None:
            print(f"{r['id']:>4} | {r['tr_s']}~{r['tr_e']}  (无可用参数)")
            continue
        a, b = r["theta"]
        tb, tm = r["train_best"], r["test"]
        print(f"{r['id']:>4} | {r['tr_s']}~{r['tr_e']}  {r['te_s']}~{r['te_e']} | "
              f"{a*100:>6.1f}% {b:>5.0f}R | {tb['sharpe']:>9.2f} | "
              f"{tm['annual']*100:>8.1f}% {tm['mdd']*100:>7.1f}% {tm['sharpe']:>9.2f}")

    # ---------- [2] θ* 漂移 ----------
    thetas = [r["theta"] for r in results if r["theta"] is not None]
    if thetas:
        risk_vals = [a for a, _ in thetas]
        be_vals = [b for _, b in thetas]
        print("\n[2] 参数稳定性 (θ* 漂移)")
        print(f"   风险: {min(risk_vals)*100:.1f}% ~ {max(risk_vals)*100:.1f}%   "
              f"(跨 {max(risk_vals)*100-min(risk_vals)*100:.1f} 个百分点)")
        print(f"   BE : {min(be_vals):.0f}R ~ {max(be_vals):.0f}R")
        if max(risk_vals) - min(risk_vals) <= V.RISK_FRACS[1] - V.RISK_FRACS[0] + 1e-9 \
           and max(be_vals) - min(be_vals) <= 2:
            print("   → θ* 集中: 参数稳定, 不是每年乱跳。")
        else:
            print("   → θ* 四散: 每年最优不一样, 过拟合风险高。")

    # ---------- [3] 稳健区间 (优秀区域交集) ----------
    print("\n[3] 稳健区间 (各窗口「Sharpe >= 最优 90% 且未爆仓」区域的交集)")
    plateaus = [set(r["plateau"]) for r in results if r["plateau"]]
    if plateaus:
        inter = set.intersection(*plateaus)
        if inter:
            risk_rng = (min(a for a, _ in inter), max(a for a, _ in inter))
            be_rng = (min(b for _, b in inter), max(b for _, b in inter))
            print(f"   交集非空: {len(inter)} 个格子")
            print(f"   稳健区间: 风险 {risk_rng[0]*100:.1f}%~{risk_rng[1]*100:.1f}%  x  "
                  f"BE {be_rng[0]:.0f}R~{be_rng[1]:.0f}R")
            print("   区间内任意一档都通过 6 段历史检验, 建议采用区间而非单点。")
        else:
            print("   交集为空 → 没有一组参数在全部 6 个窗口都进入前 10%。")
            print("   结论: 参数过拟合, 历史最优不可外推。")
    else:
        print("   (无可用参数)")

    # ---------- [4] 样本外汇总 + 最终结论 ----------
    tests = [r["test"] for r in results if r["test"] is not None]
    if tests:
        ann = [t["annual"] for t in tests]
        mdd = [t["mdd"] for t in tests]
        shp = [t["sharpe"] for t in tests]
        n_pos = sum(1 for a in ann if a > 0)
        print("\n[4] 样本外汇总 (6 段测试)")
        print(f"   年化: 中位 {sorted(ann)[len(ann)//2]*100:.1f}%  |  "
              f"最差 {min(ann)*100:.1f}%  |  {n_pos}/{len(ann)} 段为正")
        print(f"   MDD : 最差 {min(mdd)*100:.1f}%  |  Sharpe 中位 {sorted(shp)[len(shp)//2]:.2f}")
        print("\n===== 最终结论 =====")
        inter_ok = plateaus and set.intersection(*plateaus)
        if inter_ok and n_pos == len(ann):
            print("✓ 存在稳健参数区间, 且每段样本外都盈利 → edge 可外推, 可用区间内参数实盘。")
        elif inter_ok:
            print("△ 存在稳健区间, 但有窗口样本外亏损 → edge 存在但受市场状态影响, 需结合风控。")
        else:
            print("✗ 无稳健区间 / 样本外不稳 → 之前的最优参数是过拟合, 不要直接实盘。")
    print()


if __name__ == "__main__":
    main()
