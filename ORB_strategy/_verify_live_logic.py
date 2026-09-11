# -*- coding: utf-8 -*-
"""验证 live_ib_demo.py 里的 OrbLiveStrategy 逻辑(不下真单, 用回测引擎跑)。

为什么能这么验: OrbLiveStrategy 是标准 NautilusTrader Strategy, 直接塞进 BacktestEngine
就能跑。用 ETH 数据(含盘前 bar)当数据源, 这样策略能自己累积 9:00-9:29 区间。

对照: 同窗口用 pandas 独立算一遍突破次数, 看两边是否一致。
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

# 别污染真实记录文件
L.SLIP_CSV = "_verify_live_slippage.csv"
if os.path.exists(L.SLIP_CSV):
    os.remove(L.SLIP_CSV)

START, END = "2016-01-01", "2016-03-31"
INSTR = "NQ.GLBX"
BAR_TYPE = f"{INSTR}-5-MINUTE-LAST-EXTERNAL"
SLIP_TICKS = 1          # 让回测也有个可比的成本口径(与主脚本一致)
FEE = 0.5

df = pd.read_parquet("nq_5min_eth.parquet").tz_convert(L.ET)
s = pd.Timestamp(START, tz=L.ET)
e = pd.Timestamp(END, tz=L.ET) + pd.Timedelta(days=1)
df = df[(df.index >= s) & (df.index < e)].tz_convert("UTC").sort_index()
print(f"数据 {START}~{END}: {len(df):,} 根 5min bar (ETH, 含盘前)")

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


def run(dry_run):
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("LIVEVER-1"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(25000, USD)],
                     fee_model=PerContractFeeModel(Money(FEE + SLIP_TICKS * 0.25 * 2.0, USD)))
    engine.add_instrument(instrument)
    engine.add_data(bars)
    strat = L.OrbLiveStrategy(L.OrbLiveConfig(
        instrument_id=INSTR, bar_type=BAR_TYPE, qty="1", stop_pts=30.0, dry_run=dry_run))
    engine.add_strategy(strat)
    engine.run()
    pos = engine.trader.generate_positions_report()
    return strat, len(pos)


# ---------- ① DRY_RUN: 只验信号 ----------
strat, npos = run(dry_run=True)
sig = pd.read_csv(L.SLIP_CSV) if os.path.exists(L.SLIP_CSV) else pd.DataFrame()
print(f"\n[DRY_RUN] 策略落盘记录 {len(sig)} 行, 其中 entry 信号 "
      f"{int((sig['kind'] == 'entry').sum()) if len(sig) else 0} 个, 持仓 {npos} 笔(应为 0)")

# ---------- ② pandas 独立算突破 ----------
d = df.tz_convert(L.ET)
tt = d.index.time
rng = d[(tt >= dtime(9, 0)) & (tt < dtime(9, 29))]
win = d[(tt >= dtime(9, 30)) & (tt < dtime(10, 10))]
hi = rng.groupby(rng.index.date)["high"].max()
lo = rng.groupby(rng.index.date)["low"].min()
sigs = 0
for day, grp in win.groupby(win.index.date):
    if day not in hi.index:
        continue
    for c in grp["close"]:
        if c > hi[day] or c < lo[day]:
            sigs += 1
            break
print(f"[独立计算] 同窗口突破次数(每日至多一次) = {sigs}")

# ---------- ③ 真下单: 滑点记录器能不能正常配对 ----------
strat2, npos2 = run(dry_run=False)
sl = pd.read_csv(L.SLIP_CSV)
sl = sl[sl["kind"].isin(["entry", "stop", "eod"])]
print(f"\n[下单模式] 持仓 {npos2} 笔; 滑点记录 {len(sl)} 行")
print(f"  滑点(should be ~0, 回测成交价=bar收盘价=信号价): "
      f"中位 {pd.to_numeric(sl['slip_ticks'], errors='coerce').median()}")
print(f"  含止损记录 {int((sl['kind'] == 'stop').sum())} 条, 收盘记录 {int((sl['kind'] == 'eod').sum())} 条")
print("\n样例(前 6 行):")
cols = ["kind", "side", "qty", "signal_px", "fill_px", "slip_ticks", "latency_ms", "stale"]
print(sl[cols].head(6).to_string(index=False))
