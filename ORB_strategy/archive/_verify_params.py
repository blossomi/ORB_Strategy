# -*- coding: utf-8 -*-
"""验证: 顶部「参数开关区」的常量是否真的传到策略体内 (以 be_r_multiple 为重点)。

[A] 静态: 构建 strategy 实例, 逐个比对 实例属性 vs 顶部常量 vs OrbStrategyConfig 默认值
[B] 动态: 2016 全年窗口跑两次 (BE_R_MULTIPLE=3 与 =1), 看 n_be_moves / 出场分布是否随之变化
    (若参数没生效, 两次结果会完全一致)
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

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

range_map = V.build_range_map()
atr_map = V.build_atr_map()
day_last_bar = V.build_day_last_bar_map()
bars, instrument, bar_type = V.build_bars_and_instrument()

NAMES = ("risk_per_trade", "multiplier", "atr_stop_fraction",
         "max_qty", "be_r_multiple", "be_buffer_ticks")

# ---------- [A] 静态: 属性链路 ----------
# 只传必需字段, 看其余字段的默认值是多少 (Nautilus Config 是 msgspec Struct, 不是 dataclass)
try:
    _min = V.OrbStrategyConfig(instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type))
    cfg_defaults = {n: getattr(_min, n, "—") for n in NAMES}
except Exception as exc:                                    # noqa: BLE001
    print("取 Config 默认值失败:", exc)
    cfg_defaults = {}

cfg = V.OrbStrategyConfig(
    instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
    risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
    atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
    be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS,
)
strat = V.OrbStrategy(cfg, atr_map, range_map, day_last_bar)

print("=" * 96)
print("[A] 参数链路: 顶部开关区常量  ->  Config  ->  strategy 实例属性")
print("=" * 96)
print(f"{'参数名':<20}{'顶部开关区':<14}{'Config 默认值':<16}{'实例属性':<14}{'一致?'}")
for name in NAMES:
    top = getattr(V, name.upper())
    inst = getattr(strat, name, "<无此属性>")
    dv = cfg_defaults.get(name, "—")
    ok = "OK" if inst == top else "*** 不一致 ***"
    note = "" if dv == top else "   <- 默认值已过时, 靠主流程显式传参覆盖"
    print(f"{name:<20}{str(top):<14}{str(dv):<16}{str(inst):<14}{ok}{note}")

print()
print("策略体内读取这些参数的全部位置:")
src = open("orb_backtes_v8_4.py", encoding="utf-8").read().splitlines()
start = next(i for i, l in enumerate(src) if l.startswith("class OrbStrategy(Strategy):"))
end = next(i for i, l in enumerate(src) if "def build_bars_and_instrument" in l)
pat = re.compile(r"self\.(risk_per_trade|multiplier|atr_stop_fraction|max_qty|be_r_multiple|be_buffer_ticks)\b")
hits = 0
for i in range(start, end):
    for m in pat.finditer(src[i]):
        hits += 1
        print(f"  L{i+1}: self.{m.group(1)}   |  {src[i].strip()[:88]}")
print(f"  共 {hits} 处")


# ---------- [B] 动态: 改 be_r_multiple 是否会改变结果 ----------
def run(be_r):
    V.BE_R_MULTIPLE = be_r
    c = V.OrbStrategyConfig(
        instrument_id=V.INSTRUMENT_ID, bar_type=str(bar_type),
        risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
        atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
        be_r_multiple=be_r, be_buffer_ticks=V.BE_BUFFER_TICKS,
    )
    e = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId(f"PARAM-BE{int(be_r)}"), logging=LoggingConfig(log_level="ERROR")))
    e.add_venue(
        venue=Venue(V.VENUE), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        base_currency=USD, starting_balances=[Money(V.STARTING_CAPITAL, USD)],
        fee_model=PerContractFeeModel(
            Money(V.COMMISSION_PER_CONTRACT + V.SLIPPAGE_TICKS * V.TICK * V.MULTIPLIER, USD)),
    )
    e.add_instrument(instrument)
    e.add_data(bars)
    s = V.OrbStrategy(c, atr_map, range_map, day_last_bar)
    e.add_strategy(s)
    e.run()
    acct = e.trader.generate_account_report(Venue(V.VENUE))
    return dict(be=be_r, inst_be=s.be_r_multiple, moves=s.n_be_moves, stopped=s.n_stopped,
                be_exits=s.n_be_exits, eod=s.n_eod, entries=s.n_entries,
                final=float(acct["total"].astype(float).iloc[-1]))


print()
print("=" * 96)
print("[B] 动态: BE_R_MULTIPLE=3 vs =1 (2016 全年, 其余参数不变)")
print("=" * 96)
r3 = run(3.0)
r1 = run(1.0)
print(f"{'BE_R':<8}{'实例属性':<10}{'入场':<8}{'拉保本次数':<12}{'初始止损':<10}{'保本止损':<10}{'收盘':<8}{'终值'}")
for r in (r3, r1):
    print(f"{r['be']:<8}{r['inst_be']:<10}{r['entries']:<8}{r['moves']:<12}{r['stopped']:<10}"
          f"{r['be_exits']:<10}{r['eod']:<8}${r['final']:,.0f}")
same = (r3["moves"] == r1["moves"] and abs(r3["final"] - r1["final"]) < 1e-6)
print("\n两次结果是否完全相同 ->", "是 (参数没生效!)" if same else "否 (参数确实生效)")
