# -*- coding: utf-8 -*-
"""
live_ib_demo.py
===============
NautilusTrader 实盘最小框架：连 IB Gateway (paper) → 订阅 NQ 5 分钟 bar → 打印行情。

目标：先验证「连接 → 合约加载 → 订阅 → 收行情」整条链路跑通，再逐步接策略逻辑。

用法: cd ORB_strategy && ../.venv/bin/python live_ib_demo.py
前置: 本机 IB Gateway 已登录 paper 账户, API 端口 4002 开放。
"""
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
from nautilus_trader.trading.strategy import Strategy

# ===========================================================================
# ★ 配置区 —— 按需修改
# ===========================================================================
IB_HOST = "127.0.0.1"
IB_PORT = 4002                  # IB Gateway paper 端口 (live 用 4001)
ACCOUNT_ID = "DUQ715008"        # paper 账户 ID
CLIENT_ID = 1

# NQ 合约: 2026 年 9 月 (NQU6)。改成当前活跃合约:
#   月份码 F G H J K M N Q U V X Z = 1..12 月; 单年尾数, 如 2026-09 → "U6" → NQU6
NQ_CONTRACT = IBContract(
    symbol="NQ",
    secType="FUT",
    exchange="CME",
    currency="USD",
    lastTradeDateOrContractMonth="202609",
)
INSTRUMENT_ID = "NQU6.CME"                          # IB_SIMPLIFIED 编码 (venue=CME)
BAR_TYPE_STR = "NQU6.CME-5-MINUTE-LAST-EXTERNAL"


# ===========================================================================
# 策略: 只订阅 + 打印 (验证链路)
# ===========================================================================
class LiveBarDemoConfig(StrategyConfig):
    instrument_id: str
    bar_type: str


class LiveBarDemoStrategy(Strategy):
    def __init__(self, config: LiveBarDemoConfig):
        super().__init__(config)
        self.bar_type = BarType.from_str(config.bar_type)

    def on_start(self):
        self.subscribe_bars(self.bar_type)
        self.log.info(f"已订阅 {self.bar_type}")

    def on_bar(self, bar: Bar):
        self.log.info(
            f"[{self._ts(bar)}] O={bar.open} H={bar.high} L={bar.low} C={bar.close} V={bar.volume}"
        )

    def _ts(self, bar: Bar) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(bar.ts_event / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# 节点配置
# ===========================================================================
def build_node() -> TradingNode:
    # instrument provider: 自动加载 NQ 合约
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
    node = build_node()
    node.build()

    # 注册策略 (build 后通过底层 trader 注册)
    strategy = LiveBarDemoStrategy(
        LiveBarDemoConfig(instrument_id=INSTRUMENT_ID, bar_type=BAR_TYPE_STR)
    )
    node.trader.add_strategy(strategy)

    print("启动 TradingNode, 连接 IB Gateway (paper) ...")
    try:
        node.run()  # 阻塞运行
    finally:
        node.dispose()
