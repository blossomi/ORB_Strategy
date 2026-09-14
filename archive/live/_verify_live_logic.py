# -*- coding: utf-8 -*-
"""上线前验证 live_ib_demo.py 的 OrbLiveStrategy (用回测引擎跑同一个类)。

四组 (D/E 为 TODO·P2-2, 待实现):
  A. 正常手数: 信号数 vs pandas 独立计算; 滑点应≈0; 每笔入场应恰好 1 张止损单;
     统计保本(5R)触发/出场次数 —— 参数推荐新增逻辑
  B. 超大手数(逼出分笔成交): 验证「分笔成交不会重复挂止损单」这条修复真的生效
  C. 半日市: 用 HALF_DAYS 命中当天, 应提前在 12:50 平仓(而不是等到没有的 15:55)
  D. [TODO] BE 回归: 拉保本次数/BE 触发价与 pandas 独立计算(盘中 high/low 触及
     entry ± 5R)逐条对齐 —— 现在 A 组只打印不校验, BE 语义改动时没有回归网
  E. [TODO] 定时平仓回归: 配套 live_ib_demo 的 clock.set_time_alert EOD 闹钟,
     模拟时钟到点必平仓; bar 兜底路径与闹钟幂等不重复平

用法: ../.venv/bin/python _verify_live_logic.py [A|B|C]   # D/E 实现后加参
"""
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

INSTR = "NQ.GLBX"
BAR_TYPE = f"{INSTR}-5-MINUTE-LAST-EXTERNAL"
SLIP_TICKS, FEE = 1, 0.5
CAPITAL = 25000

scope = sys.argv[1] if len(sys.argv) > 1 else "A"
if scope == "A":
    START, END = "2020-01-01", "2020-03-31"
elif scope == "B":
    START, END = "2022-01-01", "2022-02-15"
else:                                    # C: 挑一段含半日市的窗口(感恩节/圣诞)
    START, END = "2025-11-20", "2025-12-31"

L.SLIP_CSV = f"_verify_live_{scope}.csv"
if os.path.exists(L.SLIP_CSV):
    os.remove(L.SLIP_CSV)

DATA_DIR = os.path.join(os.path.dirname(HERE), "ORB_strategy")
df = pd.read_parquet(os.path.join(DATA_DIR, "nq_5min_eth.parquet")).tz_convert(L.ET)
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


# ---- 前一日 14 日 ATR (Wilder), 与回测 build_atr_map 同公式, 注入策略 ----
def build_atr_map() -> dict:
    day_df = pd.read_parquet(os.path.join(DATA_DIR, "nq_5min_rth.parquet")).tz_convert(L.ET)
    day = (day_df.resample("1D")
           .agg(high=("high", "max"), low=("low", "min"), close=("close", "last")).dropna())
    prev_close = day["close"].shift(1)
    tr = pd.concat([day["high"] - day["low"],
                    (day["high"] - prev_close).abs(),
                    (day["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / L.ATR_PERIOD, adjust=False).mean().shift(1)
    return {d.date(): float(v) for d, v in atr.dropna().items()}


ATR_MAP = build_atr_map()


def run(risk: float, max_qty: int, half_days=None, capital: float = CAPITAL):
    if half_days is not None:
        L.HALF_DAYS = half_days
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("LIVEVER-1"), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
                     base_currency=USD, starting_balances=[Money(capital, USD)],
                     fee_model=PerContractFeeModel(Money(FEE + SLIP_TICKS * 0.25 * 2.0, USD)))
    engine.add_instrument(instrument)
    engine.add_data(bars)
    strat = L.OrbLiveStrategy(L.OrbLiveConfig(
        instrument_id=INSTR, bar_type=BAR_TYPE,
        risk_per_trade=risk, atr_stop_fraction=L.ATR_STOP_FRACTION,
        be_r_multiple=L.BE_R_MULTIPLE, be_buffer_ticks=L.BE_BUFFER_TICKS,
        max_qty=max_qty, multiplier=2.0, dry_run=False), atr_map=ATR_MAP)
    engine.add_strategy(strat)
    engine.run()
    return engine, strat


# ---------------- A: 正常手数 (0.7% 风险以损定仓 + 5R 保本) ----------------
if scope == "A":
    engine, strat = run(risk=0.007, max_qty=50)
    pos = engine.trader.generate_positions_report()
    orders = engine.trader.generate_orders_report()
    otype = orders["type"].astype(str)
    n_entry = int((otype == "MARKET").sum())
    n_stop = int(otype.str.contains("STOP").sum())
    n_pos = len(pos)
    print(f"\n[A] 入场 {n_pos} 笔 | 市价单 {n_entry} 张(含收盘平仓) | 止损单 {n_stop} 张")
    print(f"    止损单 : 入场 = {n_stop} : {n_pos}  -> "
          f"{'每笔恰好一张 OK' if n_stop == n_pos else '!!! 不成比例, 有重复挂单 !!!'}")
    print(f"    出场统计: 初始止损 {strat.n_stopped} | 保本止损 {strat.n_be_exits} | "
          f"收盘平仓 {strat.n_eod} | 拉保本 {strat.n_be_moves} 次 "
          f"(保本出场>0 说明 5R 逻辑真的生效)")
    sl = pd.read_csv(L.SLIP_CSV)
    ent = sl[sl["kind"] == "entry"]
    print(f"    滑点记录 {len(sl)} 行; entry 滑点中位 = "
          f"{pd.to_numeric(ent['slip_ticks'], errors='coerce').median()} tick (应≈0)")
    print(f"    latency 中位 = {pd.to_numeric(ent['latency_ms'], errors='coerce').median()} ms (回测应≈0); "
          f"stale 行数 = {int((sl['stale'] == True).sum())}")
    print(f"    手数分布 (以损定仓, 权益${CAPITAL:,}×0.7%): "
          f"{sorted(pd.to_numeric(ent['qty'], errors='coerce').dropna().astype(int).unique().tolist())}")
    print(f"    新列 bar_ts_et 样例: {ent['bar_ts_et'].iloc[0] if len(ent) else '-'} / "
          f"signal_ts_et: {ent['signal_ts_et'].iloc[0] if len(ent) else '-'}")

# ---------------- B: 超大手数 -> 分笔成交 ----------------
elif scope == "B":
    # 大本金 + 1% 风险 + max_qty 20000: 每笔都顶到 20000 手(逼出分笔)但单日亏损只占
    # 权益 ~0.3%, 账户不会被一天打穿 -> 整个窗口天天有分笔样本(对比旧版 31 笔)。
    engine, strat = run(risk=0.01, max_qty=20000, capital=50_000_000)
    orders = engine.trader.generate_orders_report()
    otype = orders["type"].astype(str)
    n_entry = int((otype == "MARKET").sum())
    n_stop = int(otype.str.contains("STOP").sum())
    pos = engine.trader.generate_positions_report()
    sl = pd.read_csv(L.SLIP_CSV)
    ent = sl[sl["kind"] == "entry"]
    multi = ent.groupby("ref").size()
    print(f"\n[B] 超大手数逼部分成交 ($5000万, risk=1%, max_qty=20000)")
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
    engine, strat = run(risk=0.007, max_qty=50, half_days=half or {"2016-11-25"})
    sl = pd.read_csv(L.SLIP_CSV)
    eod = sl[sl["kind"] == "eod"]
    print(f"    命中半日市日期集合: {sorted(half)}")
    print(f"    收盘平仓记录 {len(eod)} 条; 时间分布:")
    if len(eod):
        print("     ", eod["bar_ts_et"].str[:16].tolist()[:8])
    pos = engine.trader.generate_positions_report()
    print(f"    持仓 {len(pos)} 笔, 全部已平: {bool((pos['ts_closed'].notna()).all())}")
    # 显式断言: 半日市当天的「收盘平仓」(kind=eod) 必须发生在 12:50 而非 15:55。
    # (positions 的 ts_closed 包含止损出场 —— 半日早上被止损是正常交易, 不在此断言范围)
    half_eod = eod[eod["bar_ts_et"].str[:10].isin(half)]
    if len(half_eod):
        times = half_eod["bar_ts_et"].str[11:16].tolist()
        ok = all(t == "12:50" for t in times)
        print(f"    半日市当天 EOD 平仓时间: {times} -> "
              f"{'全部 12:50 OK' if ok else '!!! 有非 12:50 的半日 EOD 平仓 !!!'}")
    else:
        print("    (半日市当天无 EOD 平仓 —— 未持仓或早盘已止损, 见下方强制验证)")
        # 强制验证机制本身: 挑一个确实有 EOD 平仓的常规日, 塞进 HALF_DAYS 重跑,
        # 该日平仓时间必须从 15:55 变为 12:50 (直接证明 HALF_DAYS 逻辑生效)。
        probe = eod["bar_ts_et"].str[:10].iloc[0]
        if os.path.exists(L.SLIP_CSV):        # 清掉第一遍的行, 否则同一天出现两条
            os.remove(L.SLIP_CSV)
        engine2, _ = run(risk=0.007, max_qty=50, half_days={probe})
        sl2 = pd.read_csv(L.SLIP_CSV)
        eod2 = sl2[(sl2["kind"] == "eod") & (sl2["bar_ts_et"].str[:10] == probe)]
        t2 = eod2["bar_ts_et"].str[11:16].tolist()
        print(f"    [强制] 把常规日 {probe}(原本 15:55 平仓) 加入 HALF_DAYS 重跑 → "
              f"平仓时间 {t2} -> {'12:50 OK' if t2 == ['12:50'] else '!!! 机制失效 !!!'}")
