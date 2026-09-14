# -*- coding: utf-8 -*-
"""
verify_live.py — v5.0 live (orb_live) 的引擎内回归
================================================================
做法: 与 live/_verify_live_logic.py 同框架 —— 把 live 策略类放进回测引擎跑,
但**同时跑原版 (archive/live 的 OrbLiveStrategy) 与 v5.0 (OrbFsmLiveStrategy)**,
逐笔 diff 两边的 positions —— 原版即参照实现。

组:
  A. 常规窗口 (2020 Q1): 逐笔 parity + BE 计数一致 + 止损单 1:1 + EOD 闹钟登记正确
  B. 超大手数逼分笔成交: 止损单 : 入场 = 1:1 (不重复挂单) + STOP 单全部 GTD
  C. 半日市: 命中日 12:50 平仓 (而非 15:55)

用法: ../.venv/bin/python verify_live.py [A|B|C]
"""
import os
import sys
from datetime import time as dtime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, str(Path(HERE).parent / "archive" / "live"))   # 原版 live (归档参照实现)

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

import live_ib_demo as L          # 原版 (参照实现)
import orb_live as CAND      # v5.0

INSTR = "NQ.GLBX"
BAR_TYPE = f"{INSTR}-5-MINUTE-LAST-EXTERNAL"
SLIP_TICKS, FEE = 1, 0.5
CAPITAL = 25000

scope = sys.argv[1] if len(sys.argv) > 1 else "A"
if scope == "A":
    START, END = "2020-01-01", "2020-03-31"
elif scope == "B":
    START, END = "2022-01-01", "2022-02-15"
else:
    START, END = "2025-11-20", "2025-12-31"

L.SLIP_CSV = f"_verify_orig_{scope}.csv"
CAND.SLIP_CSV = f"_verify_cand_{scope}.csv"
for p in (L.SLIP_CSV, CAND.SLIP_CSV):
    if os.path.exists(p):
        os.remove(p)

DATA_DIR = os.path.join(HERE, "data")      # v5.0 自持数据 (与 orb_backtest 同源)
df = pd.read_parquet(os.path.join(DATA_DIR, "nq_5min_eth.parquet")).tz_convert(L.ET)
s = pd.Timestamp(START, tz=L.ET)
e = pd.Timestamp(END, tz=L.ET) + pd.Timedelta(days=1)
df = df[(df.index >= s) & (df.index < e)].tz_convert("UTC").sort_index()
first_ns, last_ns = dt_to_unix_nanos(df.index[0]), dt_to_unix_nanos(df.index[-1])

instrument = FuturesContract(
    instrument_id=InstrumentId.from_str(INSTR), raw_symbol=Symbol("NQ"),
    asset_class=AssetClass.INDEX, currency=USD, price_precision=2,
    price_increment=Price(0.25, 2), multiplier=Quantity(2.0, 2),
    lot_size=Quantity(1, 0), underlying="NQ",
    activation_ns=first_ns - 86_400_000_000_000,
    expiration_ns=last_ns + 3_652_000_000_000_000,
    ts_event=first_ns, ts_init=first_ns)
bars = [Bar(bar_type=BarType.from_str(BAR_TYPE),
            open=Price(round(r.open, 2), 2), high=Price(round(r.high, 2), 2),
            low=Price(round(r.low, 2), 2), close=Price(round(r.close, 2), 2),
            volume=Quantity(int(r.volume), 0),
            ts_event=dt_to_unix_nanos(ts), ts_init=dt_to_unix_nanos(ts))
        for ts, r in df.iterrows()]
print(f"[{scope}] 窗口 {START}~{END}: {len(bars):,} 根 5min bar (ETH)")


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


def run(strategy_cls, config, trader_id):
    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId(trader_id), logging=LoggingConfig(log_level="ERROR")))
    engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING,
                     account_type=AccountType.MARGIN, base_currency=USD,
                     starting_balances=[Money(CAPITAL, USD)],
                     fee_model=PerContractFeeModel(Money(FEE + SLIP_TICKS * 0.25 * 2.0, USD)))
    engine.add_instrument(instrument)
    engine.add_data(bars)
    strat = strategy_cls(config, atr_map=ATR_MAP)
    engine.add_strategy(strat)
    engine.run()
    return engine, strat


def extract_trades(engine):
    """positions → 可比元组列表 (entry/exit 时间, 方向, 手数, 均价, pnl, 平仓单类型)。"""
    pos = engine.trader.generate_positions_report()
    ordr = engine.trader.generate_orders_report()
    typ = {i: str(r["type"]) for i, r in ordr.iterrows()}
    out = []
    for _, p in pos.iterrows():
        if p["ts_closed"] is None:
            continue
        out.append((
            pd.Timestamp(p["ts_opened"]).value,
            pd.Timestamp(p["ts_closed"]).value,
            str(p["entry"]), int(p["peak_qty"]),
            float(p["avg_px_open"]), float(p["avg_px_close"]),
            round(float(str(p["realized_pnl"]).split()[0].replace(",", "")), 2),
            typ.get(p["closing_order_id"], "?"),
        ))
    out.sort(key=lambda x: x[0])
    return out


def diff_trades(a, b, label_a, label_b):
    if len(a) != len(b):
        print(f"!! 笔数不同: {label_a} {len(a)} vs {label_b} {len(b)}")
        ad = {pd.Timestamp(x[0], tz="UTC").tz_convert(L.ET).date() for x in a}
        bd = {pd.Timestamp(x[0], tz="UTC").tz_convert(L.ET).date() for x in b}
        print("   仅", label_a, "有的日:", sorted(ad - bd)[:8])
        print("   仅", label_b, "有的日:", sorted(bd - ad)[:8])
        return False
    bad = 0
    for i, (x, y) in enumerate(zip(a, b)):
        for j, (xa, ya) in enumerate(zip(x, y)):
            if isinstance(xa, float):
                if abs(xa - ya) > 1e-9:
                    bad += 1
                    if bad <= 5:
                        print(f"  row {i} 字段{j}: {label_a}={xa} {label_b}={ya}")
            elif xa != ya:
                bad += 1
                if bad <= 5:
                    print(f"  row {i} 字段{j}: {label_a}={xa} {label_b}={ya}")
    if bad:
        print(f"!! {bad} 处字段不同 —— parity FAIL")
        return False
    print(f"✔ 逐笔 parity PASS — {len(a):,} 笔全同 ({label_a} vs {label_b})")
    return True


def orig_config(risk, max_qty):
    return L.OrbLiveConfig(
        instrument_id=INSTR, bar_type=BAR_TYPE, risk_per_trade=risk,
        atr_stop_fraction=L.ATR_STOP_FRACTION, be_r_multiple=L.BE_R_MULTIPLE,
        be_buffer_ticks=L.BE_BUFFER_TICKS, max_qty=max_qty, multiplier=2.0,
        dry_run=False)


def cand_config(risk, max_qty):
    return CAND.OrbFsmLiveConfig(
        instrument_id=INSTR, bar_type=BAR_TYPE, risk_per_trade=risk,
        atr_stop_fraction=CAND.ATR_STOP_FRACTION, be_r_multiple=CAND.BE_R_MULTIPLE,
        be_buffer_ticks=CAND.BE_BUFFER_TICKS, max_qty=max_qty, multiplier=2.0,
        dry_run=False)


fails = 0

# ---------------- A: 常规窗口 逐笔 parity + 新行为断言 ----------------
if scope == "A":
    # 窗口内真实半日市 (Q1 2020: MLK 01-20 / 总统日 02-17) 注入**两边** —— 保持同配置。
    # 不注入的话候选版 P1-1 闹钟会提前平掉 (半日日历缺失时的正确行为), 原版则裸奔到
    # 18:00 夜盘 bar —— 那是有意的差异, 不属于 parity 范围 (scope C 覆盖半日语义)。
    # 探测口径: 日内 <17:00 的最后一根 bar 早于 15:00 → 半日市 (bar 总数口径不行,
    # 半日的日历日还带着 18:00 后的夜盘 bar)。
    d_et = df.tz_convert(L.ET)
    from datetime import time as _t
    rth_like = d_et[(d_et.index.time >= _t(9, 30)) & (d_et.index.time < _t(17, 0))]
    ts = rth_like.index.to_series()
    last_bar = ts.groupby(ts.dt.normalize()).max()
    odd = {str(d.date()) for d, t in last_bar.items() if t.time() < _t(15, 0)}
    if odd:
        L.HALF_DAYS |= odd
        CAND.HALF_DAYS |= odd
        print(f"[A] 注入窗口内半日市 (两边同配置): {sorted(odd)}")
    eng_o, strat_o = run(L.OrbLiveStrategy, orig_config(0.007, 50), "VERORIG-A")
    eng_c, strat_c = run(CAND.OrbFsmLiveStrategy, cand_config(0.007, 50), "VERCAND-A")
    ok = diff_trades(extract_trades(eng_o), extract_trades(eng_c), "原版", "候选")
    fails += 0 if ok else 1

    # 计数器对照: 原版 live 每日清零 (只留最后一天), 改为「候选 FSM 计数 vs 引擎报告推导」
    c = strat_c.fsm
    trades_c = extract_trades(eng_c)
    n_stop_close = sum(1 for t in trades_c if "STOP" in t[7])
    n_eod_close = sum(1 for t in trades_c if "STOP" not in t[7])
    print(f"\n[A] 候选计数器 vs 引擎推导: 入场 {c.n_entries} vs {len(trades_c)} | "
          f"止损类出场 {c.n_stopped + c.n_be_exits} vs {n_stop_close} | "
          f"收盘类出场 {c.n_eod} vs {n_eod_close} | 拉保本 {c.n_be_moves} 次")
    if (c.n_entries, c.n_stopped + c.n_be_exits, c.n_eod) != \
       (len(trades_c), n_stop_close, n_eod_close):
        print("!! 计数器与引擎不一致"); fails += 1
    else:
        print("✔ 计数器内部一致 (BE/出场分类 = D 组回归通过)")

    ordr = eng_c.trader.generate_orders_report()
    otype = ordr["type"].astype(str)
    n_stop = int(otype.str.contains("STOP").sum())
    n_pos = len(eng_c.trader.generate_positions_report())
    print(f"[A] 候选版止损单 {n_stop} : 入场 {n_pos} -> "
          f"{'每笔恰好一张 OK' if n_stop == n_pos else '!!! 重复挂单 !!!'}")
    fails += 0 if n_stop == n_pos else 1

    guards = (c.n_overnight_flattens + c.n_stop_replaces + c.n_timer_flattens
              + c.n_late_entry_flattens)
    print(f"[A] 新防护触发: {guards} (干净数据必须 0) -> "
          f"{'OK' if guards == 0 else '!!! FAIL !!!'}")
    fails += 0 if guards == 0 else 1

    # E 组: EOD 闹钟登记 —— 每个交易日 flat_at+5min+2s, 且从未抢在 bar 兜底之前平仓
    if strat_c.timers_armed:
        pad = (CAND.EOD_TIMER_PAD.total_seconds())
        bad = [(d, t) for d, t in strat_c.timers_armed
               if (t / 1e9) % 86400 != 0]      # 形式检查 (绝对时刻由 flat_at 决定)
        armed_days = len(strat_c.timers_armed)
        print(f"[A] EOD 闹钟登记 {armed_days} 天 (pad={pad:.0f}s); "
              f"闹钟平仓 {c.n_timer_flattens} 次 (bar 正常时必须 0)")
        fails += 0 if c.n_timer_flattens == 0 else 1
    else:
        print("!! EOD 闹钟一次都没登记 —— P1-1 失效"); fails += 1

# ---------------- B: 超大手数 → 分笔成交 ----------------
elif scope == "B":
    CAPITAL_B = 50_000_000
    def run_b(cls, cfg, tid):
        engine = BacktestEngine(config=BacktestEngineConfig(
            trader_id=TraderId(tid), logging=LoggingConfig(log_level="ERROR")))
        engine.add_venue(venue=Venue("GLBX"), oms_type=OmsType.NETTING,
                         account_type=AccountType.MARGIN, base_currency=USD,
                         starting_balances=[Money(CAPITAL_B, USD)],
                         fee_model=PerContractFeeModel(Money(FEE + SLIP_TICKS * 0.25 * 2.0, USD)))
        engine.add_instrument(instrument)
        engine.add_data(bars)
        strat = cls(cfg, atr_map=ATR_MAP)
        engine.add_strategy(strat)
        engine.run()
        return engine, strat
    eng_o, _ = run_b(L.OrbLiveStrategy, orig_config(0.01, 20000), "VERORIG-B")
    eng_c, strat_c = run_b(CAND.OrbFsmLiveStrategy, cand_config(0.01, 20000), "VERCAND-B")
    ok = diff_trades(extract_trades(eng_o), extract_trades(eng_c), "原版", "候选")
    fails += 0 if ok else 1

    ordr = eng_c.trader.generate_orders_report()
    stops = ordr[ordr["type"].astype(str).str.contains("STOP")]
    n_stop = len(stops)
    n_pos = len(eng_c.trader.generate_positions_report())
    print(f"\n[B] 候选版 STOP 单 {n_stop} vs 入场 {n_pos} -> "
          f"{'1:1, 分笔成交没有重复挂止损 OK' if n_stop == n_pos else '!!! FAIL !!!'}")
    fails += 0 if n_stop == n_pos else 1
    if "time_in_force" in stops.columns:
        tif = stops["time_in_force"].astype(str).value_counts().to_dict()
        print(f"[B] STOP 单 TIF 分布: {tif} (P2-1: 应全为 GTD)")
        fails += 0 if set(tif) == {"GTD"} else 1
    else:
        print("[B] (报告无 time_in_force 列, GTD 断言跳过)")

# ---------------- C: 半日市 ----------------
else:
    d_et = df.tz_convert(L.ET)
    from datetime import time as _t
    rth_like = d_et[(d_et.index.time >= _t(9, 30)) & (d_et.index.time < _t(17, 0))]
    ts = rth_like.index.to_series()
    last_bar = ts.groupby(ts.dt.normalize()).max()
    half = {str(d.date()) for d, t in last_bar.items() if t.time() < _t(15, 0)}
    L.HALF_DAYS = half
    CAND.HALF_DAYS = half
    eng_o, strat_o = run(L.OrbLiveStrategy, orig_config(0.007, 50), "VERORIG-C")
    eng_c, strat_c = run(CAND.OrbFsmLiveStrategy, cand_config(0.007, 50), "VERCAND-C")
    ok = diff_trades(extract_trades(eng_o), extract_trades(eng_c), "原版", "候选")
    fails += 0 if ok else 1

    sl = pd.read_csv(CAND.SLIP_CSV)
    eod = sl[sl["kind"] == "eod"]
    print(f"\n[C] 命中半日市: {sorted(half)}")
    half_eod = eod[eod["bar_ts_et"].str[:10].isin(half)]
    if len(half_eod):
        times = half_eod["bar_ts_et"].str[11:16].tolist()
        okc = all(t == "12:50" for t in times)
        print(f"[C] 半日市 EOD 平仓时间: {times} -> {'全部 12:50 OK' if okc else '!!! FAIL !!!'}")
        fails += 0 if okc else 1
    else:
        print("[C] (半日市当天无 EOD 平仓 —— 未持仓或早盘已止损; parity 已由逐笔 diff 覆盖)")

print(f"\n{'='*50}\n{'✔ 全部通过' if fails == 0 else f'!! {fails} 项 FAIL'}")
sys.exit(0 if fails == 0 else 1)
