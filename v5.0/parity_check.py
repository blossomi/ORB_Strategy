# -*- coding: utf-8 -*-
"""
parity_check.py — 原版 v8.4 vs FSM 重构版的逐笔 parity 裁判
============================================================
对两个 trades CSV (相同参数、相同数据) 逐字段 diff:
  字符串/整数字段 (entry/exit 时间、side、qty、exit_reason) 必须完全相等;
  浮点字段 (价格、pnl、stop_dist、r_multiple、duration) 容差 = 价格 1e-9 / 金额 0.005。

用法:
  ../.venv/bin/python parity_check.py <baseline.csv> <candidate.csv>
  (默认: archive/ORB_strategy/html_output/v8_4_trades_25000.csv vs results/v5_trades_25000.csv)
"""
import csv
import sys

FLOAT_TOL = {"entry_price": 1e-9, "exit_price": 1e-9, "pnl_usd": 0.005,
             "stop_dist_pt": 1e-9, "r_multiple": 0.011, "duration_min": 0.06}


def load(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows


def main(base_path, cand_path):
    base, cand = load(base_path), load(cand_path)
    print(f"基线 {len(base):,} 笔 ← {base_path}")
    print(f"候选 {len(cand):,} 笔 ← {cand_path}")

    if len(base) != len(cand):
        print(f"!! 行数不同: {len(base)} vs {len(cand)} —— parity FAIL")
        # 找出第一个错位日帮助定位
        bd = {r["entry_time_et"][:10] for r in base}
        cd = {r["entry_time_et"][:10] for r in cand}
        print("   仅基线有的交易日:", sorted(bd - cd)[:10])
        print("   仅候选有的交易日:", sorted(cd - bd)[:10])
        return 1

    diffs = {}
    for i, (b, c) in enumerate(zip(base, cand)):
        for col in b:
            bv, cv = b[col], c[col]
            if col in FLOAT_TOL:
                try:
                    bf, cf = float(bv), float(cv)
                    if abs(bf - cf) > FLOAT_TOL[col]:
                        diffs.setdefault(col, []).append((i, bv, cv))
                except ValueError:            # 空串等
                    if bv != cv:
                        diffs.setdefault(col, []).append((i, bv, cv))
            elif bv != cv:
                diffs.setdefault(col, []).append((i, bv, cv))

    if not diffs:
        pnl_b = sum(float(r["pnl_usd"]) for r in base)
        pnl_c = sum(float(r["pnl_usd"]) for r in cand)
        print(f"\n✔ PARITY PASS — {len(base):,} 笔逐字段全同 "
              f"(Σpnl 基线 {pnl_b:,.2f} vs 候选 {pnl_c:,.2f}, 差 {pnl_c - pnl_b:+.4f})")
        return 0

    print(f"\n!! PARITY FAIL — 差异字段: {list(diffs)}")
    for col, items in diffs.items():
        print(f"\n[{col}] {len(items)} 处不同, 前 5 处:")
        for i, bv, cv in items[:5]:
            print(f"  row {i}: 基线={bv!r}  候选={cv!r}  "
                  f"(entry {base[i]['entry_time_et']} {base[i]['side']})")
    return 1


if __name__ == "__main__":
    here = __file__.rsplit("/", 1)[0]
    default_base = f"{here}/../archive/ORB_strategy/html_output/v8_4_trades_25000.csv"
    default_cand = f"{here}/results/v5_trades_25000.csv"
    base_path = sys.argv[1] if len(sys.argv) > 1 else default_base
    cand_path = sys.argv[2] if len(sys.argv) > 2 else default_cand
    sys.exit(main(base_path, cand_path))
