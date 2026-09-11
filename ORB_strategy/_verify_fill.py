# Mini verification: what price does the v4 ORB market order actually fill at?
# Runs a 1-week window and compares each entry fill price against:
#   K1 close (9:30 bar close) and K2 open (9:35 bar open) of the same day.
# Run from ORB_strategy/ with the project venv.
import sys
sys.path.insert(0, ".")

import pandas as pd
from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.core.datetime import dt_to_unix_nanos
from nautilus_trader.model.currencies import USD
from nautilus_trader.model.enums import AccountType, AssetClass, OmsType
from nautilus_trader.model.identifiers import InstrumentId, TraderId, Venue
from nautilus_trader.model.instruments import FuturesContract
from nautilus_trader.model.objects import Money, Price, Quantity
from nautilus_trader.model.data import Bar, BarType

import orb_backtes_v4 as v4
from orb_backtes_v4 import OrbStrategyConfig

ET = v4.ET
START = "2016-01-04"
END = "2016-01-15"


class LoggingOrb(v4.OrbStrategy):
    def __init__(self, config, atr_map):
        super().__init__(config, atr_map)
        self.entry_fills = []  # (date, fill_time_ns, side, price)

    def on_order_filled(self, event):
        cid = event.client_order_id
        if cid in self.pending_entry:
            self.entry_fills.append((self._et_date(event.ts_event), event.ts_event, event.order_side, event.last_px.as_double()))
        super().on_order_filled(event)


# ---- replicate v4 build but for a short window ----
df = pd.read_parquet(v4.DATA_PATH).tz_convert(ET)
start = pd.Timestamp(START, tz=ET)
end = pd.Timestamp(END, tz=ET) + pd.Timedelta(days=1)
df = df[(df.index >= start) & (df.index < end)].tz_convert("UTC").sort_index()

instrument_id = InstrumentId.from_str(v4.INSTRUMENT_ID)
first_ns = dt_to_unix_nanos(df.index[0])
last_ns = dt_to_unix_nanos(df.index[-1])
instrument = FuturesContract(
    instrument_id=instrument_id,
    raw_symbol=v4.Symbol("NQ"),
    asset_class=AssetClass.INDEX,
    currency=USD,
    price_precision=v4.PRICE_PRECISION,
    price_increment=Price.from_str(f"{v4.TICK:.2f}"),
    multiplier=Quantity.from_str(f"{v4.MULTIPLIER:.2f}"),
    lot_size=Quantity.from_str("1"),
    underlying="NQ",
    activation_ns=first_ns - 86_400_000_000_000,
    expiration_ns=last_ns + 3_652_000_000_000_000,
    ts_event=first_ns,
    ts_init=first_ns,
)
bar_type = BarType.from_str(f"{v4.INSTRUMENT_ID}-5-MINUTE-LAST-EXTERNAL")
bars = []
for ts, row in df.iterrows():
    ns = dt_to_unix_nanos(ts)
    bars.append(Bar(
        bar_type=bar_type,
        open=Price.from_str(f"{row['open']:.2f}"),
        high=Price.from_str(f"{row['high']:.2f}"),
        low=Price.from_str(f"{row['low']:.2f}"),
        close=Price.from_str(f"{row['close']:.2f}"),
        volume=Quantity.from_str(str(int(row["volume"]))),
        ts_event=ns,
        ts_init=ns,
    ))

venue = Venue(v4.VENUE)
engine = BacktestEngine(config=BacktestEngineConfig(trader_id=TraderId("ORB-VERIFY")))
engine.add_venue(
    venue=venue, oms_type=OmsType.NETTING, account_type=AccountType.MARGIN,
    base_currency=USD,
    starting_balances=[Money(v4.STARTING_CAPITAL, USD)],
    fee_model=PerContractFeeModel(Money(v4.COMMISSION_PER_CONTRACT, USD)),
)
engine.add_instrument(instrument)
engine.add_data(bars)
atr_map = v4.build_atr_map()
strategy = LoggingOrb(
    OrbStrategyConfig(
        instrument_id=v4.INSTRUMENT_ID,
        bar_type=str(bar_type),
        risk_per_trade=v4.RISK_PER_TRADE,
        multiplier=v4.MULTIPLIER,
        atr_stop_fraction=v4.ATR_STOP_FRACTION,
    ),
    atr_map,
)
engine.add_strategy(strategy)
engine.run()

# ---- compare fills against K1 close / K2 open ----
rth = df.tz_convert(ET)
print(f"\n{'date':<12}{'side':<5}{'fill_px':>9}{'K1_close':>10}{'K2_open':>9}  match")
match = 0
total = 0
for d, ts_ns, side, px in strategy.entry_fills:
    day = rth[(rth.index.date == d)]
    if len(day) == 0:
        continue
    k1 = day[(day.index.time >= pd.Timestamp("09:30", tz=ET).time()) & (day.index.time < pd.Timestamp("09:35", tz=ET).time())]
    k2 = day[(day.index.time >= pd.Timestamp("09:35", tz=ET).time()) & (day.index.time < pd.Timestamp("09:40", tz=ET).time())]
    k1c = k1["close"].iloc[0] if len(k1) else float("nan")
    k2o = k2["open"].iloc[0] if len(k2) else float("nan")
    is_k2open = abs(px - k2o) < 1e-9
    if is_k2open:
        match += 1
    total += 1
    print(f"{str(d):<12}{str(side)[:4]:<5}{px:>9.2f}{k1c:>10.2f}{k2o:>9.2f}  {'K2_OPEN' if is_k2open else 'OTHER'}")
print(f"\nfills={total}  matched_K2_open={match}  ({100.0 * match / total:.0f}%)")
