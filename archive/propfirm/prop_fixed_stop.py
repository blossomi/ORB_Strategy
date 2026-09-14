# -*- coding: utf-8 -*-
"""
prop_fixed_stop.py  (propfirm/)
===============================
问题: 「考核实盘想用固定止损(固定点数) + 固定手数, 行不行?」

动机: ATR 自适应止损的每笔美元风险随波动漂 (2019 ~11pt = $22/手 vs 2026 ~32pt
= $64/手), 考核账号的回撤带是固定美元 —— 固定止损 + 固定手数让「连亏 N 笔 = 美元
损耗恒定」可以直接手算生存空间。代价: 固定点数在低波动年偏宽 (滑点占比反而低, 好)
在高波动年偏窄 (可能被单根噪声扫损, 坏)。

窗口: **2019-01-01 起 (用户指定, 不用更早年份)** —— 与回测主线默认口径一致;
另附 2022 起切片 (近四年) 对照, 检验档位推荐是否随窗口漂移。

实现: orb_core 的止损 = atr_stop_fraction × atr_map[日] —— 把 atr_map 整体替换成
常数 ATR (fixed_pt / 0.075), 止损就锁死为 fixed_pt, 其余逻辑 (BE=5R / 以损定仓 /
部分成交) 零改动。BE 触发按 R 距离, 随 stop 同步缩放 —— 与主线同一套行为。
「固定手数」在模拟器里本来就是口径 (q 固定, 不复利); 引擎里复利手数只影响 R 的
归一化分母, R 与手数无关。

用法: python prop_fixed_stop.py    # 5 档引擎 (~2 分钟) + 真实 firm 口径对比
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "GLM_working"))
sys.path.insert(0, str(HERE))

import orb_core_v84 as core  # noqa: E402
import prop_sim as ps  # noqa: E402
from prop_be_variants import MULT, STOP_FRAC, run_and_extract  # noqa: E402

RESULTS = HERE / "results"
START = "2019-01-01"                    # 用户指定: 只用近几年数据
# 9 档 = 5 主档 + 4 中间档 (边界敏感性: 相邻档差异大=尖峰过拟合, 平滑=稳健平台)
FIXED_STOPS = (10, 12.5, 15, 17.5, 20, 22.5, 25, 27.5, 30)
ATR_BASELINE_CSV = HERE.parent / "ORB_strategy" / "html_output" / "v8_4_trades.csv"


def main() -> None:
    core.configure(multiplier=MULT, slippage_ticks=1.0, capital=25_000)
    print(f"[1/2] 构建 {START} ~ 2026-08-30 数据 ...")
    data = core.build_data(START, "2026-08-30")
    core.ensure_bars(data)

    print("[2/2] 固定止损引擎 (BE=5R, 其余与主线一致) ...")
    RESULTS.mkdir(exist_ok=True)
    series = {"ATR 7.5%×14日(主线)": ps.load_trades_mainline(str(ATR_BASELINE_CSV))}
    for pt in FIXED_STOPS:
        d2 = dict(data)
        # 止损 = 0.075 × 常数ATR = pt —— 零代码改动锁死止损
        d2["atr_map"] = {k: pt / STOP_FRAC for k in data["atr_map"]}
        df = run_and_extract(5.0, d2)
        out = RESULTS / f"prop_trades_fixed{pt:g}pt_{START[:4]}.csv"
        df.to_csv(out, index=False)
        series[f"固定{pt}pt"] = df
        print(f"  固定{pt}pt: {len(df)} 笔 -> {out.name}")

    print("\n" + "=" * 78)
    print(f"序列画像 ({START} 起, MNQ $2/点, 1 tick 滑点, BE=5R)")
    print("=" * 78)
    for name, v in series.items():
        ps.summarize_series(v, name)
        avg_risk = (v["stop_pt"] * 2.0)
        print(f"  每手美元风险: 中位 ${avg_risk.median():.0f} "
              f"(P10 ${avg_risk.quantile(0.1):.0f} / P90 ${avg_risk.quantile(0.9):.0f})"
              f" | $2000回撤带生存连亏数(q=2): {2000 / (avg_risk.median() * 2):.0f} 笔")

    print("\n" + "=" * 78)
    print("年度分解 (每年: 每笔均R / 胜率% / 年内累计R回撤) —— 档位稳健性检查")
    print("=" * 78)
    hdr = "年份  " + "".join(f"{n:>16}" for n in series)
    print(hdr)
    all_df = {n: v.assign(year=pd.to_datetime(v["date"]).dt.year) for n, v in series.items()}
    years = sorted(set().union(*[set(v["year"]) for v in all_df.values()]))
    for y in years:
        cells = []
        for n, v in all_df.items():
            vy = v[v["year"] == y]
            if len(vy) == 0:
                cells.append(f"{'-':>16}")
                continue
            cum = vy.groupby("date")["r"].sum().cumsum()
            dd = float((cum - cum.cummax()).min())
            cells.append(f"{vy['r'].mean():>+.2f}/{(vy['r']>0).mean()*100:>3.0f}%{dd:>6.1f}R")
        print(f"{y}  " + "".join(cells))

    for label, cutoff in (("2019 起全窗口", START), ("2022 起切片(近四年)", "2022-01-01")):
        print("\n" + "=" * 78)
        print(f"真实 firm 口径对比 (DD2000/目标3000/日亏1200/cons50%, 120 交易日上限) —— {label}")
        print("=" * 78)
        rows = []
        for name, v in series.items():
            v2 = v
            if cutoff != START:
                v2 = v.assign(date=pd.to_datetime(v["date"]).dt.date)
                v2 = v2[v2["date"] >= pd.Timestamp(cutoff).date()]
            res = ps.prop_run(v2, MULT, qty_ladder=(1, 2, 3, 5),
                              scenarios={s["name"]: s for s in (ps.REAL_FIRM,)})
            res.insert(0, "stop", name)
            rows.append(res)
        allr = pd.concat(rows, ignore_index=True)
        for mode in ("eod", "intraday"):
            sub = allr[allr["mode"] == mode]
            print(f"\n--- mode={mode} ---")
            print("止损口径          q | 通过% | 触标% | 爆%  | 超时% | 通过天数中位")
            for _, r in sub.iterrows():
                print(f"{r['stop']:<14} {int(r['qty']):<2}| {r['pass_rate']:5.1f} | "
                      f"{r['target_hit_rate']:5.1f} | {r['blow_rate']:4.1f} | "
                      f"{r['timeout_rate']:5.1f} | {r['days_median']:6.0f}")
        tag = "2019" if cutoff == START else "2022"
        allr.to_csv(RESULTS / f"fixed_stop_vs_atr_realfirm_{tag}.csv", index=False)
        print(f"\n明细已存 results/fixed_stop_vs_atr_realfirm_{tag}.csv")


if __name__ == "__main__":
    main()
