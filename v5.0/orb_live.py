# -*- coding: utf-8 -*-
"""
orb_live.py — v5.0 live 主线 (FSM 架构, 策略语义 = v8.4 零变化)
====================================================================
原 archive/live/live_ib_demo.py 的状态机重构版 —— 决策逻辑全部委托
orb_fsm.OrbFsm, 本文件只是 live 适配层 (IB 行情/下单/滑点/日志)。
verify_live.py 已验证与原版逐笔对齐 (A/B/C 全绿)。

相对 archive/live/live_ib_demo.py 的实质变化 (TODO 清单的落地):
  ✓ P1-1 EOD 定时平仓: 换日时 clock.set_time_alert("eod_flat", flat_at+5min+2s),
    bar 触发路径保留为兜底, 两路幂等 (FSM day_closed 闸); on_stop cancel_timer。
    对齐 notebook「16:00:02 闹钟 = 15:55 标签 bar 收口」的语义。
  ✓ P1-2 隔夜残留仓位防护: 换日重置时 net_position≠0 → 撤单+市价强平+error 告警
    (原版只撤单不平仓, 仓位会裸奔一整天)。
  ✓ P2-1 止损单 GTD 当日过期 (flat_at+2min): 双保险防跨日残留单。
  ✓ 新增 (原版没有): 持仓中止损单被撤/被拒 → 立即重挂 + error; live 迟到成交
    (EOD 后才 fill) → 立即平仓, 不挂隔夜止损。
  ✓ ATR 数据层换外源 NDX 磁盘表 (atr_source.py, 2026-09-16 研判): 收盘后 17:10 ET
    定时更新 + 失败重试/桌面告警; 启动与换日从表重算, 不再向 IB 请求日线。
    表陈旧 ≤5 日历日 → 告警但照常交易; 缺失/超限 → 当日不开仓 (FSM atr=None 语义)。
  其余行为 (信号/定价/BE/滑点记录/DRY_RUN/日志审计) 与原版逐笔对齐。

用法:
  cd v5.0 && ../.venv/bin/python orb_live.py
  回归: ../.venv/bin/python verify_live.py A|B|C
"""
import os
from datetime import datetime, time, timedelta, timezone
from math import floor
from zoneinfo import ZoneInfo

from ibapi.common import MarketDataTypeEnum
from nautilus_trader.adapters.interactive_brokers.common import IBContract
from nautilus_trader.adapters.interactive_brokers.config import (
    InteractiveBrokersDataClientConfig,
    InteractiveBrokersExecClientConfig,
    InteractiveBrokersInstrumentProviderConfig,
)
from nautilus_trader.adapters.interactive_brokers.factories import (
    InteractiveBrokersLiveDataClientFactory,
    InteractiveBrokersLiveExecClientFactory,
)
from nautilus_trader.config import LoggingConfig, RoutingConfig, StrategyConfig
from nautilus_trader.live.node import TradingNode, TradingNodeConfig
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

# SlippageTracker = v5.0 自持副本 (源 archive/live/, 2026-09-15 拷贝)
from slippage_tracker import SlippageTracker  # noqa: E402

from atr_source import (ATR_PERIOD, ATR_STALE_DAYS, atr_from_table,  # noqa: E402
                        load_table, notify_desktop, table_freshness, update_table)
from orb_fsm import HOLD, FsmCommands, FsmEnv, FsmParams, OrbFsm  # noqa: E402

# ===========================================================================
# ★ 配置区 —— 与 live/live_ib_demo.py 保持一致 (parity 的前提)
# ===========================================================================
IB_HOST = "127.0.0.1"
IB_PORT = 4002
ACCOUNT_ID = "DUQ715008"
CLIENT_ID = 1

SYMBOL = "MNQ"
CONTRACT_MONTH = "202612"
LOCAL_SYMBOL = "Z6"
TICK = 0.25
MULTIPLIER = 2.0 if SYMBOL == "MNQ" else 20.0

DRY_RUN = True

RISK_PER_TRADE = 0.007
ATR_STOP_FRACTION = 0.075
BE_R_MULTIPLE = 5.0
BE_BUFFER_TICKS = 0
MAX_QTY = 50
ATR_OVERRIDE_PTS = None              # 人工兜底: 表不可用时手动钉死 ATR (点)

MARKET_DATA_TYPE = MarketDataTypeEnum.REALTIME

T_RANGE_START = time(9, 0)
T_RANGE_END = time(9, 29)
T_WIN_START = time(9, 30)
T_WIN_END = time(10, 10)
T_FLAT = time(15, 55)

HALF_DAY_FLAT = time(12, 50)
HALF_DAYS = {"2026-11-27", "2026-12-24"}

SLIP_CSV = "live_slippage.csv"
SLIP_STALE_MS = 2000

NQ_CONTRACT = IBContract(
    symbol=SYMBOL, secType="FUT", exchange="CME", currency="USD",
    lastTradeDateOrContractMonth=CONTRACT_MONTH,
)
INSTRUMENT_ID = f"{SYMBOL}{LOCAL_SYMBOL}.CME"
BAR_TYPE_STR = f"{INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL"

ET = ZoneInfo("America/New_York")

EOD_TIMER_NAME = "eod_flat"          # P1-1
EOD_TIMER_PAD = timedelta(minutes=5, seconds=2)   # flat_at + 5min + 2s
STOP_GTD_PAD = timedelta(minutes=2)              # P2-1: 止损 flat_at+2min 过期

ATR_UPDATE_TIMER = "atr_update"
ATR_UPDATE_AT = time(17, 10)         # 指数 16:00 收盘 → 留出发布时间 (研判: 盘后更新为主)
ATR_UPDATE_RETRIES = 3
ATR_UPDATE_RETRY_PAD = timedelta(minutes=60)


def tick_round(px: float) -> float:
    return round(round(px / TICK) * TICK, 2)


def flat_at_for(d) -> time:
    """当日收盘平仓时刻 (半日市提前)。"""
    return HALF_DAY_FLAT if str(d) in HALF_DAYS else T_FLAT


# ---------------- 运行日志 (与原版同机制) ----------------
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_log_path: str | None = None


def flog(msg: str) -> str:
    global _log_path
    os.makedirs(LOG_DIR, exist_ok=True)
    if _log_path is None:
        _log_path = os.path.join(LOG_DIR, f"live_{datetime.now(ET):%Y%m%d_%H%M%S}.log")
        with open(_log_path, "a", encoding="utf-8") as f:
            f.write(f"# 运行记录 启动于 {datetime.now(ET):%Y-%m-%d %H:%M:%S} ET\n")
    with open(_log_path, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(ET):%Y-%m-%d %H:%M:%S} {msg}\n")
    print(msg, flush=True)
    return _log_path


# ===========================================================================
# 策略: FSM 适配层
# ===========================================================================
class OrbFsmLiveConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    risk_per_trade: float
    atr_stop_fraction: float
    be_r_multiple: float
    be_buffer_ticks: int
    max_qty: int
    multiplier: float
    dry_run: bool


class OrbFsmLiveStrategy(FsmEnv, FsmCommands, Strategy):
    def __init__(self, config: OrbFsmLiveConfig, atr_map: dict | None = None):
        """atr_map: {date: ATR点数} —— 回归验证时注入 (与实盘日线请求二选一)。"""
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.risk_per_trade = float(config.risk_per_trade)
        self.atr_stop_fraction = float(config.atr_stop_fraction)
        self.be_r_multiple = float(config.be_r_multiple)
        self.be_buffer_ticks = int(config.be_buffer_ticks)
        self.max_qty = int(config.max_qty)
        self.multiplier = float(config.multiplier)
        self.dry_run = bool(config.dry_run)
        self.atr_map = atr_map                                  # None = 实盘模式
        self._atr_px: float | None = None
        self._atr_upd_attempts = 0                              # 收盘更新连续失败计数

        self.slip = SlippageTracker(SLIP_CSV, tick=TICK, multiplier=MULTIPLIER,
                                    stale_ms=SLIP_STALE_MS)
        self._order_role: dict[str, tuple[str, str]] = {}       # cid -> (角色, ref)
        self._stop_order_obj = None                             # 当前止损单对象 (modify 用)
        self._stop_cid = None                                   # 其 cid (撤/拒路由用)

        self._rng_hi: float | None = None
        self._rng_lo: float | None = None
        self._last_bar_date = None                              # bar 留痕用 (与 FSM.day 独立)

        self.timers_armed: list[tuple[str, int]] = []           # 验证用: 闹钟登记记录

        self.fsm = OrbFsm(
            FsmParams(tick=TICK, multiplier=self.multiplier,
                      risk_per_trade=self.risk_per_trade,
                      atr_stop_fraction=self.atr_stop_fraction,
                      max_qty=self.max_qty,
                      be_r_multiple=self.be_r_multiple,
                      be_buffer_ticks=self.be_buffer_ticks,
                      be_use_nominal_r=True, adjust_stop_to_risk=False,
                      t_win_start=T_WIN_START, t_win_end=T_WIN_END),
            env=self, cmds=self, audit=True)

    # ---------------- 生命周期 ----------------
    def on_start(self):
        self.subscribe_bars(self.bar_type)
        if self.atr_map is None:
            self._refresh_atr(datetime.now(ET).date(), "启动")
            self._arm_atr_update()
        self._log(f"[启动] 已订阅 {self.bar_type} | 合约 {self.instrument_id} "
                  f"({SYMBOL}, ${MULTIPLIER:g}/点) | 风险 {self.risk_per_trade:.1%}/笔, "
                  f"上限 {self.max_qty} 手 | 止损 {self.atr_stop_fraction:.1%}×{ATR_PERIOD}日ATR"
                  f"(NDX 表) | "
                  f"{self.be_r_multiple:g}R 拉保本 | EOD 闹钟 {EOD_TIMER_PAD} | "
                  f"{'DRY_RUN 只记信号' if self.dry_run else '!!! 真实下单模式 !!!'}")
        self._log(f"[启动] 滑点落盘 {SLIP_CSV} | 运行记录 "
                  f"{os.path.basename(_log_path) if _log_path else 'logs/live_*.log'}")

    # ---------------- ATR: NDX 外源磁盘表 (不再向 IB 请求日线) ----------------
    def _refresh_atr(self, as_of, reason: str) -> None:
        """as_of 当日 ATR ← 磁盘表重算 (研判②分层降级):
        表陈旧 >5 日历日/缺失 → 先补拉一轮; 仍陈旧 ≤5 日 → ⚠️ 用旧值照常交易;
        缺失/行数不足 → atr=None → FSM 当日不开仓。"""
        df = load_table()
        stale, last = table_freshness(df, as_of)
        if stale > ATR_STALE_DAYS:
            self._log(f"[ATR] 表{'缺失' if df is None else f'陈旧 {stale} 天 (最新 {last})'}"
                      f" → 补拉一轮")
            res = update_table(log=self._log)
            if not res["ok"]:
                self._atr_alert(f"ATR 表补拉失败: {res['error']}")
            df = load_table()
            stale, last = table_freshness(df, as_of)
        self._atr_px = atr_from_table(df, as_of)
        if self._atr_px is None:
            self._atr_alert("ATR 不可用 (表缺失或行数不足) → 当日不开仓")
            return
        note = (f" ⚠️ 表陈旧 {stale} 天 (最新 {last:%Y-%m-%d}), 按研判用旧值照常交易"
                " —— 请检查收盘更新通道" if stale > ATR_STALE_DAYS else "")
        self._log(f"[ATR] {reason}: {ATR_PERIOD}日ATR = {self._atr_px:.2f} pt "
                  f"(表最新 {last:%Y-%m-%d}) → 止损距离 {self.atr_stop_fraction:.1%}×ATR = "
                  f"{tick_round(self._atr_px * self.atr_stop_fraction):.2f} pt{note}")

    def _arm_atr_update(self) -> None:
        """收盘后更新闹钟 (研判: 盘后更新为主, 启动校验为辅):
        今日 17:10 未过则今日, 否则明日; 触发后在本回调内续订。"""
        now = datetime.now(ET)
        at = datetime.combine(now.date(), ATR_UPDATE_AT, tzinfo=ET)
        if at <= now:
            at += timedelta(days=1)
        try:
            self.clock.cancel_timer(ATR_UPDATE_TIMER)
        except Exception:
            pass
        self.clock.set_time_alert(ATR_UPDATE_TIMER, at.astimezone(timezone.utc),
                                  callback=self._on_atr_update_timer, override=True)
        self.timers_armed.append(("atr_update", int(at.timestamp() * 1e9)))
        self._log(f"[ATR] 收盘更新闹钟 {at:%m-%d %H:%M} ET "
                  f"(失败重试 ≤{ATR_UPDATE_RETRIES - 1} 次 / 间隔 "
                  f"{ATR_UPDATE_RETRY_PAD.total_seconds() / 60:.0f}min)")

    def _rearm_atr_after(self, delay: timedelta) -> None:
        at = datetime.now(ET) + delay
        try:
            self.clock.cancel_timer(ATR_UPDATE_TIMER)
        except Exception:
            pass
        self.clock.set_time_alert(ATR_UPDATE_TIMER, at.astimezone(timezone.utc),
                                  callback=self._on_atr_update_timer, override=True)
        self.timers_armed.append(("atr_retry", int(at.timestamp() * 1e9)))

    def _on_atr_update_timer(self, event) -> None:
        res = update_table(log=self._log)
        if res["ok"]:
            self._atr_upd_attempts = 0
            warn = f" | ⚠️ {'; '.join(res['warn'])}" if res["warn"] else ""
            self._log(f"[ATR更新] 收盘更新完成 (源 {res['source']}, "
                      f"+{res['added']} 行){warn}")
            self._arm_atr_update()
            return
        self._atr_upd_attempts += 1
        if self._atr_upd_attempts < ATR_UPDATE_RETRIES:
            self._log(f"[ATR更新] 失败: {res['error']} "
                      f"(第 {self._atr_upd_attempts}/{ATR_UPDATE_RETRIES} 次) → 重试",
                      level="warning")
            self._rearm_atr_after(ATR_UPDATE_RETRY_PAD)
        else:
            self._atr_alert(f"收盘 ATR 更新连续 {ATR_UPDATE_RETRIES} 次失败: "
                            f"{res['error']} —— 明早开盘前请手动跑 atr_source.py")
            self._atr_upd_attempts = 0
            self._arm_atr_update()               # 明日照常再试, 告警已发出

    def _atr_alert(self, msg: str) -> None:
        """及时提醒: error 进日志 (logs/live_*.log 可 grep) + 桌面通知 (darwin)。"""
        self._log(f"[ATR告警] {msg}", level="error")
        notify_desktop("ORB live · ATR", msg)

    def on_stop(self):
        try:                                    # P1-1: 防残留回调
            self.clock.cancel_timer(EOD_TIMER_NAME)
        except Exception:
            pass
        f = self.fsm
        self._log(f"[停止] 当日统计: 信号 {f.n_signals} | 入场 {f.n_entries} | "
                  f"拉保本 {f.n_be_moves} | 止损出场 {f.n_stopped} | "
                  f"保本出场 {f.n_be_exits} | 收盘平仓 {f.n_eod} | "
                  f"防护触发: 残留强平 {f.n_overnight_flattens} / 止损重挂 "
                  f"{f.n_stop_replaces} / 闹钟平仓 {f.n_timer_flattens} / 迟到成交 "
                  f"{f.n_late_entry_flattens}")
        self._log("[停止] 滑点汇总:\n" + SlippageTracker.summarize(SLIP_CSV))

    # ---------------- 工具 ----------------
    def _now_et(self, ns: int) -> datetime:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).astimezone(ET)

    def _log(self, msg: str, level: str = "info"):
        getattr(self.log, level)(msg)
        flog(msg)

    def _net_pos(self) -> int:
        return int(self.portfolio.net_position(self.instrument_id) or 0)

    # ---------------- FsmEnv 实现 (live 语义) ----------------
    def equity(self) -> float:
        eq = self.portfolio.equity(venue=self.instrument_id.venue)
        return eq[USD].as_double()

    def atr_for(self, d):
        if self.atr_map is not None:
            return self.atr_map.get(d)
        if self._atr_px is None and ATR_OVERRIDE_PTS:
            return ATR_OVERRIDE_PTS
        return self._atr_px

    def range_for(self, d):
        if self._rng_hi is None or self._rng_lo is None:
            return None
        return (self._rng_hi, self._rng_lo)

    def be_ok(self, d, t) -> bool:
        return t < flat_at_for(d)

    def flatten_now(self, d, t) -> bool:
        return t >= flat_at_for(d)

    def net_position(self) -> int:
        return self._net_pos()

    def on_new_day(self, d) -> None:
        """换日: 重置盘前区间 (否则跨日累积会越滚越宽, 信号越来越少) + 从表刷新 ATR
        (昨晚收盘更新已入库 → 换日重算即含昨日; 昨晚失败则走 _refresh_atr 的分层降级)。"""
        self._rng_hi = self._rng_lo = None
        if self.atr_map is None:
            self._refresh_atr(d, f"换日 {d}")
        self._log(f"—— 新交易日 {d} ——")

    # ---------------- FsmCommands 实现 ----------------
    def submit_entry_market(self, side: str, qty: int, ref: str) -> None:
        px_sig = self._pending_sig_px           # FSM 在调用前记录的信号价 (见 on_bar)
        self.slip.note_signal(ref, kind="entry", side=side,
                              qty=float(qty), signal_px=px_sig,
                              signal_ts_ns=self.clock.timestamp_ns(),
                              bar_ts_ns=self._pending_bar_ts)
        self._log(f"[信号] {side} @ {px_sig:.2f} {qty} 手  "
                  f"(区间 {self._rng_lo:.2f} ~ {self._rng_hi:.2f} | "
                  f"止损距离 {self.fsm.r_pts:.2f}pt ≈ 每手风险 "
                  f"${self.fsm.r_pts * self.multiplier:.0f})")
        if self.dry_run:
            self.slip.note_skipped(ref, note=f"DRY_RUN: 信号已记录 (qty={qty}, "
                                             f"stop_dist={self.fsm.r_pts}pt), 未下单")
            return
        order = self.order_factory.market(
            instrument_id=self.instrument_id, order_side=OrderSide[side],
            quantity=Quantity(qty, 0))
        self._order_role[str(order.client_order_id)] = ("entry", ref)
        self.submit_order(order)

    def place_stop_market(self, exit_side: str, qty: int, trigger: float,
                          ref: str) -> None:
        d = self.fsm.day
        expire = (datetime.combine(d, flat_at_for(d), tzinfo=ET) + STOP_GTD_PAD)
        self.slip.note_signal(ref, kind="stop", side=exit_side, qty=float(qty),
                              signal_px=trigger, trigger_px=trigger,
                              signal_ts_ns=None, bar_ts_ns=self.clock.timestamp_ns())
        sl = self.order_factory.stop_market(
            instrument_id=self.instrument_id, order_side=OrderSide[exit_side],
            quantity=Quantity(qty, 0),
            trigger_price=Price(trigger, 2), reduce_only=True,
            time_in_force=TimeInForce.GTD, expire_time=expire)   # P2-1
        self._stop_order_obj = sl
        self._stop_cid = str(sl.client_order_id)
        self._order_role[self._stop_cid] = ("stop", ref)
        self.submit_order(sl)
        self._log(f"[止损挂单] {exit_side} {qty} 手 @ {trigger:.2f} "
                  f"(入场 {self.fsm.entry_px:.2f}, R = {self.fsm.r_pts:.2f}pt, "
                  f"GTD {expire:%H:%M}, 达 {self.be_r_multiple:g}R 拉保本)")

    def resize_stop(self, qty: int) -> None:
        self.modify_order(self._stop_order_obj, quantity=Quantity(qty, 0))
        self._log(f"止损单数量改为 {qty} 手 (分笔成交只此一张)")

    def modify_stop_trigger(self, trigger: float) -> None:
        self.modify_order(self._stop_order_obj,
                          trigger_price=Price(trigger, 2))

    def cancel_all(self) -> None:
        self.cancel_all_orders(self.instrument_id)

    def flatten_position(self, reason: str, ref: str) -> None:
        pos = self._net_pos()
        if pos == 0:
            return
        side = "SELL" if pos > 0 else "BUY"
        px = self._pending_sig_px or 0.0
        self.slip.note_signal(ref, kind="eod", side=side, qty=float(abs(pos)),
                              signal_px=px, signal_ts_ns=self.clock.timestamp_ns(),
                              bar_ts_ns=self._pending_bar_ts)
        self._log(f"[收盘/{reason}] 平掉 {pos} 手 @ 信号价 {px:.2f}")
        if self.dry_run:
            self.slip.note_skipped(ref, note=f"DRY_RUN: {reason} 平仓信号")
            return
        self.cancel_all_orders(self.instrument_id)
        order = self.order_factory.market(
            instrument_id=self.instrument_id, order_side=OrderSide[side],
            quantity=Quantity(abs(pos), 0))
        self._order_role[str(order.client_order_id)] = ("eod", ref)
        self.submit_order(order)

    def arm_eod_timer(self, d) -> None:
        """P1-1: flat_at + 5min + 2s 闹钟。过去的时刻不再上 (首日晚间 bar 场景)。"""
        alert_dt = datetime.combine(d, flat_at_for(d), tzinfo=ET) + EOD_TIMER_PAD
        if alert_dt.timestamp() <= self.clock.timestamp_ns() / 1e9:
            return
        try:
            self.clock.cancel_timer(EOD_TIMER_NAME)
        except Exception:
            pass
        self.clock.set_time_alert(
            EOD_TIMER_NAME,
            alert_dt.astimezone(timezone.utc),
            callback=self._on_eod_timer,
            override=True)
        self.timers_armed.append((str(d), int(alert_dt.timestamp() * 1e9)))

    def _on_eod_timer(self, event):
        self.fsm.on_eod_timer(event.ts_event)

    def fsm_log(self, msg: str, level: str = "info") -> None:
        self._log(msg, level)

    # ---------------- 行情 ----------------
    def on_bar(self, bar: Bar):
        t = self._now_et(bar.ts_event)
        d, hhmm = t.date(), t.time()
        o, h, l, c = (bar.open.as_double(), bar.high.as_double(),
                      bar.low.as_double(), bar.close.as_double())

        # ⓪ bar 留痕 (对账底稿; 与原版一致 9:00-17:00)
        if time(9, 0) <= hhmm <= time(17, 0):
            self._log(f"[bar] {hhmm:%H:%M} O={o:.2f} H={h:.2f} L={l:.2f} "
                      f"C={c:.2f} V={bar.volume.as_double():.0f}")

        # ① 盘前区间累积 (env 数据源; FSM 只读结果)
        if T_RANGE_START <= hhmm < T_RANGE_END:
            self._rng_hi = h if self._rng_hi is None else max(self._rng_hi, h)
            self._rng_lo = l if self._rng_lo is None else min(self._rng_lo, l)
            self._log(f"→ 区间 {self._rng_lo:.2f}~{self._rng_hi:.2f}")

        # 窗口内区间仍为空 → 警示 (原版行为)
        if (T_WIN_START <= hhmm < T_WIN_END and not self.fsm.entered_today
                and self._rng_hi is None):
            self._log("区间为空 (没收到盘前 bar?) —— 无法判突破", level="warning")

        # 持仓期进度日志 (原版 _log_position)
        if self.fsm.state == HOLD and self.be_ok(d, hhmm) and self.fsm.r_pts:
            f = self.fsm
            cur_r = ((c - f.entry_px) if f.entry_side == "BUY"
                     else (f.entry_px - c)) / f.r_pts
            state = "已拉保本" if f.stop_moved else f"{self.be_r_multiple:g}R 拉保本"
            self._log(f"[持仓] 浮盈 {cur_r:+.2f}R / 峰值 {f.peak_r:.2f}R "
                      f"(止损 {f.stop_trigger:.2f}, {state})")

        # ②③④ 信号/BE/收盘 全部交给 FSM
        self._pending_sig_px = c
        self._pending_bar_ts = bar.ts_event
        self.fsm.on_bar(bar.ts_event, d, hhmm, o, h, l, c)
        self._last_bar_date = d

    # ---------------- 成交回报 → 滑点落盘 + FSM ----------------
    def on_order_filled(self, event):
        cid = str(event.client_order_id)
        role, ref = self._order_role.get(cid, (None, None))
        px = event.last_px.as_double()
        q = int(event.last_qty.as_double())

        if role == "entry":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q,
                                      fill_ts_ns=event.ts_event, order_type="MARKET")
            if row:
                self._log(f"[入场成交] {px:.2f} × {q} 手 (累计 {self.fsm.filled_qty + q}) | "
                          f"滑点 {row['slip_ticks']} tick (${row['slip_usd']}) | "
                          f"信号→成交 {row['latency_ms']} ms"
                          f"{'  [STALE 样本]' if row['stale'] else ''}")
            self.fsm.on_entry_fill(px, q, event.ts_event)

        elif role == "stop":
            trig = self.fsm.stop_trigger or px     # 防御: 状态已清后的迟到分笔
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q,
                                      fill_ts_ns=event.ts_event,
                                      order_type="STOP_MARKET",
                                      trigger_px=trig)
            if row:
                self._log(f"[止损成交] {px:.2f} (触发 {trig:.2f}) | "
                          f"滑点 {row['slip_ticks']} tick (${row['slip_usd']})")
            self.fsm.on_stop_fill(px, q, event.ts_event)
            self._stop_cid = None

        elif role == "eod":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q,
                                      fill_ts_ns=event.ts_event, order_type="MARKET")
            if row:
                self._log(f"[收盘成交] {px:.2f} | 滑点 {row['slip_ticks']} tick")

    def on_order_canceled(self, event):
        self._route_stop_death(event, "被撤")

    def on_order_rejected(self, event):
        self._route_stop_death(event, "被拒")

    def on_order_expired(self, event):
        self._route_stop_death(event, "过期")

    def _route_stop_death(self, event, why: str):
        cid = str(event.client_order_id)
        if cid == self._stop_cid:
            self._stop_cid = None
            self.fsm.on_stop_dead(event.ts_event, why)


# ===========================================================================
# 节点配置 (与原版一致)
# ===========================================================================
def build_node() -> TradingNode:
    instrument_provider = InteractiveBrokersInstrumentProviderConfig(
        load_contracts=frozenset({NQ_CONTRACT}),
    )
    data_config = InteractiveBrokersDataClientConfig(
        ibg_host=IB_HOST, ibg_port=IB_PORT, ibg_client_id=CLIENT_ID,
        instrument_provider=instrument_provider, routing=RoutingConfig(default=True),
        market_data_type=MARKET_DATA_TYPE,
        use_regular_trading_hours=False,       # 盘前 9:00-9:29 bar 必须收得到
    )
    exec_config = InteractiveBrokersExecClientConfig(
        ibg_host=IB_HOST, ibg_port=IB_PORT, ibg_client_id=CLIENT_ID,
        account_id=ACCOUNT_ID, instrument_provider=instrument_provider,
        routing=RoutingConfig(default=True),
    )
    node_config = TradingNodeConfig(
        trader_id="ORB-LIVE-V50",
        data_clients={"IB": data_config},
        exec_clients={"IB": exec_config},
        logging=LoggingConfig(log_level="INFO"),
    )
    node = TradingNode(config=node_config)
    node.add_data_client_factory("IB", InteractiveBrokersLiveDataClientFactory)
    node.add_exec_client_factory("IB", InteractiveBrokersLiveExecClientFactory)
    return node


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    node = build_node()
    node.build()

    strategy = OrbFsmLiveStrategy(OrbFsmLiveConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE_STR,
        risk_per_trade=RISK_PER_TRADE,
        atr_stop_fraction=ATR_STOP_FRACTION,
        be_r_multiple=BE_R_MULTIPLE,
        be_buffer_ticks=BE_BUFFER_TICKS,
        max_qty=MAX_QTY,
        multiplier=MULTIPLIER,
        dry_run=DRY_RUN,
    ))
    node.trader.add_strategy(strategy)

    flog(f"[启动] TradingNode → IB Gateway {IB_HOST}:{IB_PORT} ({ACCOUNT_ID}) | "
         f"FSM 候选版 (P1-1/P1-2/P2-1 已实现)")
    try:
        node.run()
    finally:
        node.dispose()
        flog("[退出] " + SlippageTracker.summarize(SLIP_CSV))
