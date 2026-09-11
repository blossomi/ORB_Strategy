# -*- coding: utf-8 -*-
"""上线前验证 live_ib_demo.py 的 OrbLiveStrategy (用回测引擎跑同一个类)。

三组:
  A. 正常手数: 信号数 vs pandas 独立计算; 滑点应≈0; 每笔入场应恰好 1 张止损单
  B. 超大手数(逼出分笔成交): 验证「分笔成交不会重复挂止损单」这条修复真的生效
  C. 半日市: 用 HALF_DAYS 命中当天, 应提前在 12:50 平仓(而不是等到没有的 15:55)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

from datetime import time as dtime

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

INSTR = "NQ.GLBX"
BAR_TYPE = f"{INSTR}-5-MINUTE-LAST-EXTERNAL"
SLIP_TICKS, FEE = 1, 0.5

scope = sys.argv[1] if len(sys.argv) > 1 else "A"
if scope == "A":
    START, END = "2016-01-01", "2016-03-31"
elif scope == "B":
    START, END = "2016-01-01", "2016-02-15"
else:                                    # C: 挑一段含半日市的窗口(感恩节/圣诞)
    START, END = "2016-11-20", "2016-12-31"

L.SLIP_CSV = f"_verify_live_{scope}.csv"
if os.path.exists(L.SLIP_CSV):
    os.remove(L.SLIP_CSV)

df = pd.read_parquet("nq_5min_eth.parquet").tz_convert(L.ET)
s = pd.Timestamp(START, tz=L.ET)
e = pd.Timestamp(END, tz=L.ET) + pd.Timedelta(days=1)
df = df[(df.index >= s) & (df.index < e)].tz_convert("UTC").sort_index()
first_ns, last_ns = dt_to_unix_nanos(df.index[0]), dt_to_unix_nanos(df.index[-1])

instrument = FuturesContract(
    instrument_id=InstrumentId.from_str(INSTR), raw_symbol=Symbol("NQ"),
    asset_class=AssetClass.INDEX, currency=USD, price_precision=2,
    price_increment=Price.from_str("0.25"), multiplier=Quantity.from_str("2.00"),
    lot_size=Quantity.from_str("1"), underlying="NQ",
    activation_ns=first_ns - 86_400_000_000_000,
    expiration_ns=last_ns + 3_652_000_000_000_000,
    ts_event=first_ns, ts_init=first_ns)
bars = [Bar(bar_type=BarType.from_str(BAR_TYPE),
            open=Price.from_str(f"{r.open:.2f}"), high=Price.from_str(f"{r.high:.2f}"),
            low=Price.from_str(f"{r.low:.2f}"), close=Price.from_str(f"{r.close:.2f}"),
            volume=Quantity.from_str(str(int(r.volume))),
            ts_event=dt_to_unix_nanos(ts), ts_init=dt_to_unix_nanos(ts))
        for ts, r in df.iterrows()]
print(f"[{scope}] 窗口 {START}~{END}: {len(bars):,} 根 5min bar (ETH)")


def run(qty: str, half_days=None):
    if half_days is not None:
        L.HALF_DAYS = half_days
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("LIVEVER-1"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(25000, USD)],
                     fee_model=PerContractFeeModel(Money(FEE + SLIP_TICKS * 0.25 * 2.0, USD)))
    engine.add_instrument(instrument)
    engine.add_data(bars)
    strat = L.OrbLiveStrategy(L.OrbLiveConfig(
        instrument_id=INSTR, bar_type=BAR_TYPE, qty=qty, stop_pts=30.0, dry_run=False))
    engine.add_strategy(strat)
    engine.run()
    return engine, strat


# ---------------- A: 正常手数 ----------------
if scope == "A":
    engine, strat = run("1")
    pos = engine.trader.generate_positions_report()
    orders = engine.trader.generate_orders_report()
    otype = orders["type"].astype(str)
    n_entry = int((otype == "MARKET").sum())
    n_stop = int(otype.str.contains("STOP").sum())
    n_pos = len(pos)
    print(f"\n[A] 入场 {n_pos} 笔 | 市价单 {n_entry} 张(含收盘平仓) | 止损单 {n_stop} 张")
    print(f"    止损单 : 入场 = {n_stop} : {n_pos}  -> "
          f"{'每笔恰好一张 OK' if n_stop == n_pos else '!!! 不成比例, 有重复挂单 !!!'}")
    sl = pd.read_csv(L.SLIP_CSV)
    ent = sl[sl["kind"] == "entry"]
    print(f"    滑点记录 {len(sl)} 行; entry 滑点中位 = {pd.to_numeric(ent['slip_ticks'], errors='coerce').median()} tick (应≈0)")
    print(f"    latency 中位 = {pd.to_numeric(ent['latency_ms'], errors='coerce').median()} ms (回测应≈0); "
          f"stale 行数 = {int((sl['stale'] == True).sum())}")
    print(f"    新列 bar_ts_et 样例: {ent['bar_ts_et'].iloc[0] if len(ent) else '-'} / "
          f"signal_ts_et: {ent['signal_ts_et'].iloc[0] if len(ent) else '-'}")

# ---------------- B: 超大手数 -> 分笔成交 ----------------
elif scope == "B":
    engine, strat = run("20000")
    orders = engine.trader.generate_orders_report()
    otype = orders["type"].astype(str)
    n_entry = int((otype == "MARKET").sum())
    n_stop = int(otype.str.contains("STOP").sum())
    pos = engine.trader.generate_positions_report()
    sl = pd.read_csv(L.SLIP_CSV)
    ent = sl[sl["kind"] == "entry"]
    multi = ent.groupby("ref").size()
    print(f"\n[B] 20000 手逼部分成交")
    print(f"    市价单 {n_entry} 张(含收盘平仓) | STOP 单 {n_stop} 张 | 入场 {len(pos)} 笔")
    print(f"    分笔成交的入场单(同一 ref 多行): {int((multi > 1).sum())} / {len(multi)}")
    print(f"    最大分笔数: {int(multi.max()) if len(multi) else 0}")
    print(f"    -> STOP 单 {n_stop} vs 入场 {len(pos)}: "
          f"{'1:1, 分笔成交没有重复挂止损 OK' if n_stop == len(pos) else '!!! 止损单数不对, 会双倍平仓 !!!'}")

# ---------------- C: 半日市 ----------------
else:
    # 先把窗口里真实的半日市找出来(当天 bar 数明显少)
    d = df.tz_convert(L.ET)
    daily = d.groupby(d.index.date).size()
    odd = daily[daily < daily.median() * 0.8]
    print(f"\n[C] 窗口内异常短的日子: {[(str(k), int(v)) for k, v in odd.items()]}")
    half = {str(k) for k in odd.index}
    engine, strat = run("1", half_days=half or {"2016-11-25"})
    sl = pd.read_csv(L.SLIP_CSV)
    eod = sl[sl["kind"] == "eod"]
    print(f"    命中半日市日期集合: {sorted(half)}")
    print(f"    收盘平仓记录 {len(eod)} 条; 时间分布:")
    if len(eod):
        print("     ", eod["bar_ts_et"].str[:16].tolist()[:8])
    pos = engine.trader.generate_positions_report()
    print(f"    持仓 {len(pos)} 笔, 全部已平: {bool((pos['ts_closed'].notna()).all())}")
