# -*- coding: utf-8 -*-
"""验证 (窗口 2016 全年):

[A] 缺陷 4 —— 「入场当根 K 线的 high/low 判定保本」是否真的发生 (未来函数)
    patch 三个方法记录时序:
      on_bar        -> 标记当前是否处在 on_bar 调用栈内
      _enter        -> 记录触发入场的 bar.ts_event
      on_order_filled -> 记录成交回调是否发生在 on_bar 内
      _check_be     -> 记录「拉保本发生在哪根 bar」, 并与入场 bar 对比

[B] 缺陷 1 修复 —— 滑点是否进入成本 (对比总盈亏 / 费用)
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
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, OmsType
from nautilus_trader.model.identifiers import TraderId, Venue
from nautilus_trader.model.objects import Money

import orb_backtes_v8_4 as V

V.START_DATE = "2016-01-01"
V.END_DATE = "2016-12-31"

S = {"in_on_bar": False, "cur_bar_ts": None, "last_entry_bar_ts": None,
     "fills": [], "be": [], "check_calls_with_trade": 0, "check_calls_total": 0}

_o_on_bar = V.OrbStrategy.on_bar
_o_enter = V.OrbStrategy._enter
_o_filled = V.OrbStrategy.on_order_filled
_o_check = V.OrbStrategy._check_be


def on_bar(self, bar):
    S["in_on_bar"] = True
    S["cur_bar_ts"] = bar.ts_event
    try:
        _o_on_bar(self, bar)
    finally:
        S["in_on_bar"] = False


def enter(self, side, bar, d):
    S["last_entry_bar_ts"] = bar.ts_event
    _o_enter(self, side, bar, d)


def filled(self, event):
    cid = event.client_order_id
    if cid in self.pending_entry or cid in self._entry_filled:
        S["fills"].append({
            "cid": str(cid),
            "in_on_bar": S["in_on_bar"],
            "cur_bar_ts": S["cur_bar_ts"],
            "entry_bar_ts": S["last_entry_bar_ts"],
        })
    _o_filled(self, event)


def check(self, bar):
    S["check_calls_total"] += 1
    if self._trade is not None:
        S["check_calls_with_trade"] += 1
    before = self._trade is not None and self._trade["stop_moved"]
    _o_check(self, bar)
    after = self._trade is not None and self._trade["stop_moved"]
    if after and not before:
        S["be"].append({
            "bar_ts": bar.ts_event,
            "entry_bar_ts": S["last_entry_bar_ts"],
            "same_bar": bar.ts_event == S["last_entry_bar_ts"],
        })


V.OrbStrategy.on_bar = on_bar
V.OrbStrategy._enter = enter
V.OrbStrategy.on_order_filled = filled
V.OrbStrategy._check_be = check

range_map = V.build_range_map()
atr_map = V.build_atr_map()
day_last_bar = V.build_day_last_bar_map()
bars, instrument, bar_type = V.build_bars_and_instrument()
print(f"窗口 {V.START_DATE}~{V.END_DATE}, bars={len(bars)}, 乘数={instrument.multiplier}", flush=True)

engine = BacktestEngine(config=BacktestEngineConfig(
    trader_id=TraderId("VERIFY-001"), logging=LoggingConfig(log_level="ERROR")))
engine.add_venue(
    venue=Venue(V.VENUE), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
    base_currency=USD, starting_balances=[Money(V.STARTING_CAPITAL, USD)],
    fee_model=PerContractFeeModel(
        Money(V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER, USD)),
)
engine.add_instrument(instrument)
engine.add_data(bars)
cfg = V.OrbStrategyConfig(
    instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
    risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
    atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
    be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS,
)
strat = V.OrbStrategy(cfg, atr_map, range_map, day_last_bar)
engine.add_strategy(strat)
print("运行 ...", flush=True)
engine.run()

f = pd.DataFrame(S["fills"])
b = pd.DataFrame(S["be"])

print("\n" + "=" * 62)
print("[A] 缺陷 4 验证: 入场当根 K 线的保本判定 (未来函数)")
print("=" * 62)
print(f"入场成交回调次数            : {len(f)}")
if len(f):
    print(f"  其中发生在 on_bar 调用栈内 : {int(f['in_on_bar'].sum())}  <- 若为 0, 说明成交回调是异步的")
    print(f"  成交时的 bar == 入场 bar   : {int((f['cur_bar_ts'] == f['entry_bar_ts']).sum())}")
print(f"拉保本(触发)次数             : {len(b)}   (策略自计 n_be_moves={strat.n_be_moves})")
if len(b):
    print(f"  其中发生在入场当根 K 线内 : {int(b['same_bar'].sum())}  <- >0 即坐实未来函数")
    print(f"  发生在入场之后的 K 线内   : {int((~b['same_bar']).sum())}")
print(f"_check_be 调用总次数        : {S['check_calls_total']}")
print(f"  其中持仓非空(真的在判)    : {S['check_calls_with_trade']}")

print("\n" + "=" * 62)
print("[B] 缺陷 1 验证: 滑点成本是否生效")
print("=" * 62)
acct = engine.trader.generate_account_report(Venue(V.VENUE))
eq = acct["total"].astype(float)
print(f"手续费 ${V.COMMISSION_PER_CONTRACT}/手/边 + 滑点 {V.SLIPPAGE_TICKS} tick/手/边 "
      f"= 每手每边 ${V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER}")
print(f"最终权益 ${float(eq.iloc[-1]):,.2f}  (起始 ${V.STARTING_CAPITAL:,})")
print(f"入场次数 {strat.n_entries}, 出场分布: 初始止损 {strat.n_stopped} / 保本 {strat.n_be_exits} / 收盘 {strat.n_eod}")
print(f"买不起手数天数 {strat.n_cant_afford} / 无突破天数 {strat.n_no_trade} / 被上限压制 {strat.n_capped}")
print("\n" + V.STRATEGY_DESC)
