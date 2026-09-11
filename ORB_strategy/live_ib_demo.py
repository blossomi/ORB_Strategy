# -*- coding: utf-8 -*-
"""
live_ib_demo.py
===============
NautilusTrader 实盘最小框架：连 IB Gateway (paper) → 订阅 NQ/MNQ 5 分钟 bar
→ 跑 ORB 信号 → 记录「信号价 vs 实际成交价」的真实滑点。

目标（按顺序验证，别跳步）
--------------------------
1. 连接 → 合约加载 → 订阅 → 收 bar        （已验证过）
2. DRY_RUN=True：只记录信号, 不下单       ← 建议先跑几天, 核对信号时点对不对
3. DRY_RUN=False：真下单(paper), 累积真实滑点样本 → 用 summarize() 校准回测的 SLIPPAGE_TICKS

⚠️ 关键前提：**必须先有实时 streaming 行情订阅**
   现在账户是 DELAYED_FROZEN(延迟数据), 收到的 bar 比真实市场晚 10-15 分钟。
   在这种状态下测滑点是**无效**的 —— 你算出的"滑点"里混着十几分钟的价格漂移。
   SlippageTracker 会自动把 latency 超标的样本标 stale=True 并在汇总时剔除,
   但这只能防止误读, 不能替代实时行情。

用法
----
  cd ORB_strategy && ../.venv/bin/python live_ib_demo.py            # 默认 DRY_RUN
  收工后看汇总:
  ../.venv/bin/python -c "from slippage_tracker import SlippageTracker as T; print(T.summarize('live_slippage.csv'))"

前置
----
  本机 IB Gateway 已登录 paper 账户, API 端口 4002 开放, API 类型选 IB API(非 FIX/CTCI)。
"""
from datetime import datetime, time, timezone
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

SYMBOL = "NQ"                   # "NQ" (每点 $20) 或 "MNQ" (每点 $2)
CONTRACT_MONTH = "202609"       # IB 用 YYYYMM
LOCAL_SYMBOL = "U6"             # 2026-09 → U6 (月份码 F G H J K M N Q U V X Z)
TICK = 0.25                     # NQ/MNQ 最小变动 = 0.25 pt
MULTIPLIER = 2.0 if SYMBOL == "MNQ" else 20.0

DRY_RUN = True                  # True: 只记信号不下单 (先跑这个!)
ORDER_QTY = 20                  # 下单手数。实盘试水建议从 1 手 MNQ 起
STOP_PTS = 30.0                 # 止损点数。回测是 7.5%×前日14日ATR, 实盘先固定, 之后按当日 ATR 覆盖

# ORB 参数 (对齐回测 v8.4 的当前配置)
T_RANGE_START = time(9, 0)      # 盘前区间开始
T_RANGE_END = time(9, 29)       # 盘前区间结束
T_WIN_START = time(9, 30)       # 入场窗口开始
T_WIN_END = time(10, 10)        # 入场窗口结束 (无突破则当日放弃)
T_FLAT = time(15, 55)           # 收盘清仓

SLIP_CSV = "live_slippage.csv"  # 滑点落盘文件 (追加写)
SLIP_STALE_MS = 2000            # 信号→成交 超过 2 秒即标 stale (正常 IB 往返 < 500ms)

NQ_CONTRACT = IBContract(
    symbol=SYMBOL, secType="FUT", exchange="CME", currency="USD",
    lastTradeDateOrContractMonth=CONTRACT_MONTH,
)
INSTRUMENT_ID = f"{SYMBOL}{LOCAL_SYMBOL}.CME"            # IB_SIMPLIFIED 编码, venue=CME
BAR_TYPE_STR = f"{INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL"

ET = ZoneInfo("America/New_York")


# ===========================================================================
# 策略: ORB 信号 + 滑点记录
# ===========================================================================
class OrbLiveConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    qty: str
    stop_pts: float
    dry_run: bool


class OrbLiveStrategy(Strategy):
    def __init__(self, config: OrbLiveConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.quantity = Quantity.from_str(config.qty)
        self.stop_pts = float(config.stop_pts)
        self.dry_run = bool(config.dry_run)

        self.slip = SlippageTracker(SLIP_CSV, tick=TICK, multiplier=MULTIPLIER,
                                    stale_ms=SLIP_STALE_MS)
        self._order_role: dict[str, tuple[str, str]] = {}   # client_order_id -> (角色, ref)

        self._day = None
        self._rng_hi: float | None = None
        self._rng_lo: float | None = None
        self._entered_today = False
        self._entry_side: OrderSide | None = None
        self._filled_qty = 0
        self._stop_order = None
        self._stop_trigger: float | None = None

    # ---------------- 生命周期 ----------------
    def on_start(self):
        self.subscribe_bars(self.bar_type)
        self.log.info(
            f"已订阅 {self.bar_type} | 合约 {self.instrument_id} ({SYMBOL}, "
            f"${MULTIPLIER:g}/点) | 手数 {self.quantity} | "
            f"{'DRY_RUN 只记信号' if self.dry_run else '!!! 真实下单模式 !!!'}"
        )

    def on_stop(self):
        self.log.info(SlippageTracker.summarize(SLIP_CSV))

    # ---------------- 工具 ----------------
    def _now_et(self, ns: int) -> datetime:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).astimezone(ET)

    # ---------------- 行情 ----------------
    def on_bar(self, bar: Bar):
        t = self._now_et(bar.ts_event)
        d, hhmm = t.date(), t.time()

        if d != self._day:                       # 新交易日: 重置区间与状态
            self._day = d
            self._rng_hi = self._rng_lo = None
            self._entered_today = False
            self._filled_qty = 0
            self._stop_order = None
            self.log.info(f"—— 新交易日 {d} ——")

        # ① 累积盘前区间 (需要 9:00 之前的 bar; 若订阅的是 RTH-only 数据类型, 这里会一直是 None)
        if T_RANGE_START <= hhmm < T_RANGE_END:
            hi, lo = bar.high.as_double(), bar.low.as_double()
            self._rng_hi = hi if self._rng_hi is None else max(self._rng_hi, hi)
            self._rng_lo = lo if self._rng_lo is None else min(self._rng_lo, lo)
            return

        # ② 入场窗口内: 逐根 bar 用收盘价判突破
        if T_WIN_START <= hhmm < T_WIN_END and not self._entered_today:
            if self._rng_hi is None:
                self.log.warning("区间为空 (没收到盘前 bar?) —— 无法判突破")
            else:
                c = bar.close.as_double()
                if c > self._rng_hi:
                    self._on_signal(OrderSide.BUY, bar, c)
                elif c < self._rng_lo:
                    self._on_signal(OrderSide.SELL, bar, c)

        # ③ 收盘清仓
        if hhmm >= T_FLAT:
            self._flatten(bar)

    # ---------------- 信号 / 下单 ----------------
    def _on_signal(self, side: OrderSide, bar: Bar, px: float):
        self._entered_today = True
        ref = f"entry-{bar.ts_event}"
        # ① 先登记信号价 —— 必须在下单之前, 否则延迟会漏进滑点
        self.slip.note_signal(ref, kind="entry", side=side.name,
                              qty=float(self.quantity), signal_px=px,
                              signal_ts_ns=bar.ts_event)
        self.log.info(f"[信号] {side.name} @ {px:.2f}  "
                      f"(区间 {self._rng_lo:.2f} ~ {self._rng_hi:.2f}, 手数 {self.quantity})")

        if self.dry_run:
            self.slip.note_skipped(ref, note="DRY_RUN: 信号已记录, 未下单")
            return

        self._entry_side = side
        order = self.order_factory.market(
            instrument_id=self.instrument_id, order_side=side, quantity=self.quantity)
        self._order_role[str(order.client_order_id)] = ("entry", ref)
        self.submit_order(order)

    def _place_stop(self, fill_px: float, fill_ts_ns: int):
        """入场成交后挂止损。止损单的滑点 = 触发价 vs 实际成交价。"""
        side = OrderSide.SELL if self._entry_side == OrderSide.BUY else OrderSide.BUY
        trig = fill_px - self.stop_pts if side == OrderSide.SELL else fill_px + self.stop_pts
        trig = round(round(trig / TICK) * TICK, 2)
        self._stop_trigger = trig

        ref = f"stop-{fill_ts_ns}"
        self.slip.note_signal(ref, kind="stop", side=side.name, qty=float(self._filled_qty),
                              signal_px=trig, trigger_px=trig, signal_ts_ns=None)
        self._stop_order = self.order_factory.stop_market(
            instrument_id=self.instrument_id, order_side=side,
            quantity=Quantity.from_str(str(self._filled_qty)),
            trigger_price=Price.from_str(f"{trig:.2f}"), reduce_only=True)
        self._order_role[str(self._stop_order.client_order_id)] = ("stop", ref)
        self.submit_order(self._stop_order)
        self.log.info(f"已挂止损 {side.name} {self._filled_qty} 手 @ {trig:.2f}")

    def _flatten(self, bar: Bar):
        pos = int(self.portfolio.net_position(self.instrument_id) or 0)
        if pos == 0:
            return
        side = OrderSide.SELL if pos > 0 else OrderSide.BUY
        ref = f"eod-{bar.ts_event}"
        px = bar.close.as_double()
        self.slip.note_signal(ref, kind="eod", side=side.name, qty=float(abs(pos)),
                              signal_px=px, signal_ts_ns=bar.ts_event)
        self.log.info(f"[收盘] 平掉 {pos} 手")
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
                self.log.info(f"入场成交 {px:.2f} | 滑点 {row['slip_ticks']} tick "
                              f"(${row['slip_usd']}) | 信号→成交 {row['latency_ms']} ms"
                              f"{'  [STALE 样本]' if row['stale'] else ''}")
            self._place_stop(px, event.ts_event)

        elif role == "stop":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q, fill_ts_ns=event.ts_event,
                                      order_type="STOP_MARKET", trigger_px=self._stop_trigger)
            if row:
                self.log.info(f"止损成交 {px:.2f} (触发 {self._stop_trigger:.2f}) | "
                              f"滑点 {row['slip_ticks']} tick (${row['slip_usd']})")
            self._stop_order = None

        elif role == "eod":
            row = self.slip.note_fill(ref, fill_px=px, fill_qty=q, fill_ts_ns=event.ts_event,
                                      order_type="MARKET")
            if row:
                self.log.info(f"收盘平仓 {px:.2f} | 滑点 {row['slip_ticks']} tick")


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
        market_data_type=MarketDataTypeEnum.DELAYED_FROZEN,  # 无实时订阅账户 → 延迟数据
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
    node.add_exec_client_factory("IB", InteractiveBrokersExecClientFactory)
    return node


if __name__ == "__main__":
    node = build_node()
    node.build()

    strategy = OrbLiveStrategy(OrbLiveConfig(
        instrument_id=INSTRUMENT_ID,
        bar_type=BAR_TYPE_STR,
        qty=str(ORDER_QTY),
        stop_pts=STOP_PTS,
        dry_run=DRY_RUN,
    ))
    node.trader.add_strategy(strategy)     # TradingNode 本身没有 add_strategy

    print(f"启动 TradingNode → IB Gateway {IB_HOST}:{IB_PORT} ({ACCOUNT_ID})")
    print(f"模式: {'DRY_RUN (只记信号)' if DRY_RUN else '真实下单 (paper)'} | "
          f"滑点落盘: {SLIP_CSV} | 合约 {INSTRUMENT_ID} | ${MULTIPLIER:g}/点")
    try:
        node.run()                          # 阻塞运行
    finally:
        node.dispose()
        print("\n" + SlippageTracker.summarize(SLIP_CSV))
