# -*- coding: utf-8 -*-
"""
prop_be_variants.py  (propfirm/)
================================
回答一个具体问题: 「prop 考核的目标函数下, 拉保本应该拉多近?」

背景: 复利口径下 1R 保本伤长尾 (回测主线结论), 但考核不是复利赛 —— 是在窄回撤带里
的竞速。1R/2R 保本把左尾抬起来 (连亏变浅), 代价是砍掉部分长尾 (目标变慢)。
哪个划算由通过率说话: 同一引擎跑 BE=5R(主线) / 2R / 1R, 导出逐笔 R,
交给 prop_sim.py 在考核规则下对比通过率。

口径: MNQ ($2/点) × 1 tick 滑点 × $25k × 0.7% 复利 (与主线一致; R 逐笔口径与
手数无关, 复利只影响手数不影响 R)。窗口 2016 起 —— 故意包含 2017 唯一亏损年,
考核风控评估不应该用跳过坏年的样本。

用法: python prop_be_variants.py          # 3 次引擎 + 模拟对比, 一次跑完
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "GLM_working"))
import orb_core_v84 as core  # noqa: E402  (搜索核心留在 GLM_working, 只读依赖)

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
MAINLINE_CSV = HERE.parent / "ORB_strategy" / "html_output" / "v8_4_trades.csv"
STOP_FRAC = 0.075
MULT = 2.0                     # MNQ $2/点
FEE_ROUND_TRIP_PC = 2 * (core.COMMISSION_PER_CONTRACT + 1 * core.TICK * MULT)
# = 双边 $2/手 (佣金 $0.5×2 + 1 tick 滑点 $0.5×2); 引擎把滑点折进费率, 成交报告的
#   realized_pnl 不含费, 所以每手扣回双边费再算净 R

ET = core.ET


def run_and_extract(be_r: float, data: dict, stop_frac: float = STOP_FRAC) -> pd.DataFrame:
    """跑一次引擎, 从 positions report 反推逐笔 (日期, 每手净盈亏, stop_pt)。

    stop_frac: 止损 = stop_frac × 前一日 14日ATR (默认 7.5% = 主线)。
    """
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.backtest.models import PerContractFeeModel
    from nautilus_trader.config import LoggingConfig
    from nautilus_trader.model.currencies import USD
    from nautilus_trader.model.enums import AccountType, OmsType
    from nautilus_trader.model.identifiers import TraderId, Venue
    from nautilus_trader.model.objects import Money

    venue = Venue(core.VENUE)
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-PROP"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=venue, oms_type=OmsType.NETTING,
                     account_type=AccountType.MARGIN, base_currency=USD,
                     starting_balances=[Money(core.DEFAULT_CAPITAL, USD)],
                     fee_model=PerContractFeeModel(Money(core.FEE_PER_SIDE, USD)))
    engine.add_instrument(data["instrument"])
    engine.add_data(data["bars"])
    cfg = core.OrbStrategyConfig(
        instrument_id=core.INSTRUMENT_ID, bar_type=str(data["bar_type"]),
        risk_per_trade=0.007, multiplier=core.MULTIPLIER,
        atr_stop_fraction=stop_frac, max_qty=core.MAX_QTY,
        be_r_multiple=be_r, be_buffer_ticks=core.BE_BUFFER_TICKS)
    engine.add_strategy(core.OrbStrategy(cfg, data["atr_map"], data["range_map"],
                                         data["day_last_bar"]))
    engine.run()

    rep = engine.trader.generate_positions_report()
    rows = []
    for _, p in rep.iterrows():
        if p["ts_closed"] is None or pd.isna(p["ts_closed"]):
            continue
        qty = float(p["peak_qty"])          # 平仓后 quantity=0 (FLAT), 用峰值手数
        if qty <= 0:
            continue
        # realized_pnl 不含佣金 (滑点已折进费率), commissions 字符串 "[36.00 USD]"
        pnl_pc = (core.money_float(p["realized_pnl"]) - core.money_float(p["commissions"])) / qty
        ts = pd.Timestamp(p["ts_opened"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        d = ts.tz_convert(ET).date()
        atr = data["atr_map"].get(d)
        if atr is None:
            continue
        stop_pt = stop_frac * atr
        rows.append(dict(date=d, pnl_pc=pnl_pc, stop_pt=stop_pt,
                         r=pnl_pc / (stop_pt * core.MULTIPLIER), exit_reason=""))
    engine.reset()
    return pd.DataFrame(rows)


def main() -> None:
    core.configure(multiplier=MULT, slippage_ticks=1.0, capital=25_000)
    print("[1/3] 构建数据 (2019-01-01 ~ 2026-08-30, 用户指定近几年口径) ...")
    data = core.build_data("2019-01-01", "2026-08-30")
    core.ensure_bars(data)

    print("[2/3] 跑 BE=5R / 2R / 1R 三次引擎 ...")
    RESULTS.mkdir(exist_ok=True)
    for be in (5.0, 2.0, 1.0):
        df = run_and_extract(be, data)
        out = RESULTS / f"prop_trades_be{int(be)}_2019.csv"
        df.to_csv(out, index=False)
        print(f"  BE={be}R: {len(df)} 笔 -> {out.name}")

    print("[3/3] 考核模拟对比 (真实 firm 口径 + Apex 形对照) ...")
    import prop_sim as ps

    variants = {}
    for be in (5.0, 2.0, 1.0):
        v = ps.load_trades_engine(str(RESULTS / f"prop_trades_be{int(be)}_2019.csv"))
        variants[be] = v
        ps.summarize_series(v, f"引擎 2019 起 BE={be}R (MNQ 1tick, {len(v)} 笔)")

    scs = {s["name"]: s for s in (ps.REAL_FIRM, ps.APEX_LIKE)}
    for be, v in variants.items():
        res = ps.prop_run(v, MULT, qty_ladder=(2, 3, 5), scenarios=scs)
        ps.print_table(res, "eod")


if __name__ == "__main__":
    main()
