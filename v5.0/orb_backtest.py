# -*- coding: utf-8 -*-
"""
orb_backtest.py — v5.0 回测主线 (FSM 架构, 策略语义 = v8.4 零变化)
====================================================
策略决策全部委托给 orb_fsm.OrbFsm; 本文件只是适配层 (env + commands) 与数据管道。

与原版 archive/ORB_strategy/orb_backtes_v8_4.py 的关系:
  - 行为契约: 逐笔等价 (entry/exit 时间、方向、手数、价格、PnL、出场原因全同),
    由 parity_check.py 对基线 CSV 验证 —— 它是本版能不能用的裁判。
  - 代码结构: 十几个平行状态变量 → 显式状态机 (见 orb_fsm.py 头注)。
  - 性能: 数据管道重写 —— RTH parquet 只读 1 次 (原版读 3 次+结尾再读 1 次),
    ET 时间预计算成 {ts_ns: (date, time)} 字典 (原版每根 bar 2 次 pd.Timestamp
    链式 tz_convert), Bar 构造 iterrows → itertuples + Price/Quantity 直接构造。
  - 新增防护 (干净数据下恒不触发, parity 断言为 0): 隔夜残留强平 / 止损单死亡重挂 /
    迟到入场成交平仓 / EOD 闹钟幂等。

用法: cd v5.0 && ../.venv/bin/python orb_backtest.py
"""
import os
import time as walltime
from datetime import date as ddate
from datetime import time as dtime
from math import floor, sqrt
from pathlib import Path

import numpy as np
import pandas as pd
import zoneinfo

from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import LoggingConfig, StrategyConfig
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, Symbol, TraderId, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from orb_fsm import FLAT, FsmCommands, FsmEnv, FsmParams, OrbFsm

# ===========================================================================
# 参数 (与原版「参数开关区」同值 —— parity 的前提)
# ===========================================================================
# 数据两件套 (parquet 不进库, 重建 = 从 archive/ORB_strategy/ 拷):
#   RTH = 常规时段 (每日 78 根 5m, 09:30-16:00) —— 撮合 + ATR/收盘语义
#   ETH = 含盘前 (每日 288 根) —— 只为取 9:00-9:30 盘前区间 (RTH 里没有盘前 bar)
DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_PATH = str(DATA_DIR / "nq_5min_rth.parquet")
RANGE_DATA_PATH = str(DATA_DIR / "nq_5min_eth.parquet")
OUT_DIR = Path(__file__).resolve().parent / "results"

# 样本窗口。⚠️ 起点是使用者手改的"活页": 引用任何数字必须带起点。
#   硬规则: ≥2016 (2010-2015 数据本身稀疏); 2021 起属样本选择 (跳过 2019-2020)
END_DATE = "2026-08-30"                  # 数据终点 (覆盖到 08-28 RTH / 08-30 ETH)
START_DATE = "2021-01-01"

# ==============================================================================
# 合约配置信息
# ==============================================================================
# 合成连续合约: 整个样本当一张永不换月的合约跑 (GLBX = CME 数据平台标识)。
#   含义: 换月价差未建模; 好处: 换月日无假仓位/假成交, parity 可逐笔对齐。
INSTRUMENT_ID = "NQ.GLBX"
VENUE = "GLBX"
MULTIPLIER = 2.0        # MNQ 每点价值 $2: 价格变动 1 点 → 每手盈亏 ±$2 (NQ 大合约是 $20)
TICK = 0.25             # 最小变动价位 (点): MNQ 1 tick = 0.25 点 = $0.50/手
PRICE_PRECISION = 2

# ==============================================================================
#  时间&区间
# ==============================================================================
ET = zoneinfo.ZoneInfo("America/New_York")
T_RANGE_START = dtime(9, 0)    # 盘前区间窗 [9:00, 9:30): 6 根 5m bar 的高/低 = 突破区间
T_RANGE_END = dtime(9, 30)     # 右端必须配 <: 吞了 9:30 开盘首根 = 灾难 (PF 1.04, 实测)
T_WIN_START = dtime(9, 30)     # 入场窗 [9:30, 10:10): 逐根**收盘价**判突破 (不用 high/low:
T_WIN_END = dtime(10, 10)      #  触及判定有双边歧义; 窗毕无突破 → 当日放弃, 不追)
#  bar 时间戳都是 ET 开盘左标签 (标签 09:30 的 bar 覆盖 09:30:00-09:34:59)

# R = 本策略的单笔风险单位 = 止损距离 (点)。+2R = 赚 2 倍止损距离, -1R = 打止损。
# 收益结构: 胜率仅 ~16%, 全部利润来自右尾 (P95 +9.6R), 左尾被止损封底 ≈ -1.1R
# —— 任何截断右尾的参数 (紧 BE/止盈/trailing) 都实测伤策略, 改 BE 相关参数前先读 notebook。
BE_R_MULTIPLE = 5        # 浮盈触及 5×止损距离 → 止损拉到保本 (一次性; 5R 推荐, 1-2R 有害)
BE_BUFFER_TICKS = 1      # 保本价在入场价上再垫 1 tick 覆盖成本; 锁定推荐口径 = 0

# BE 判定用的 R 取哪一个 (两处 R 在反推生效时会不同):
#   True  = 反推**之前**的名义 ATR 止损距离 (7.5%×ATR, tick 取整后)
#           -> 与 csv 导出的 r_multiple 口径一致; 且与手数取整解耦,
#              永远锚在 "N × 7.5%ATR 的价格位移" 上, 不会因小手数被放大
#   False = 反推**之后**实际挂在市场的止损距离
#           -> 与真实单笔风险同源 (N R 就是 N 倍单笔风险), 但手数越小时 N R 被撑得越远
BE_USE_NOMINAL_R = True


ATR_PERIOD = 14             # Wilder 14 日 ATR (ewm alpha=1/14 平滑); 永远用**前一日**值
ATR_STOP_FRACTION = 0.075   # 止损距离 = 7.5% × 前一日 ATR (点)。5%-10% 合适;
                            # 0.5%-1% 灾难 (止损小于单根噪声, 实测爆仓)
ADJUST_STOP_TO_RISK = True  # 反推止损: floor 取整丢了风险预算 → 反向放宽止损距离补回
                            # (实测中位仅 +1.75%, 复利大手数后趋近 0; 关停评估 = notebook 待办)

STARTING_CAPITAL = 25000    # 初始权益: 决定"买不买得起 1 手"(NQ 标准合约 2026 年 1% 风险
                            # 需 ≥$43k) + 复利路径。小本金必须用 MNQ
RISK_PER_TRADE = 0.007      # 每笔风险 = 权益 × 0.7% → 手数 = floor(风险额/(止损距离×$2))。
                            # 0.3%-1.0% 只放大年化/MDD (Sharpe 不动); 1.5% 档 MDD -65% 不可接受
MAX_QTY = 200               # 单笔手数硬帽: 风险手数超过它时被压到它 (权益后期几乎必触)
LEVERAGE_CAP = None                 # 名义杠杆帽: qty×入场价×$2 ≤ cap×权益; None=不设
                                    # (敏感性结论见 notebook 持久结论 A「名义杠杆帽」)

COMMISSION_PER_CONTRACT = 0.5   # $0.50/手/边 (IB MNQ 实际 ≈$0.49, 含交易所+监管费)
SLIPPAGE_TICKS = 1              # 滑点 1 tick。⚠️ 实现在 fee_model 里折成固定每手费用
                                # (见 engine.add_venue) —— 均值口径, 非逐笔随机; 实测滑点
                                # (slippage_tracker) 出来多少 tick 就用它复测边界参数

FSM_PARAMS = FsmParams(
    tick=TICK, multiplier=MULTIPLIER, risk_per_trade=RISK_PER_TRADE,
    atr_stop_fraction=ATR_STOP_FRACTION, max_qty=MAX_QTY,
    leverage_cap=LEVERAGE_CAP,
    be_r_multiple=BE_R_MULTIPLE, be_buffer_ticks=BE_BUFFER_TICKS,
    be_use_nominal_r=BE_USE_NOMINAL_R, adjust_stop_to_risk=ADJUST_STOP_TO_RISK,
    t_win_start=T_WIN_START, t_win_end=T_WIN_END,
)


def tick_round(px: float) -> float:
    return round(round(px / TICK) * TICK, PRICE_PRECISION)


# ===========================================================================
# 数据管道 (与原版公式逐行一致; 每个文件只读一次)
# ===========================================================================
def build_range_map(eth_df: pd.DataFrame) -> dict[ddate, tuple[float, float]]:
    """每日盘前区间 {date: (high, low)}。
    右开写法 [9:00, 9:30) 配 <: 命中盘前 6 根 (09:00…09:25), 正确排除开盘首根
    (标签 09:30, 量 2.3 万手 vs 盘前数百手)。已验证边界敏感性: 只有「盘前 30 分钟、
    右开」这一档成立, 相邻档全更差 (细节 notebook 持久结论 A)。"""
    df = eth_df.tz_convert(ET)
    t = df.index.time
    df = df[(t >= T_RANGE_START) & (t < T_RANGE_END)]
    out = {}
    for d, grp in df.groupby(df.index.normalize()):
        out[d.date()] = (float(grp["high"].max()), float(grp["low"].min()))
    return out


def build_atr_map(rth_df: pd.DataFrame) -> dict[ddate, float]:
    """每日 ATR {date: 前一日 ATR 点数}。
    Wilder 平滑 = TR 的 ewm(alpha=1/14) (TR = 三值取大: 高低差 / |高-昨收| / |低-昨收|)。
    shift(1) = 用**前一日**的 ATR —— 当日还没走完, 用当日值 = 未来函数。"""
    df = rth_df.tz_convert(ET)
    day = (df.resample("1D")
           .agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
           .dropna())
    prev_close = day["close"].shift(1)
    tr = pd.concat([day["high"] - day["low"],
                    (day["high"] - prev_close).abs(),
                    (day["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()
    atr_use = atr.shift(1)
    return {d.date(): float(v) for d, v in atr_use.dropna().items()}


def build_day_last_bar_map(rth_df: pd.DataFrame) -> dict[ddate, dtime]:
    """每日实际最后一根 5m bar 的标签时刻 (常态 15:55, 半日市 12:50)。
    EOD 平仓锚定它而非固定 15:55 —— 修复"半日市跨夜"bug (半日在 12:50 收口,
    固定 15:55 会拿着仓位从 12:50 裸奔到隔夜)。"""
    df = rth_df.tz_convert(ET)
    ts = df.index.to_series()
    last = ts.groupby(ts.dt.normalize()).max()
    return {t.date(): t.time() for t in last}


def build_bars_and_instrument(rth_df: pd.DataFrame, et_lookup: dict):
    """样本窗口过滤 + Bar 构造 (itertuples + Price/Quantity 直接构造, 免 f-string)。"""
    df = rth_df.tz_convert(ET)
    start = pd.Timestamp(START_DATE, tz=ET)
    end = pd.Timestamp(END_DATE, tz=ET) + pd.Timedelta(days=1)
    df = df[(df.index >= start) & (df.index < end)].tz_convert("UTC").sort_index()

    instrument_id = InstrumentId.from_str(INSTRUMENT_ID)
    first_ns = dt_to_unix_nanos(df.index[0])
    last_ns = dt_to_unix_nanos(df.index[-1])
    instrument = FuturesContract(
        instrument_id=instrument_id, raw_symbol=Symbol("NQ"),
        asset_class=AssetClass.INDEX, currency=USD,
        price_precision=PRICE_PRECISION,
        price_increment=Price(TICK, PRICE_PRECISION),
        multiplier=Quantity(MULTIPLIER, 2), lot_size=Quantity(1, 0),
        underlying="NQ",
        activation_ns=first_ns - 86_400_000_000_000,
        expiration_ns=last_ns + 3_652_000_000_000_000,
        ts_event=first_ns, ts_init=first_ns)

    bar_type = BarType.from_str(f"{INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL")

    # ET (date, time) 预计算: 向量化 tz_convert 一次, 之后每根 bar 字典 O(1)
    et_idx = df.index.tz_convert(ET)
    et_lookup.update(zip((i.value for i in df.index),
                         zip(et_idx.date, et_idx.time)))

    bars = []
    append = bars.append
    for ts, o, h, l, c, v in zip(df.index, df["open"], df["high"], df["low"],
                                 df["close"], df["volume"]):
        ns = ts.value
        append(Bar(bar_type=bar_type,
                   open=Price(round(o, 2), 2), high=Price(round(h, 2), 2),
                   low=Price(round(l, 2), 2), close=Price(round(c, 2), 2),
                   volume=Quantity(int(v), 0),
                   ts_event=ns, ts_init=ns))
    return bars, instrument, bar_type, df


# ===========================================================================
# 适配层: 一个类同时实现 FsmEnv (读) + FsmCommands (写) + nautilus Strategy
# ===========================================================================
class OrbFsmConfig(StrategyConfig):
    instrument_id: str
    bar_type: str


class OrbFsmBacktestStrategy(FsmEnv, FsmCommands, Strategy):
    """继承两个端口基类: 漏实现任何端口方法 → 立即 NotImplementedError (不再静默)。"""
    def __init__(self, config: OrbFsmConfig, params: FsmParams,
                 atr_map: dict, range_map: dict, day_last_bar: dict,
                 et_lookup: dict):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.atr_map = atr_map
        self.range_map = range_map
        self.day_last_bar = day_last_bar
        self.et_lookup = et_lookup
        # FSM 持有 self —— env/commands 都是这个适配层 (回测/live 差异全部收口在
        # env 方法和 command 实现里, 决策逻辑零拷贝)
        self.fsm = OrbFsm(params, env=self, cmds=self, audit=False)
        self._entry_cids: set = set()
        self._stop_order = None          # 当前止损单对象 (modify 需要对象而非 cid)

    # ---------------- 生命周期 ----------------
    def on_start(self):
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        d, t = self.et_lookup[bar.ts_event]      # 预计算表; 引擎 bar 必来自注入数据
        self.fsm.on_bar(bar.ts_event, d, t,
                        bar.open.as_double(), bar.high.as_double(),
                        bar.low.as_double(), bar.close.as_double())

    # ---------------- FsmEnv 实现 (回测语义) ----------------
    def equity(self) -> float:
        eq = self.portfolio.equity(venue=self.instrument_id.venue)
        return eq[USD].as_double()

    def atr_for(self, d: ddate):
        return self.atr_map.get(d)

    def range_for(self, d: ddate):
        return self.range_map.get(d)

    def be_ok(self, d: ddate, t: dtime) -> bool:
        last = self.day_last_bar.get(d)
        return last is not None and t < last

    def flatten_now(self, d: ddate, t: dtime) -> bool:
        last = self.day_last_bar.get(d)
        return last is not None and t == last

    # ---------------- FsmCommands 实现 ----------------
    def submit_entry_market(self, side: str, qty: int, ref: str) -> None:
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide[side],
            quantity=Quantity(qty, 0))
        self._entry_cids.add(order.client_order_id)
        self.submit_order(order)             # 回测: submit 内同步成交 → 重入 FSM

    def place_stop_market(self, exit_side: str, qty: int, trigger: float,
                          ref: str) -> None:
        sl = self.order_factory.stop_market(
            instrument_id=self.instrument_id,
            order_side=OrderSide[exit_side],
            quantity=Quantity(qty, 0),
            trigger_price=Price(trigger, PRICE_PRECISION),
            reduce_only=True)
        self._stop_order = sl
        self.submit_order(sl)

    def resize_stop(self, qty: int) -> None:
        self.modify_order(self._stop_order, quantity=Quantity(qty, 0))

    def modify_stop_trigger(self, trigger: float) -> None:
        self.modify_order(self._stop_order,
                          trigger_price=Price(trigger, PRICE_PRECISION))

    def cancel_all(self) -> None:
        self.cancel_all_orders(self.instrument_id)

    def flatten_position(self, reason: str, ref: str) -> None:
        self.close_all_positions(self.instrument_id)   # 回测语义, 对齐原版 EOD 分支

    def fsm_log(self, msg: str, level: str = "info") -> None:
        getattr(self.log, level)(msg)

    # ---------------- 成交回报 → FSM 事件 ----------------
    def on_order_filled(self, event):
        cid = event.client_order_id
        if cid in self._entry_cids:
            self.fsm.on_entry_fill(event.last_px.as_double(),
                                   int(event.last_qty.as_double()), event.ts_event)
        elif self._stop_order is not None and cid == self._stop_order.client_order_id:
            self.fsm.on_stop_fill(event.last_px.as_double(),
                                  int(event.last_qty.as_double()), event.ts_event)
        # EOD close_all_positions 生成的市价单成交: 忽略 (对齐原版)


# ===========================================================================
# 统计 + CSV 导出 (与原版同公式; parity 的输出面)
# ===========================================================================
def money_float(x) -> float:
    s = str(x).replace(",", "")
    for tok in s.split():
        try:
            return float(tok)
        except ValueError:
            continue
    return 0.0


def to_sec(ts) -> int:
    if isinstance(ts, (int, float)):
        return int(ts / 1_000_000_000)
    return int(pd.Timestamp(ts).timestamp())


def export_trades_csv(engine, out_path, atr_map):
    import csv
    pos = engine.trader.generate_positions_report()
    ordr = engine.trader.generate_orders_report()

    closing = {}
    for idx, row in ordr.iterrows():
        typ = str(row["type"])
        trig = None
        if "STOP" in typ:
            try:
                trig = float(row["trigger_price"])
            except (TypeError, ValueError):
                trig = None
        closing[idx] = (typ, trig)

    rows = []
    for _, p in pos.iterrows():
        if p["ts_closed"] is None:
            continue
        t_open = pd.Timestamp(to_sec(p["ts_opened"]), unit="s", tz="UTC").tz_convert(ET)
        t_close = pd.Timestamp(to_sec(p["ts_closed"]), unit="s", tz="UTC").tz_convert(ET)
        side = "LONG" if str(p["entry"]) == "BUY" else "SHORT"
        qty = int(p["peak_qty"])
        entry_px = float(p["avg_px_open"])
        exit_px = float(p["avg_px_close"])
        pnl = money_float(p["realized_pnl"])
        dur = (t_close - t_open).total_seconds() / 60

        atr = atr_map.get(t_open.date())
        stop_dist = max(TICK, tick_round(ATR_STOP_FRACTION * atr)) if atr else None
        r_mult = pnl / (qty * stop_dist * MULTIPLIER) if (stop_dist and stop_dist > 0) else None

        ctype, trig = closing.get(p["closing_order_id"], ("MARKET", None))
        # 出场归因启发式: 保本止损的触发价 ≈ 入场价(+1 tick 缓冲), 与初始止损
        # (7.5%×ATR, 近年 30+ 点) 差一个量级 → 2 点阈值足以区分两者
        if "STOP" in ctype:
            reason = "保本止损" if (trig is not None and abs(trig - entry_px) < 2.0) else "初始止损"
        else:
            reason = "收盘平仓"

        rows.append([t_open.strftime("%Y-%m-%d %H:%M:%S"), t_close.strftime("%Y-%m-%d %H:%M:%S"),
                     side, qty, round(entry_px, 2), round(exit_px, 2),
                     round(pnl, 2), round(stop_dist, 2) if stop_dist else "",
                     round(r_mult, 2) if r_mult is not None else "",
                     reason, round(dur, 1)])

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["entry_time_et", "exit_time_et", "side", "qty", "entry_price",
                    "exit_price", "pnl_usd", "stop_dist_pt", "r_multiple",
                    "exit_reason", "duration_min"])
        w.writerows(rows)
    return len(rows)


def print_stats(engine, strategy, sample_df):
    """绩效指标 (口径提示): 年化 = CAGR = (终值/本金)^(1/年数)-1; Sharpe/Sortino 由
    **日收益率** ×√252 年化 (日内策略无隔夜持仓, 日粒度无哑区间); R/PF 等逐笔口径
    见 export_trades_csv。本策略 Sortino ≫ Sharpe (右偏长尾), 降权 Sharpe。"""
    acct = engine.trader.generate_account_report(Venue(VENUE))
    eq = acct['total'].astype(float)
    eq.index = pd.to_datetime(acct.index)
    daily = eq.resample('1D').last().dropna()
    peak = daily.cummax()
    mdd = float((daily - peak).div(peak).min())

    final_total = float(eq.iloc[-1])
    years = (sample_df.index[-1] - sample_df.index[0]).days / 365.25
    annual = (final_total / STARTING_CAPITAL) ** (1.0 / years) - 1.0

    ret = daily.pct_change().dropna()
    r = ret.to_numpy()
    sharpe = float(r.mean() / r.std() * sqrt(252)) if r.std() > 0 else 0.0
    downside = np.minimum(r, 0.0)
    dstd = float(np.sqrt(np.mean(downside ** 2)))
    sortino = float(r.mean() / dstd * sqrt(252)) if dstd > 0 else 0.0

    pos = engine.trader.generate_positions_report()
    closed = [(pd.Timestamp(p["ts_opened"]), money_float(p["realized_pnl"]))
              for _, p in pos.iterrows() if p["ts_closed"] is not None]
    closed.sort(key=lambda x: x[0])
    pnls = np.array([v for _, v in closed])
    winrate = float((pnls > 0).sum() / len(pnls)) if len(pnls) else 0.0
    wins = pnls[pnls > 0].sum()
    losses = abs(pnls[pnls <= 0].sum())
    pf = float(wins / losses) if losses > 0 else float("inf")

    max_win_streak = max_loss_streak = 0
    cur_win = cur_loss = 0
    for v in pnls:
        if v > 0:
            cur_win += 1; cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        else:
            cur_loss += 1; cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)

    f = strategy.fsm
    print(f"\n===== 入场次数: {f.n_entries:,} =====")
    print(f"===== 浮盈达 {BE_R_MULTIPLE}R → 拉保本: {f.n_be_moves:,} 次 =====")
    print(f"===== 出场分布: 初始止损 {f.n_stopped:,}  |  保本止损 {f.n_be_exits:,}  |  收盘平仓 {f.n_eod:,} =====")
    print(f"===== 当日无突破: {f.n_no_trade:,} 天 | 买不起: {f.n_cant_afford:,} 天 "
          f"| 压顶: {f.n_capped:,} 天 | 杠杆帽: {f.n_lev_capped:,} 天 "
          f"| 整除跳过: {f.n_lot_exact:,} 天 =====")
    guards = (f.n_overnight_flattens + f.n_stop_replaces + f.n_timer_flattens
              + f.n_late_entry_flattens)
    print(f"===== 新防护触发次数 (干净数据必须=0): {guards} =====")
    print(f"最终权益: ${final_total:,.2f}  总盈亏: ${final_total - STARTING_CAPITAL:,.2f}  "
          f"收益率: {(final_total / STARTING_CAPITAL - 1) * 100:,.1f}%")
    pf_str = "∞" if np.isinf(pf) else f"{pf:.2f}"
    print(f"回测年限: {years:.2f} 年   年化: {annual * 100:,.1f}%    Sharpe: {sharpe:.2f}    "
          f"Sortino: {sortino:.2f}    胜率: {winrate * 100:.1f}%    PF: {pf_str}")
    print(f"峰值权益: ${daily.max():,.0f}   最大回撤: {mdd * 100:.1f}%")
    print(f"最大连胜: {max_win_streak} 笔   最大连败: {max_loss_streak} 笔")
    return final_total


# ===========================================================================
# 主流程
# ===========================================================================
if __name__ == "__main__":
    t0 = walltime.perf_counter()
    rth = pd.read_parquet(DATA_PATH)
    eth = pd.read_parquet(RANGE_DATA_PATH)
    range_map = build_range_map(eth)
    atr_map = build_atr_map(rth)
    day_last_bar = build_day_last_bar_map(rth)
    et_lookup: dict = {}
    bars, instrument, bar_type, sample_df = build_bars_and_instrument(rth, et_lookup)
    t1 = walltime.perf_counter()
    print(f"[数据] RTH {len(rth):,} 行 + ETH {len(eth):,} 行 → 区间 {len(range_map):,} 天 | "
          f"ATR {len(atr_map):,} 天 | {len(bars):,} 根 Bar   ({t1 - t0:.1f}s)")

    engine = BacktestEngine(config=BacktestEngineConfig(
        trader_id=TraderId("ORB-BT-V50"),
        logging=LoggingConfig(log_level="WARNING")))
    engine.add_venue(
        venue=Venue(VENUE), oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
        base_currency=USD, starting_balances=[Money(STARTING_CAPITAL, USD)],
        # 成本建模: 滑点折进固定每手费用 (每边 = 佣金 + N tick × $2)。
        # 这是均值口径而非逐笔随机滑点 —— 数字对"平均每边成本"敏感、对滑点分布形状不敏感;
        # 1 tick 来回 = $2/手, 在低波动年 (2017 止损中位 3.67pt) 占 1R 比例爆炸, 是
        # 回测数字最大的不真实来源 (真实滑点以 slippage_tracker 实测为准)
        fee_model=PerContractFeeModel(
            Money(COMMISSION_PER_CONTRACT + SLIPPAGE_TICKS * TICK * MULTIPLIER, USD)))
    engine.add_instrument(instrument)
    engine.add_data(bars)

    strategy = OrbFsmBacktestStrategy(
        OrbFsmConfig(instrument_id=INSTRUMENT_ID, bar_type=str(bar_type)),
        FSM_PARAMS, atr_map, range_map, day_last_bar, et_lookup)
    engine.add_strategy(strategy)

    t2 = walltime.perf_counter()
    engine.run()
    t3 = walltime.perf_counter()
    print(f"[引擎] run: {t3 - t2:.1f}s")

    final = print_stats(engine, strategy, sample_df)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_csv = OUT_DIR / f"v5_trades_{STARTING_CAPITAL}.csv"
    n = export_trades_csv(engine, out_csv, atr_map)
    t4 = walltime.perf_counter()
    print(f"\n已导出逐笔 CSV: {out_csv}  ({n:,} 笔)")
    print(f"分段计时: 数据 {t1 - t0:.1f}s | 引擎 {t3 - t2:.1f}s | 统计+导出 {t4 - t3:.1f}s "
          f"| 总计 {t4 - t0:.1f}s")
