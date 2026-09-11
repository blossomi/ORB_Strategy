# -*- coding: utf-8 -*-
"""
orb_backtest_recent.py
======================
近几个月回测（可复现）—— 复用 orb_backtes_v8_4.py 的 ORB 策略，
只改样本窗口 / 本金，不重复实现策略代码。

用途：快速验证「NQ 5 分钟 ORB 策略」在近几个月 / 任意窗口的表现。

用法: cd ORB_strategy && ../.venv/bin/python orb_backtest_recent.py

只需改下方 WINDOWS 列表（每条 = (起始日, 终止日, 起始本金)）即可复现
2026-09-03 那次回测结果。策略参数（止损/拉保本/手续费等）沿用
orb_backtes_v8_4.py 顶部的参数开关区，去那儿改。
"""
import importlib.util

import pandas as pd

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

# ===========================================================================
# ★ 可调参数：想跑哪段时间、用多少本金，改这里 ★
# ===========================================================================
WINDOWS = [
    ("2025-01-01", "2026-08-30", 100_000),   # 近 20 个月
    ("2026-01-01", "2026-08-30", 100_000),   # 2026 年初至今
    ("2026-05-01", "2026-08-30", 100_000),   # 近 4 个月
]


# ===========================================================================
# 加载 v8.4 策略模块（复用其策略类 / 参数 / 数据构建函数）
# ===========================================================================
def _load_v84():
    spec = importlib.util.spec_from_file_location("orb_v84", "orb_backtes_v8_4.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def run_backtest(m, start_date, end_date, capital):
    """跑一段窗口，返回 dict 结果。"""
    m.START_DATE = start_date
    m.END_DATE = end_date
    m.STARTING_CAPITAL = capital

    range_map = m.build_range_map()
    atr_map = m.build_atr_map()
    day_last_bar = m.build_day_last_bar_map()
    bars, instrument, bar_type = m.build_bars_and_instrument()

    venue = Venue(m.VENUE)
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId("ORB-BT-RECENT"),
            logging=LoggingConfig(log_level="WARNING"),
        )
    )
    engine.add_venue(
        venue=venue,
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(capital, USD)],
        fee_model=PerContractFeeModel(Money(m.COMMISSION_PER_CONTRACT, USD)),
    )
    engine.add_instrument(instrument)
    engine.add_data(bars)

    cfg = m.OrbStrategyConfig(
        instrument_id=m.INSTRUMENT_ID,
        bar_type=str(bar_type),
        risk_per_trade=m.RISK_PER_TRADE,
        multiplier=m.MULTIPLIER,
        atr_stop_fraction=m.ATR_STOP_FRACTION,
        max_qty=m.MAX_QTY,
        tp_r_multiple=m.TP_R_MULTIPLE,
        be_r_multiple=m.BE_R_MULTIPLE,
        be_buffer_ticks=m.BE_BUFFER_TICKS,
    )
    strategy = m.OrbStrategy(cfg, atr_map, range_map, day_last_bar)
    engine.add_strategy(strategy)
    engine.run()

    acct = engine.trader.generate_account_report(venue)
    eq = acct["total"].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample("1D").last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())
    final = float(eq.iloc[-1])
    years = (bars[-1].ts_event - bars[0].ts_event) / 1e9 / 86400 / 365.25
    annual = (final / capital) ** (1.0 / years) - 1.0

    return {
        "start": start_date, "end": end_date, "capital": capital,
        "bars": len(bars),
        "entries": strategy.n_entries,
        "tp": strategy.n_tp_hits,
        "stopped": strategy.n_stopped,
        "be_exits": strategy.n_be_exits,
        "eod": strategy.n_eod,
        "be_moves": strategy.n_be_moves,
        "no_trade_days": strategy.n_no_trade,
        "capped": strategy.n_capped,
        "final": final, "ret": final / capital - 1,
        "annual": annual, "mdd": mdd,
    }


def _min_capital_hint(m, start_date, end_date):
    """按窗口内「中位止损距离」估算买 1 手所需最低本金（供提示）。"""
    atr_map = m.build_atr_map()
    lo = pd.Timestamp(start_date).date()
    hi = pd.Timestamp(end_date).date()
    stops = [
        m.tick_round(m.ATR_STOP_FRACTION * v)
        for d, v in atr_map.items()
        if lo <= d <= hi
    ]
    if not stops:
        return None
    import statistics
    median_pt = statistics.median(stops)
    per_lot_risk = median_pt * m.MULTIPLIER
    return median_pt, per_lot_risk, per_lot_risk / m.RISK_PER_TRADE


def main():
    m = _load_v84()

    # 先打印一个最小本金提示
    for start, end, _ in WINDOWS:
        hint = _min_capital_hint(m, start, end)
        if hint:
            median_pt, per_lot_risk, need = hint
            print(f"[{start} ~ {end}] 中位止损 {median_pt:.1f}pt → 每手风险 "
                  f"${per_lot_risk:,.0f} → 1% 风险买 1 手需本金 ≥ ${need:,.0f}")
    print()

    for start, end, capital in WINDOWS:
        r = run_backtest(m, start, end, capital)
        print(f"=== {r['start']} ~ {r['end']} | 本金 ${r['capital']:,} | bars={r['bars']:,} ===")
        print(f"入场 {r['entries']:,} 笔 | 止盈 {r['tp']} / 初始止损 {r['stopped']} / "
              f"保本止损 {r['be_exits']} / 收盘 {r['eod']}")
        print(f"拉保本 {r['be_moves']} 次 | 无交易日 {r['no_trade_days']} 天 | "
              f"被上限压制 {r['capped']} 天")
        print(f"最终权益 ${r['final']:,.0f} | 收益 {r['ret']*100:+.1f}% | "
              f"年化 {r['annual']*100:+.1f}% | MDD {r['mdd']*100:.1f}%")
        if r["entries"] == 0:
            print("  ⚠️ 0 交易：本金太低买不起 1 手，请把本金提到上方提示的最低值以上。")
        print()


if __name__ == "__main__":
    main()
