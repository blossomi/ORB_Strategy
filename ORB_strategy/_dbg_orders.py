# -*- coding: utf-8 -*-
"""排查 B 组: 分笔成交时为什么挂了 2 倍止损单。打印订单状态/数量明细。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity

import live_ib_demo as L

L.SLIP_CSV = "_dbg_orders.csv"
if os.path.exists(L.SLIP_CSV):
    os.remove(L.SLIP_CSV)

INSTR = "NQ.GLBX"
BAR_TYPE = f"{INSTR}-5-MINUTE-LAST-EXTERNAL"
df = pd.read_parquet("nq_5min_eth.parquet").tz_convert(L.ET)
s = pd.Timestamp("2016-01-01", tz=L.ET)
e = pd.Timestamp("2016-02-15", tz=L.ET) + pd.Timedelta(days=1)
df = df[(df.index >= s) & (df.index < e)].tz_convert("UTC").sort_index()
first_ns, last_ns = dt_to_unix_nanos(df.index[0]), dt_to_unix_nanos(df.index[-1])
instrument = FuturesContract(
    instrument_id=InstrumentId.from_str(INSTR), raw_symbol=Symbol("NQ"),
    asset_class=AssetClass.INDEX, currency=USD, price_precision=2,
    price_increment=Price.from_str("0.25"), multiplier=Quantity.from_str("2.00"),
    lot_size=Quantity.from_str("1"), underlying="NQ",
    activation_ns=first_ns - 86_400_000_000_000,
    expiration_ns=last_ns + 3_652_000_000_000_000, ts_event=first_ns, ts_init=first_ns)
bars = [Bar(bar_type=BarType.from_str(BAR_TYPE),
            open=Price.from_str(f"{r.open:.2f}"), high=Price.from_str(f"{r.high:.2f}"),
            low=Price.from_str(f"{r.low:.2f}"), close=Price.from_str(f"{r.close:.2f}"),
            volume=Quantity.from_str(str(int(r.volume))),
            ts_event=dt_to_unix_nanos(ts), ts_init=dt_to_unix_nanos(ts)) for ts, r in df.iterrows()]

engine = BacktestEngine(config=BacktestEngineConfig(
    trader_id=TraderId("DBG-1"), logging=LoggingConfig(log_level="ERROR")))
engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                 base_currency=USD, starting_balances=[Money(25000, USD)],
                 fee_model=PerContractFeeModel(Money(1.0, USD)))
engine.add_instrument(instrument)
engine.add_data(bars)
strat = L.OrbLiveStrategy(L.OrbLiveConfig(
    instrument_id=INSTR, bar_type=BAR_TYPE, qty="20000", stop_pts=30.0, dry_run=False))
engine.add_strategy(strat)
engine.run()

orders = engine.trader.generate_orders_report()
print("订单报告列:", list(orders.columns))
t = orders["type"].astype(str)
stops = orders[t.str.contains("STOP")]
print(f"\nSTOP 单共 {len(stops)} 张")
for col in ("status", "quantity", "filled_qty", "side", "reduce_only"):
    if col in stops.columns:
        print(f"  {col} 分布: {stops[col].astype(str).value_counts().to_dict()}")

mk = orders[t == "MARKET"]
print(f"\nMARKET 单共 {len(mk)} 张")
if "status" in mk.columns:
    print("  status:", mk["status"].astype(str).value_counts().to_dict())
if "quantity" in mk.columns:
    print("  quantity 前 8:", mk["quantity"].astype(str).tolist()[:8])

print("\n一次入场的订单时间线(前 6 条):")
cols = [c for c in ("client_order_id", "type", "status", "side", "quantity", "ts_init") if c in orders.columns]
o = orders.sort_values("ts_init")
print(o[cols].head(12).to_string(index=False))
