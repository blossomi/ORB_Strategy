# -*- coding: utf-8 -*-
"""实证: 在纯 bar 回测下, FillModel 的滑点是否真的作用到成交价上。

跑同一个小窗口两次 (默认 FillModel vs OneTickSlippageFillModel), 对比每笔成交价。
窗口: 2016-01-04 ~ 2016-01-15 (两周, 样本小跑得快)。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import OneTickSlippageFillModel, PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

import orb_backtes_v8_4 as V

V.START_DATE = "2016-01-04"
V.END_DATE = "2016-01-15"

range_map = V.build_range_map()
atr_map = V.build_atr_map()
day_last_bar = V.build_day_last_bar_map()
bars, instrument, bar_type = V.build_bars_and_instrument()
print(f"bars={len(bars)} 窗口 {V.START_DATE}~{V.END_DATE}", flush=True)


def run(fill_model, tag):
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("EXP-" + tag),
        logging=LoggingConfig(log_level="ERROR"),
    ))
    kw = dict(
        venue=Venue(V.VENUE),
        oms_type=OmsType.NETTING,
        account_type=AccountType.MARGIN,
        base_currency=USD,
        starting_balances=[Money(V.STARTING_CAPITAL, USD)],
        fee_model=PerContractFeeModel(Money(V.COMMISSION_PER_CONTRACT, USD)),
    )
    if fill_model is not None:
        kw["fill_model"] = fill_model
    engine.add_venue(**kw)
    engine.add_instrument(instrument)
    engine.add_data(bars)
    cfg = V.OrbStrategyConfig(
        instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
        risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
        atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
        be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS,
    )
    engine.add_strategy(V.OrbStrategy(cfg, atr_map, range_map, day_last_bar))
    engine.run()
    pos = engine.trader.generate_positions_report()
    rows = []
    for _, p in pos.iterrows():
        if p["ts_closed"] is None:
            continue
        rows.append({
            "t": str(pd.Timestamp(p["ts_opened"]).tz_convert(V.ET)),
            "side": str(p["entry"]),
            "qty": int(p["peak_qty"]),
            "open": float(p["avg_px_open"]),
            "close": float(p["avg_px_close"]),
            "pnl": V.money_float(p["realized_pnl"]),
        })
    return pd.DataFrame(rows)


base = run(None, "BASE")
slip = run(OneTickSlippageFillModel(), "SLIP")

print("\n===== 成交价对比 =====")
print("默认 FillModel 笔数:", len(base), " OneTickSlippage 笔数:", len(slip))
if len(base) and len(slip) and len(base) == len(slip):
    m = base.merge(slip, on="t", suffixes=("_base", "_slip"))
    m["d_open"] = m["open_slip"] - m["open_base"]
    m["d_close"] = m["close_slip"] - m["close_base"]
    print(m[["t", "side_base", "qty_base", "open_base", "open_slip", "d_open",
             "close_base", "close_slip", "d_close"]].to_string(index=False))
    print("\n成交价差异: 入场 非零笔数 %d/%d, 出场 非零笔数 %d/%d"
          % ((m["d_open"].abs() > 1e-9).sum(), len(m),
             (m["d_close"].abs() > 1e-9).sum(), len(m)))
    print("总盈亏: 默认 $%.2f  1tick滑点 $%.2f  差 $%.2f"
          % (base["pnl"].sum(), slip["pnl"].sum(), slip["pnl"].sum() - base["pnl"].sum()))
else:
    print("两次运行笔数不一致, 无法逐笔对比 -> 说明滑点模型改变了成交/撮合路径")
    print(base[["t", "side", "qty", "open", "close", "pnl"]].to_string(index=False))
    print(slip[["t", "side", "qty", "open", "close", "pnl"]].to_string(index=False))
