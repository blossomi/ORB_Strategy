# -*- coding: utf-8 -*-
"""
live_ib_demo.py
===============
NautilusTrader 实盘最小框架：连 IB Gateway (paper) → 订阅 MNQ 5 分钟 bar
→ 跑 ORB 信号 → 按推荐参数下单 → 记录「信号价 vs 实际成交价」的真实滑点。

参数 = 2026-09-12 三参数寻优结论（GLM_working/results/REPORT.md，MNQ/$25k/1tick 口径验证过）:
  止损 7.5% × 前一日 14日ATR (Wilder)  |  仓位 每笔风险 0.7% 权益 (以损定仓)  |  保本 浮盈达 5R 拉到入场价

目标（按顺序验证，别跳步）
--------------------------
1. 连接 → 合约加载 → 订阅 → 收 bar        （已验证过）
2. DRY_RUN=True：只记录信号, 不下单       ← 建议先跑几天, 核对信号时点/手数/止损价对不对
3. DRY_RUN=False：真下单(paper), 累积真实滑点样本 → 用 summarize() 校准回测的 SLIPPAGE_TICKS

⚠️ 关键前提：**必须先有实时 streaming 行情订阅**
   现在账户是 DELAYED_FROZEN(延迟数据), 收到的 bar 比真实市场晚 10-15 分钟。
   在这种状态下测滑点是**无效**的 —— 你算出的"滑点"里混着十几分钟的价格漂移。
   SlippageTracker 会自动把 latency 超标的样本标 stale=True 并在汇总时剔除,
   但这只能防止误读, 不能替代实时行情。

用法
----
  cd live && ../.venv/bin/python live_ib_demo.py                  # 默认 DRY_RUN
  收工后看汇总:
  cd live && ../.venv/bin/python -c "from slippage_tracker import SlippageTracker as T; print(T.summarize('live_slippage.csv'))"
  上线前回归验证(改完策略逻辑必跑):
  cd live && ../.venv/bin/python _verify_live_logic.py            # 依次跑 A/B/C

运行记录
--------
  每次启动在 logs/live_<启动时刻>.log 落一份完整事件流 (9:00 起每根 5min bar 的 OHLCV、
  区间更新、突破、信号、成交、拉保本、收盘平仓、日结计数), 墙钟时间戳前缀 —— 事后核对
  信号时点与 bar 推送节奏全靠它。每次运行一个新文件, 从不覆盖。
  控制台输出(含 nautilus/adapter 报错)用 nohup 重定向到 logs/nohup_<日期>.log。
  完整操作流程见同目录 OPERATIONS.md (操作手册)。

前置
----
  本机 IB Gateway 已登录 paper 账户, API 端口 4002 开放, API 类型选 IB API(非 FIX/CTCI)。
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
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.trading.strategy import Strategy

from slippage_tracker import SlippageTracker

# ===========================================================================
# ★ 配置区 —— 按需修改
# ===========================================================================
IB_HOST = "127.0.0.1"
IB_PORT = 4002                  # IB Gateway paper 端口 (live 用 4001; TWS 是 7497/7496)
ACCOUNT_ID = "DUQ715008"        # paper 账户 ID
CLIENT_ID = 1

SYMBOL = "MNQ"                  # 推荐 MNQ ($2/点): 小账户颗粒度, 参数结论与 NQ 一致 (REPORT.md 附2)
# ⚠️ 必须始终用**当月主力合约**: NQ 到期日 = 到期月第三个周五, 成交量通常提前 ~1 周
#    滚入下季合约(3/6/9/12 月循环)。用旧合约=成交稀薄, 滑点样本全废。
#    下次换月: 2026-12-10 前后滚入 202703 (H7); 2027-03-11 前后滚入 202706 (M7)。
CONTRACT_MONTH = "202612"       # IB 用 YYYYMM
LOCAL_SYMBOL = "Z6"             # 2026-12 → Z6 (月份码 F G H J K M N Q U V X Z)
TICK = 0.25                     # NQ/MNQ 最小变动 = 0.25 pt
MULTIPLIER = 2.0 if SYMBOL == "MNQ" else 20.0

DRY_RUN = True                  # True: 只记信号不下单 (先跑这个!)

# ---- 策略参数 (2026-09-12 推荐: 7.5%ATR × 5R保本 × 0.7%风险) ----
RISK_PER_TRADE = 0.007          # 每笔风险 = 权益的 0.7% (以损定仓, floor 取整)
ATR_PERIOD = 14                 # ATR 周期 (Wilder)
ATR_STOP_FRACTION = 0.075       # 止损 = 7.5% × 前一日 14日ATR
BE_R_MULTIPLE = 5.0             # 浮盈达 5R → 止损拉到保本, 之后持有到收盘
BE_BUFFER_TICKS = 0             # 保本缓冲 (tick), 0 = 止损正好在入场价 (与回测口径一致)
MAX_QTY = 50                    # 单笔手数安全帽。0.7%×paper 权益($106万)会算出 >100 手,
                                # 实盘验证期先用 50 手 MNQ (≈10 手 NQ) 保守跑
ATR_LOOKBACK_DAYS = 120         # 启动时拉多少个自然日的日线算 ATR (~82 个交易日, Wilder 种子权重 <0.3%)
ATR_OVERRIDE_PTS = None         # 手动兜底 (pt)。日线请求失败时才用; None = 失败则当日不交易

# 行情类型: 订阅实时 CME 数据包之后**必须**改成 REALTIME ——
# 否则 adapter 仍按延迟数据发请求, 订阅白买(且延迟数据不能用于下单/测滑点)。
MARKET_DATA_TYPE = MarketDataTypeEnum.REALTIME
# MARKET_DATA_TYPE = MarketDataTypeEnum.DELAYED_FROZEN   # 未订阅时只能退回这个

# ORB 时间参数 (对齐回测 v8.4)
T_RANGE_START = time(9, 0)      # 盘前区间开始
T_RANGE_END = time(9, 29)       # 盘前区间结束
T_WIN_START = time(9, 30)       # 入场窗口开始
T_WIN_END = time(10, 10)        # 入场窗口结束 (无突破则当日放弃)
T_FLAT = time(15, 55)           # 常规日收盘清仓

# ⚠️ 半日市(提前 13:00 收盘): 这些日期根本没有 15:55 的 bar, 用 T_FLAT 会导致
#    **持仓整夜不平**。回测脚本靠"当日实际最后一根 bar"解决, 实盘只能预先列日期。
#    每年更新一次 (CME 惯例: 感恩节次日 / 圣诞前夕 / 独立日前夕)。
#    2026 年剩余: 11-27(感恩节次日)、12-24(圣诞前夕)。已过: 07-03。
HALF_DAY_FLAT = time(12, 50)
HALF_DAYS = {"2026-11-27", "2026-12-24"}

SLIP_CSV = "live_slippage.csv"  # 滑点落盘文件 (追加写)
SLIP_STALE_MS = 2000            # 信号→成交 超过 2 秒即标 stale (正常 IB 往返 < 500ms)

NQ_CONTRACT = IBContract(
    symbol=SYMBOL, secType="FUT", exchange="CME", currency="USD",
    lastTradeDateOrContractMonth=CONTRACT_MONTH,
)
INSTRUMENT_ID = f"{SYMBOL}{LOCAL_SYMBOL}.CME"            # IB_SIMPLIFIED 编码, venue=CME
BAR_TYPE_STR = f"{INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL"
DAILY_BAR_TYPE_STR = f"{INSTRUMENT_ID}-1-DAY-LAST-EXTERNAL"   # 一次性请求, 算前一日 ATR

ET = ZoneInfo("America/New_York")


def tick_round(px: float) -> float:
    """取整到最小变动价位 0.25 的倍数。"""
    return round(round(px / TICK) * TICK, 2)


# ---------------- 运行日志: logs/live_<启动时刻>.log ----------------
# 路径锚定脚本所在目录 (不依赖 cwd), 保证从任何位置启动都落在 live/logs/。
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
_log_path: str | None = None


def flog(msg: str) -> str:
    """追加一行运行记录 (ET 墙钟时间前缀) 并回显 stdout; 返回日志路径。

    与 nautilus 的 self.log 双轨: self.log 只走控制台 (靠 nohup 重定向留底),
    flog 落 logs/ 永久文件。每次**进程**启动一个新文件, 从不覆盖 —— 所有运行
    记录都保留。墙钟前缀让「bar 推送是否踩点」可事后审计 (5min 边界后几秒内)。
    """
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
# 策略: ORB 信号 + 滑点记录
# ===========================================================================
class OrbLiveConfig(StrategyConfig):
    # 全部必填 —— 漏传直接报错, 不留与配置区不一致的默认值 (对齐 6f64902 的教训)
    instrument_id: str
    bar_type: str
    risk_per_trade: float
    atr_stop_fraction: float
    be_r_multiple: float
    be_buffer_ticks: int
    max_qty: int
    multiplier: float
    dry_run: bool


class OrbLiveStrategy(Strategy):
    def __init__(self, config: OrbLiveConfig, atr_map: dict | None = None):
        """atr_map: {date: ATR点数} —— 回测/回归验证时注入 (与实盘日线请求二选一)。"""
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

        self.slip = SlippageTracker(SLIP_CSV, tick=TICK, multiplier=MULTIPLIER,
                                    stale_ms=SLIP_STALE_MS)
        self._order_role: dict[str, tuple[str, str]] = {}       # client_order_id -> (角色, ref)

        self._day = None
        self._rng_hi: float | None = None
        self._rng_lo: float | None = None
        self._entered_today = False
        self._entry_side: OrderSide | None = None
        self._entry_px: float | None = None    # 首笔成交价 (R/保本 的锚)
        self._r_pts: float | None = None       # 止损距离 (pt, 信号时按 ATR 定)
        self._filled_qty = 0
        self._stop_order = None
        self._stop_trigger: float | None = None
        self._stop_moved = False               # 已拉保本
        self._atr_px: float | None = None      # 实盘模式: 最近一次算出的前一日 ATR
        self._peak_r = 0.0                     # 持仓期间浮盈峰值 (R 倍数, 记录用)

        # 统计 (收盘/止损回报时打印, 便于对账; 换日时清零)
        self.n_signals = 0
        self.n_entries = 0
        self.n_be_moves = 0
        self.n_stopped = 0
        self.n_be_exits = 0
        self.n_eod = 0

    # ---------------- 生命周期 ----------------
    def on_start(self):
        self.subscribe_bars(self.bar_type)
        if self.atr_map is None:
            # 实盘: 一次性拉日线算前一日 ATR。注意这是**非流式**请求(不带 keepUpToDate),
            # 与被 2188 挡的实时历史 bar 流是两回事 (notebook 2026-09-03 结论)。
            self._request_atr()
        self._log(
            f"[启动] 已订阅 {self.bar_type} | 合约 {self.instrument_id} "
            f"({SYMBOL}, ${MULTIPLIER:g}/点) | 风险 {self.risk_per_trade:.1%}/笔, "
            f"上限 {self.max_qty} 手 | 止损 {self.atr_stop_fraction:.1%}×{ATR_PERIOD}日ATR | "
            f"{self.be_r_multiple:g}R 拉保本 | "
            f"{'DRY_RUN 只记信号' if self.dry_run else '!!! 真实下单模式 !!!'}")
        self._log(f"[启动] 滑点落盘 {SLIP_CSV} | 运行记录 "
                  f"{os.path.basename(_log_path) if _log_path else 'logs/live_*.log'} "
                  f"(logs/ 下, 每次运行一个新文件)")

    def _request_atr(self):
        start = datetime.now(ET) - timedelta(days=ATR_LOOKBACK_DAYS)
        self.request_bars(BarType.from_str(DAILY_BAR_TYPE_STR), start)
        self._log(f"已请求日线 (近 {ATR_LOOKBACK_DAYS} 天) 以计算前一日 {ATR_PERIOD}日ATR")

    def on_historical_data(self, data):
        if self.atr_map is not None or data is None:
            return
        rows = []
        today = datetime.now(ET).date()
        for b in data:
            d = datetime.fromtimestamp(b.ts_event / 1e9, tz=timezone.utc).astimezone(ET).date()
            if d < today:                       # 只用已走完的交易日 (当日 bar 是半根)
                rows.append((d, b.high.as_double(), b.low.as_double(), b.close.as_double()))
        rows.sort()
        if len(rows) < ATR_PERIOD + 1:
            self._log(f"日线只有 {len(rows)} 根 (<{ATR_PERIOD + 1}), 无法算 ATR", level="error")
            return
        trs = []
        for i in range(1, len(rows)):
            _, h, l, pc = rows[i][0], rows[i][1], rows[i][2], rows[i - 1][3]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = trs[0]                            # Wilder 递推 (= ewm alpha=1/N, adjust=False)
        for tr in trs[1:]:
            atr = (atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        self._atr_px = atr
        self._log(f"[ATR] 前一日 {ATR_PERIOD}日ATR = {atr:.2f} pt → "
                  f"止损距离 {self.atr_stop_fraction:.1%}×ATR = "
                  f"{tick_round(atr * self.atr_stop_fraction):.2f} pt")

    def _atr_for(self, d) -> float | None:
        if self.atr_map is not None:
            return self.atr_map.get(d)
        return self._atr_px

    def on_stop(self):
        self._log(f"[停止] 当日统计: 信号 {self.n_signals} | 入场 {self.n_entries} | "
                  f"拉保本 {self.n_be_moves} | 止损出场 {self.n_stopped} | "
                  f"保本出场 {self.n_be_exits} | 收盘平仓 {self.n_eod}")
        self._log("[停止] 滑点汇总:\n" + SlippageTracker.summarize(SLIP_CSV))

    # ---------------- 工具 ----------------
    def _now_et(self, ns: int) -> datetime:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).astimezone(ET)

    def _log(self, msg: str, level: str = "info"):
        """双轨日志: nautilus 控制台 (nohup 留底) + logs/ 永久文件 (见 flog)。"""
        getattr(self.log, level)(msg)
        flog(msg)

    # ---------------- 行情 ----------------
    def on_bar(self, bar: Bar):
        t = self._now_et(bar.ts_event)
        d, hhmm = t.date(), t.time()

        flat_at = HALF_DAY_FLAT if str(d) in HALF_DAYS else T_FLAT

        if d != self._day:                       # 新交易日: 重置区间与状态
            if self._day is not None:
                # 清掉昨日残留挂单(例如止损一直没触发、或收盘平仓失败留下的单)
                self.cancel_all_orders(self.instrument_id)
                self._log(f"[日结] {self._day} 信号 {self.n_signals} | 入场 {self.n_entries} | "
                          f"拉保本 {self.n_be_moves} | 止损出场 {self.n_stopped} | "
                          f"保本出场 {self.n_be_exits} | 收盘平仓 {self.n_eod}")
                self.n_signals = self.n_entries = self.n_be_moves = 0
                self.n_stopped = self.n_be_exits = self.n_eod = 0
            self._day = d
            self._rng_hi = self._rng_lo = None
            self._entered_today = False
            self._entry_side = None
            self._entry_px = None
            self._r_pts = None
            self._filled_qty = 0
            self._stop_order = None
            self._stop_moved = False
            self._peak_r = 0.0
            if self.atr_map is None:            # 实盘: 每天开盘前刷新 ATR
                self._request_atr()
            self._log(f"—— 新交易日 {d} ——")

        # ⓪ 9:00~17:00 的每根 5min bar 都留痕 (OHLCV) —— 事后对账的原始底稿。
        #    墙钟前缀 vs bar 时间的差 = bar 推送延迟, 可审计是否踩点 (5min 边界后几秒内)。
        in_range = T_RANGE_START <= hhmm < T_RANGE_END
        bar_desc = None
        if time(9, 0) <= hhmm <= time(17, 0):
            bar_desc = (f"[bar] {hhmm:%H:%M} O={bar.open.as_double():.2f} "
                        f"H={bar.high.as_double():.2f} L={bar.low.as_double():.2f} "
                        f"C={bar.close.as_double():.2f} V={bar.volume.as_double():.0f}")

        # ① 累积盘前区间 (需要 9:00 之前的 bar; 若订阅的是 RTH-only 数据类型, 这里会一直是 None)
        if in_range:
            hi, lo = bar.high.as_double(), bar.low.as_double()
            self._rng_hi = hi if self._rng_hi is None else max(self._rng_hi, hi)
            self._rng_lo = lo if self._rng_lo is None else min(self._rng_lo, lo)
            if bar_desc:
                self._log(bar_desc + f" → 区间 {self._rng_lo:.2f}~{self._rng_hi:.2f}")
            return
        if bar_desc:
            self._log(bar_desc)

        # ② 入场窗口内: 逐根 bar 用收盘价判突破
        if T_WIN_START <= hhmm < T_WIN_END and not self._entered_today:
            if self._rng_hi is None:
                self._log("区间为空 (没收到盘前 bar?) —— 无法判突破", level="warning")
            else:
                c = bar.close.as_double()
                if c > self._rng_hi or c < self._rng_lo:
                    edge = self._rng_hi if c > self._rng_hi else self._rng_lo
                    arrow = "↑" if c > self._rng_hi else "↓"
                    self._log(f"[突破] {hhmm:%H:%M} C={c:.2f} {arrow} 破 "
                              f"{'上沿' if c > self._rng_hi else '下沿'} "
                              f"{abs(c - edge):.2f}pt (区间 {self._rng_lo:.2f}~{self._rng_hi:.2f})")
                    side = OrderSide.BUY if c > self._rng_hi else OrderSide.SELL
                    self._on_signal(side, bar, c, d)
                else:
                    self._log(f"[窗口] {hhmm:%H:%M} C={c:.2f} 未破区间 "
                              f"{self._rng_lo:.2f}~{self._rng_hi:.2f}")

        # ③ 持仓中: 记浮盈进度 + 浮盈达 N R 拉保本 (在收盘清仓之前)
        #    注: 回测引擎里市价单在 submit 内同步成交, 入场当根 bar 即可检查;
        #    实盘成交是异步的, 首次检查从下一根 bar 开始 (与回测的细微差异, 可接受)。
        if hhmm < flat_at and self._stop_order is not None:
            self._log_position(bar)
            self._check_be(bar)

        # ④ 收盘清仓 (半日市提前到 HALF_DAY_FLAT, 否则当天没有 15:55 的 bar → 整夜不平)
        if hhmm >= flat_at:
            self._flatten(bar)

    def _log_position(self, bar: Bar):
        """持仓期间每根 bar 记一行浮盈进度 (R 倍数/峰值) —— 事后核对 BE 触发时点用。"""
        if self._entry_px is None or not self._r_pts:
            return
        c = bar.close.as_double()
        if self._entry_side == OrderSide.BUY:
            cur_r = (c - self._entry_px) / self._r_pts
            self._peak_r = max(self._peak_r,
                               (bar.high.as_double() - self._entry_px) / self._r_pts)
        else:
            cur_r = (self._entry_px - c) / self._r_pts
            self._peak_r = max(self._peak_r,
                               (self._entry_px - bar.low.as_double()) / self._r_pts)
        state = "已拉保本" if self._stop_moved else f"{self.be_r_multiple:g}R 拉保本"
        self._log(f"[持仓] 浮盈 {cur_r:+.2f}R / 峰值 {self._peak_r:.2f}R "
                  f"(止损 {self._stop_trigger:.2f}, {state})")

    # ---------------- 信号 / 下单 ----------------
    def _on_signal(self, side: OrderSide, bar: Bar, px: float, d):
        atr = self._atr_for(d)
        if atr is None or atr <= 0:
            if ATR_OVERRIDE_PTS:
                atr = ATR_OVERRIDE_PTS
                self._log(f"ATR 不可用, 用手动兜底 {atr} pt", level="warning")
            else:
                self._log("前一日 ATR 不可用 —— 无法定止损, 当日不交易", level="error")
                return
        stop_dist = max(TICK, tick_round(atr * self.atr_stop_fraction))

        # 以损定仓: floor(权益 × risk% / (止损pt × 每点$)), 上限 MAX_QTY
        eq = self.portfolio.equity(venue=self.instrument_id.venue)[USD].as_double()
        risk_qty = eq * self.risk_per_trade / (stop_dist * self.multiplier)
        qty = int(floor(min(risk_qty, self.max_qty)))
        if qty < 1:
            self._log(f"买不起 1 手 (权益 ${eq:,.0f} × {self.risk_per_trade:.1%} = "
                      f"${eq * self.risk_per_trade:,.0f} < 每手风险 "
                      f"${stop_dist * self.multiplier:,.0f}) —— 当日放弃", level="error")
            return

        self._entered_today = True
        self._r_pts = stop_dist
        self.n_signals += 1
        ref = f"entry-{bar.ts_event}"
        # ① 先登记信号价 —— 必须在下单之前, 否则下单耗时会漏进滑点。
        #    signal_ts 用 clock 的墙钟时间, 不是 bar.ts_event: IB adapter 给的是 bar 的
        #    **开始**时间, 而 bar 是在结束时才推送, 用 bar.ts_event 会让每笔 latency
        #    虚增一整根 bar(5 分钟) → 全部样本被标 stale, 采集作废。
        self.slip.note_signal(ref, kind="entry", side=side.name,
                              qty=float(qty), signal_px=px,
                              signal_ts_ns=self.clock.timestamp_ns(),
                              bar_ts_ns=bar.ts_event)
        self._log(f"[信号] {side.name} @ {px:.2f} {qty} 手  "
                  f"(区间 {self._rng_lo:.2f} ~ {self._rng_hi:.2f} | "
                  f"ATR {atr:.2f} → 止损距离 {stop_dist:.2f}pt ≈ 每手风险 "
                  f"${stop_dist * self.multiplier:.0f})")

        if self.dry_run:
            self.slip.note_skipped(ref, note=f"DRY_RUN: 信号已记录 (qty={qty}, "
                                             f"stop_dist={stop_dist}pt), 未下单")
            return

        self._entry_side = side
        order = self.order_factory.market(
            instrument_id=self.instrument_id, order_side=side,
            quantity=Quantity.from_str(str(qty)))
        self._order_role[str(order.client_order_id)] = ("entry", ref)
        self.submit_order(order)

    def _place_stop(self, fill_px: float, fill_ts_ns: int):
        """入场成交后挂/调止损。止损单的滑点 = 触发价 vs 实际成交价。

        ⚠️ 分笔成交时**绝不能重复 submit** —— 否则市场里会同时存在两张止损单,
        触发时把手数平两次(实盘直接被达成反向)。已有挂单就改数量。
        """
        if self._entry_px is None:
            self._entry_px = fill_px        # 首笔成交价 = R/保本 的锚
            self.n_entries += 1
        side = OrderSide.SELL if self._entry_side == OrderSide.BUY else OrderSide.BUY
        trig = fill_px - self._r_pts if side == OrderSide.SELL else fill_px + self._r_pts
        trig = tick_round(trig)
        self._stop_trigger = trig

        # ⚠️ 必须用 `not is_closed`, 不能用 is_open!
        #    Nautilus 的 is_open 只在 ACCEPTED/TRIGGERED/PARTIALLY_FILLED 等状态为 True,
        #    **不含 INITIALIZED/SUBMITTED**; 而填单回调与 submit_order 可能在同一批消息里,
        #    此时订单还是 SUBMITTED -> is_open=False -> 判断失效 -> 又挂一张止损单(实测
        #    31 笔入场挂了 62 张)。is_closed 只对 DENIED/REJECTED/CANCELED/EXPIRED/FILLED 为 True。
        if self._stop_order is not None and not self._stop_order.is_closed:
            self.modify_order(
                self._stop_order,
                quantity=Quantity.from_str(str(self._filled_qty)))
            self._log(f"止损单数量改为 {self._filled_qty} 手 (分笔成交只此一张)")
            return

        ref = f"stop-{fill_ts_ns}"
        # signal_ts 留空: 止损的触发时刻我们拿不到, latency 对止损无意义, 不参与 staleness 判定
        self.slip.note_signal(ref, kind="stop", side=side.name, qty=float(self._filled_qty),
                              signal_px=trig, trigger_px=trig, signal_ts_ns=None,
                              bar_ts_ns=fill_ts_ns)
        self._stop_order = self.order_factory.stop_market(
            instrument_id=self.instrument_id, order_side=side,
            quantity=Quantity.from_str(str(self._filled_qty)),
            trigger_price=Price.from_str(f"{trig:.2f}"), reduce_only=True)
        self._order_role[str(self._stop_order.client_order_id)] = ("stop", ref)
        self.submit_order(self._stop_order)
        self._log(f"[止损挂单] {side.name} {self._filled_qty} 手 @ {trig:.2f} "
                  f"(入场 {self._entry_px:.2f}, R = {self._r_pts:.2f}pt, "
                  f"达 {self.be_r_multiple:g}R 拉保本)")

    def _check_be(self, bar: Bar):
        """浮盈达 N R (触及判断) → 止损拉到保本+缓冲, 之后持有到收盘 (非 trailing)。"""
        if self._stop_moved or self._entry_px is None or self._r_pts is None:
            return
        if self._stop_order is None or self._stop_order.is_closed:
            return
        r, entry = self._r_pts, self._entry_px
        if self._entry_side == OrderSide.BUY:
            if bar.high.as_double() >= entry + self.be_r_multiple * r:
                self._move_stop_to_be()
        else:
            if bar.low.as_double() <= entry - self.be_r_multiple * r:
                self._move_stop_to_be()

    def _move_stop_to_be(self):
        buffer = self.be_buffer_ticks * TICK
        be_px = tick_round(self._entry_px + buffer) if self._entry_side == OrderSide.BUY \
            else tick_round(self._entry_px - buffer)
        old_trig = self._stop_trigger
        self.modify_order(self._stop_order, trigger_price=Price.from_str(f"{be_px:.2f}"))
        self._stop_trigger = be_px        # ⚠️ 必须同步: 止损成交时滑点记录用这个当 trigger_px
        self._stop_moved = True
        self.n_be_moves += 1
        self._log(f"[BE] 浮盈达 {self.be_r_multiple:g}R (峰值 {self._peak_r:.2f}R) → "
                  f"止损拉到保本 @ {be_px:.2f} (原 {old_trig:.2f}), 之后持有到收盘")

    def _flatten(self, bar: Bar):
        pos = int(self.portfolio.net_position(self.instrument_id) or 0)
        if pos == 0:
            return
        side = OrderSide.SELL if pos > 0 else OrderSide.BUY
        ref = f"eod-{bar.ts_event}"
        px = bar.close.as_double()
        self.slip.note_signal(ref, kind="eod", side=side.name, qty=float(abs(pos)),
                              signal_px=px, signal_ts_ns=self.clock.timestamp_ns(),
                              bar_ts_ns=bar.ts_event)
        self._log(f"[收盘] 平掉 {pos} 手 @ 信号价 {px:.2f}")
        self.n_eod += 1
        if self.dry_run:
            self.slip.note_skipped(ref, note="DRY_RUN: 收盘平仓信号")
            return
        self.cancel_all_orders(self.instrument_id)
        order = self.order_factory.market(
            instrument_id=self.instrument_id, order_side=side,
            quantity=Quantity.from_str(str(abs(pos))))
        self._order_role[str(order.client_order_id)] = ("eod", ref)
        self.submit_order(order)

    # ---------------- 成交回报 → 滑点落盘 ----------------
    def on_order_filled(self, event):
        cid = str(event.client_order_id)
        role, ref = self._order_role.get(cid, (None, None))
        px = event.last_px.as_double()
        q = int(event.last_qty.as_double())

        if role == "entry":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q, fill_ts_ns=event.ts_event,
                                      order_type="MARKET")
            self._filled_qty += q
            if row:
                self._log(f"[入场成交] {px:.2f} × {q} 手 (累计 {self._filled_qty}) | "
                          f"滑点 {row['slip_ticks']} tick (${row['slip_usd']}) | "
                          f"信号→成交 {row['latency_ms']} ms"
                          f"{'  [STALE 样本]' if row['stale'] else ''}")
            self._place_stop(px, event.ts_event)

        elif role == "stop":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q, fill_ts_ns=event.ts_event,
                                      order_type="STOP_MARKET", trigger_px=self._stop_trigger)
            if row:
                self._log(f"[止损成交] {px:.2f} (触发 {self._stop_trigger:.2f}) | "
                          f"滑点 {row['slip_ticks']} tick (${row['slip_usd']})")
            if self._stop_moved:
                self.n_be_exits += 1
                self._log(f"→ 保本止损出场 (共 {self.n_be_exits} 次)")
            else:
                self.n_stopped += 1
                self._log(f"→ 初始止损出场 (共 {self.n_stopped} 次)")
            self._stop_order = None

        elif role == "eod":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q, fill_ts_ns=event.ts_event,
                                      order_type="MARKET")
            if row:
                self._log(f"[收盘成交] {px:.2f} | 滑点 {row['slip_ticks']} tick")


# ===========================================================================
# 节点配置
# ===========================================================================
def build_node() -> TradingNode:
    instrument_provider = InteractiveBrokersInstrumentProviderConfig(
        load_contracts=frozenset({NQ_CONTRACT}),
    )

    data_config = InteractiveBrokersDataClientConfig(
        ibg_host=IB_HOST,
        ibg_port=IB_PORT,
        ibg_client_id=CLIENT_ID,
        instrument_provider=instrument_provider,
        routing=RoutingConfig(default=True),
        market_data_type=MARKET_DATA_TYPE,   # 订阅实时数据包后必须是 REALTIME (见配置区说明)
        # 必须 False: 默认 True 只推 RTH bar(9:30 起), 盘前 9:00-9:29 的 bar 收不到,
        # 策略的区间会永远为空 → 整天不发信号(且不报错)。对应 IB 请求的 useRTH=False。
        use_regular_trading_hours=False,
    )
    exec_config = InteractiveBrokersExecClientConfig(
        ibg_host=IB_HOST,
        ibg_port=IB_PORT,
        ibg_client_id=CLIENT_ID,
        account_id=ACCOUNT_ID,
        instrument_provider=instrument_provider,
        routing=RoutingConfig(default=True),
    )

    node_config = TradingNodeConfig(
        trader_id="ORB-LIVE-001",
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

    strategy = OrbLiveStrategy(OrbLiveConfig(
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
    node.trader.add_strategy(strategy)     # TradingNode 本身没有 add_strategy

    run_log = flog(f"[启动] TradingNode → IB Gateway {IB_HOST}:{IB_PORT} ({ACCOUNT_ID})")
    flog(f"[启动] 模式: {'DRY_RUN (只记信号)' if DRY_RUN else '真实下单 (paper)'} | "
         f"滑点落盘 {SLIP_CSV} | 合约 {INSTRUMENT_ID} | ${MULTIPLIER:g}/点 | "
         f"风险 {RISK_PER_TRADE:.1%}/笔, 止损 {ATR_STOP_FRACTION:.1%}×{ATR_PERIOD}日ATR, "
         f"{BE_R_MULTIPLE:g}R 拉保本, 上限 {MAX_QTY} 手")
    print(f"运行记录: {run_log}   (Ctrl+C / kill -INT 退出时会写日结汇总)")
    try:
        node.run()                          # 阻塞运行
    finally:
        node.dispose()
        flog("[退出] " + SlippageTracker.summarize(SLIP_CSV))
