# Variant test: enter with a MARKET order on the K2 bar event (t == 09:35).
# What price does it fill at? K2 open? K2 close? Something else?
# Run from ORB_strategy/ with the project venv.
import sys
sys.path.insert(0, ".")
from datetime import time as dtime

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType, OrderSide
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity

import orb_backtes_v4 as v4
from orb_backtes_v4 import OrbStrategyConfig

ET = v4.ET
START = "2016-01-04"
END = "2016-01-15"
T_K2 = dtime(9, 35)


class EnterOnK2(v4.OrbStrategy):
    def __init__(self, config, atr_map):
        super().__init__(config, atr_map)
        self.fills = []  # (date, fill_px, k2_open, k2_close)

    def on_bar(self, bar):
        t = self._et_time(bar.ts_event)
        if t == v4.T_K1:
            # record K1 direction only (same rule as v4)
            self.k1 = {
                "open": bar.open.as_double(),
                "high": bar.high.as_double(),
                "low": bar.low.as_double(),
                "close": bar.close.as_double(),
            }
        elif t == T_K2:
            self._enter_on_k2(bar)  # market order NOW
        elif t == v4.T_EOD:
            self.cancel_all_orders(self.instrument_id)
            self.close_all_positions(self.instrument_id)

    def _enter_on_k2(self, bar):
        k1o, k1c = self.k1["open"], self.k1["close"]
        if k1c > k1o:
            side = OrderSide.BUY
        elif k1c < k1o:
            side = OrderSide.SELL
        else:
            return
        atr = self.atr_map.get(self._et_date(bar.ts_event))
        if atr is None or atr <= 0:
            return
        stop_dist = max(v4.TICK, v4.tick_round(v4.ATR_STOP_FRACTION * atr))
        entry = bar.open.as_double()  # K2 open (for the record)
        stop_price = v4.tick_round(entry - stop_dist) if side == OrderSide.BUY else v4.tick_round(entry + stop_dist)
        actual_dist = abs(entry - stop_price)
        if actual_dist <= 0:
            return
        equity = self._equity()
        qty = int(v4.floor(equity * self.risk_per_trade / (actual_dist * v4.MULTIPLIER)))
        if qty < 1:
            self.n_skipped += 1
            return
        order = self.order_factory.market(
            instrument_id=self.instrument_id, order_side=side, quantity=Quantity.from_str(str(qty)),
        )
        self.pending_entry[order.client_order_id] = actual_dist
        self.submit_order(order)

    def on_order_filled(self, event):
        cid = event.client_order_id
        if cid in self.pending_entry:
            d = self._et_date(event.ts_event)
            self.fills.append((d, event.last_px.as_double()))
        super().on_order_filled(event)


# ---- build (same as verify_fill) ----
df = pd.read_parquet(v4.DATA_PATH).tz_convert(ET)
start = pd.Timestamp(START, tz=ET)
end = pd.Timestamp(END, tz=ET) + pd.Timedelta(days=1)
df = df[(df.index >= start) & (df.index < end)].tz_convert("UTC").sort_index()

instrument_id = InstrumentId.from_str(v4.INSTRUMENT_ID)
first_ns = dt_to_unix_nanos(df.index[0])
last_ns = dt_to_unix_nanos(df.index[-1])
instrument = FuturesContract(
    instrument_id=instrument_id, raw_symbol=v4.Symbol("NQ"), asset_class=AssetClass.INDEX,
    currency=USD, price_precision=v4.PRICE_PRECISION,
    price_increment=Price.from_str(f"{v4.TICK:.2f}"),
    multiplier=Quantity.from_str(f"{v4.MULTIPLIER:.2f}"),
    lot_size=Quantity.from_str("1"), underlying="NQ",
    activation_ns=first_ns - 86_400_000_000_000,
    expiration_ns=last_ns + 3_652_000_000_000_000,
    ts_event=first_ns, ts_init=first_ns,
)
bar_type = BarType.from_str(f"{v4.INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL")
bars = []
for ts, row in df.iterrows():
    ns = dt_to_unix_nanos(ts)
    bars.append(Bar(
        bar_type=bar_type, open=Price.from_str(f"{row['open']:.2f}"),
        high=Price.from_str(f"{row['high']:.2f}"), low=Price.from_str(f"{row['low']:.2f}"),
        close=Price.from_str(f"{row['close']:.2f}"),
        volume=Quantity.from_str(str(int(row["volume"]))), ts_event=ns, ts_init=ns,
    ))

venue = Venue(v4.VENUE)
engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("ORB-VERIFY-K2")))
engine.add_venue(
    venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
    base_currency=USD, starting_balances=[Money(v4.STARTING_CAPITAL, USD)],
    fee_model=PerContractFeeModel(Money(v4.COMMISSION_PER_CONTRACT, USD)),
)
engine.add_instrument(instrument)
engine.add_data(bars)
strategy = EnterOnK2(
    OrbStrategyConfig(
        instrument_id=v4.INSTRUMENT_ID, bar_type=str(bar_type),
        risk_per_trade=v4.RISK_PER_TRADE, multiplier=v4.MULTIPLIER,
        atr_stop_fraction=v4.ATR_STOP_FRACTION,
    ),
    v4.build_atr_map(),
)
engine.add_strategy(strategy)
engine.run()

rth = df.tz_convert(ET)
print(f"\n{'date':<12}{'fill_px':>9}{'K2_open':>9}{'K2_close':>9}  match")
for d, px in strategy.fills:
    day = rth[(rth.index.date == d)]
    k2 = day[(day.index.time >= pd.Timestamp("09:35", tz=ET).time()) & (day.index.time < pd.Timestamp("09:40", tz=ET).time())]
    k2o = k2["open"].iloc[0] if len(k2) else float("nan")
    k2c = k2["close"].iloc[0] if len(k2) else float("nan")
    tag = "K2_OPEN" if abs(px - k2o) < 1e-9 else ("K2_CLOSE" if abs(px - k2c) < 1e-9 else "OTHER")
    print(f"{str(d):<12}{px:>9.2f}{k2o:>9.2f}{k2c:>9.2f}  {tag}")
