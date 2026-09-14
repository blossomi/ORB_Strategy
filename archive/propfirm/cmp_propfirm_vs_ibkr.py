# -*- coding: utf-8 -*-
"""
cmp_propfirm_vs_ibkr.py  (propfirm/)
====================================
同一个 ORB v8.4 策略、同一段历史 (2019-01-02 → 2026-08-28)、同一个成本口径 (1 tick 滑点 +
佣金每边), 对比两条变现通道:

  A. IBKR 自有资金实盘 (MNQ, $25k 起, 0.7% 以损定仓复利)
     - 佣金口径: IBKR MNQ = $0.25 执行费 + ~$0.22 CME 交易所费 ≈ $0.49/边
       (官方费率页 + CME 费用表; 回测主线假设 $0.50/边 → 基本等价, 本脚本按 $0.49 重算)
     - 输出: 逐年盈亏 / 月净分布 / 账户 MDD / 最小手数约束 / 最差 12 个月
  B. LucidFlex $50K prop 考核 (官方规则, 见 prop_lucid_rolling.py)
     - 输出: 单号月净 (滚动口径) / 5 号上限 / 门票成本

用法: ../.venv/bin/python cmp_propfirm_vs_ibkr.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

TRADES = HERE.parent / "ORB_strategy" / "html_output" / "v8_4_trades.csv"
CAPITAL = 25_000.0
BACKTEST_COMMISSION_SIDE = 0.50      # 回测主线假设 ($/手/边)
IBKR_COMMISSION_SIDE = 0.49          # IBKR MNQ: 0.25 执行费 + 0.22 CME 费 (≈)
TICK = 0.25
MULT = 2.0


def main() -> None:
    t = pd.read_csv(TRADES)
    t["date"] = pd.to_datetime(t["entry_time_et"])
    t["y"] = t["date"].dt.year

    # ---- 成本对齐: 把回测的 $0.50/边 换成 IBKR 的 $0.49/边 ----
    delta_pc = 2 * (BACKTEST_COMMISSION_SIDE - IBKR_COMMISSION_SIDE)   # $/手 往返差额
    t["pnl_ibkr"] = t["pnl_usd"] + t["qty"] * delta_pc                 # delta_pc 为正 → 省下的钱

    print("=" * 104)
    print("A. IBKR 自有资金实盘 (MNQ, $25k 起, 0.7% 以损定仓复利, 1 tick 滑点)")
    print("=" * 104)
    print(f"成本: 回测假设 佣金 ${BACKTEST_COMMISSION_SIDE:.2f}/边 + 1 tick 滑点; "
          f"IBKR 实际 MNQ ≈ ${IBKR_COMMISSION_SIDE:.2f}/边(执行 $0.25+CME $0.22) + 滑点 → "
          f"逐笔差额 ${delta_pc:+.2f}/手往返 (总差额 ${t['qty'].sum() * delta_pc:+,.0f})")
    print("→ 结论: 回测成本口径 ≈ IBKR 真实成本, 不需要重跑引擎\n")

    for tag, col in (("回测口径 ($0.50/边)", "pnl_usd"), ("IBKR 口径 ($0.49/边)", "pnl_ibkr")):
        eq = CAPITAL + t[col].cumsum()
        peak = eq.cummax()
        dd = (eq - peak) / peak
        yrs = (t["date"].iloc[-1] - t["date"].iloc[0]).days / 365.25
        mo = t.set_index("date")[col].resample("ME").sum()
        print(f"  [{tag}] 期末 ${eq.iloc[-1]:>12,.0f} | 总收益 {eq.iloc[-1] / CAPITAL - 1:>8.1%} | "
              f"年化 {(eq.iloc[-1] / CAPITAL) ** (1 / yrs) - 1:>6.1%} | MDD {dd.min():>6.1%} "
              f"(-${(peak - eq).max():,.0f})")
        print(f"           月净: 中位 ${mo.median():>7,.0f} | 均值 ${mo.mean():>7,.0f} | "
              f"最差月 ${mo.min():>7,.0f} | 负月占比 {100*(mo < 0).mean():.0f}% | "
              f"月数 {len(mo)}")

    print("\n逐年明细 (IBKR 口径):")
    t["eq_ibkr"] = CAPITAL + t["pnl_ibkr"].cumsum()
    rows = []
    for y, g in t.groupby("y"):
        start_eq = CAPITAL + t[t["y"] < y]["pnl_ibkr"].sum()
        pnl = g["pnl_ibkr"].sum()
        peak = g["eq_ibkr"].cummax()
        mdd = ((g["eq_ibkr"] - peak) / peak).min()
        pf_num = g.loc[g["pnl_ibkr"] > 0, "pnl_ibkr"].sum()
        pf_den = -g.loc[g["pnl_ibkr"] < 0, "pnl_ibkr"].sum()
        rows.append(dict(年=y, 笔数=len(g), 年初权益=start_eq, 当年盈亏=pnl,
                         年内收益=pnl / start_eq, 年内MDD=mdd,
                         PF=pf_num / pf_den if pf_den else np.nan,
                         月均=pnl / len(pd.date_range(g["date"].min(), g["date"].max(),
                                                      freq="ME")),
                         手数中位=g["qty"].median()))
    ydf = pd.DataFrame(rows)
    print(f"{'年':>5} | {'笔数':>5} | {'年初权益':>10} | {'当年盈亏':>10} | {'年内收益':>8} | "
          f"{'年内MDD':>8} | {'PF':>5} | {'月均$':>7} | {'手数中位':>8}")
    print("-" * 92)
    for _, r in ydf.iterrows():
        print(f"{int(r['年']):>5} | {int(r['笔数']):>5} | {r['年初权益']:>10,.0f} | "
              f"{r['当年盈亏']:>10,.0f} | {r['年内收益']:>8.1%} | {r['年内MDD']:>8.1%} | "
              f"{r['PF']:>5.2f} | {r['月均']:>7,.0f} | {r['手数中位']:>8.0f}")
    ydf.to_csv(HERE / "results" / "cmp_ibkr_yearly.csv", index=False)

    # 最差 12 个月滚动 (美元)
    eq = CAPITAL + t["pnl_ibkr"].cumsum()
    s = pd.Series(eq.to_numpy(), index=t["date"])
    m = s.resample("ME").last()
    w12 = (m / m.shift(12) - 1).dropna()
    print(f"\n最差滚动 12 个月: {w12.min():.1%} | 最好 {w12.max():.1%} | "
          f"12 个月为负的窗口占比 {100*(w12 < 0).mean():.0f}%")
    print(f"最小手数约束: 买不起 1 手的天数 {(t['qty'] < 1).sum()} 天; "
          f"手数 min {t['qty'].min():.0f} / 中位 {t['qty'].median():.0f} / max {t['qty'].max():.0f}")
    print(f"近 12 个月实际手数: 中位 {t[t['date'] >= t['date'].max() - pd.Timedelta(days=365)]['qty'].median():.0f} 手")

    # 固定手数口径 (不复利): 用当前 regime 的手数
    for q in (2, 5, 10):
        pnl_fixed = q * (t["pnl_usd"] / t["qty"]) + q * delta_pc
        eqf = CAPITAL + pnl_fixed.cumsum()
        mof = pnl_fixed.groupby(t["y"]).sum()
        print(f"  固定 {q:>2} 手 (不复利): 期末 ${eqf.iloc[-1]:>10,.0f} | "
              f"年化 {(eqf.iloc[-1]/CAPITAL)**(1/yrs)-1:>6.1%} | "
              f"MDD {(((eqf-eqf.cummax())/eqf.cummax()).min()):>6.1%} | "
              f"月净中位 ${(pnl_fixed.groupby(t['date'].dt.to_period('M')).sum()).median():>7,.0f}")

    print("\n" + "=" * 104)
    print("B. 同手数对照 (1R = $143 = prop r14 档的以损定仓手数, 唯一差别 = 有没有规则墙)")
    print("=" * 104)
    r14_q = np.maximum(1, np.floor(143.0 / (t["stop_dist_pt"] * MULT))).astype(int)
    pnl_143 = r14_q * (t["pnl_usd"] / t["qty"])
    eq143 = CAPITAL + pnl_143.cumsum()
    m143 = pnl_143.groupby(t["date"].dt.to_period("M")).sum()
    dd143 = (eq143 - eq143.cummax()).min()
    print(f"  B1. IBKR $25k / MNQ, 固定 1R=$143 (无规则墙, 未复利):")
    print(f"      总盈亏 ${pnl_143.sum():>10,.0f} ({(pnl_143.sum()/143):.0f}R) | "
          f"月净 中位 ${m143.median():>7,.0f} / 均值 ${m143.mean():>7,.0f} | "
          f"负月 {100*(m143 < 0).mean():.0f}%")
    print(f"      账户最大回撤 ${-dd143:>9,.0f} (占 $25k 的 {dd143/CAPITAL:.1%}) | "
          f"最差单月 ${m143.min():,.0f}")
    print(f"      ⚠️ 固定手数下策略累计 R 回撤 = 45.4R = ${45.4*143:,.0f} → $25k 扛得住; "
          f"prop 的 $2,000 带宽只等于 14R, 只有它的 1/3")
    print(f"  B2. 同一手数放进 LucidFlex $50K 官方规则 (prop_lucid_rolling.py): "
          f"净 $25,378 → 月净 $276")
    print(f"  → **规则墙本身吃掉了 {100*(1-276/(pnl_143.sum()/91.8)):.0f}% 的 edge** "
          f"(同一批交易、同一手数、同一成本, 差别只在考核爆号 + 带宽 14R + 提款上限)")

    print("\n" + "=" * 104)
    print("C. LucidFlex $50K prop 考核 (官方规则, 数字来自 prop_lucid_rolling.py)")
    print("=" * 104)
    print("  单号净/月中位 (滚动起点): 考14/资7 $251 | 考7/资14 $195 | 考14/资14 $185")
    print("  连续路径全样本 (2019→2026): 考14/资7 净 $25,378 (月净 $276) | 考7/资14 $25,594 ($279)")
    print("  账号上限 5 个 (funded ≤5) → 5 号满配 ≈ $1,255/月 (同过同爆, 现金流 ×5)")
    print("  本金风险: $0 (爆号只损失门票 $140/新号、$95/重置); 每号最多 5 次提款后需重考")


if __name__ == "__main__":
    main()
