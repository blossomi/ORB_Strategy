# -*- coding: utf-8 -*-
"""
prepare_es.py
=============
从 es_continuous_1m.parquet 生成两份 5 分钟数据:
  1) es_5min_rth.parquet  RTH 9:30-16:00 ET (供回测)
  2) es_5min_eth.parquet  ETH 全时段 (去 17:00-18:00 维护停盘, 供计算盘前区间)

逻辑与 prepare_5min.py / prepare_5min_eth.py 完全一致, 仅换 ES 输入输出。
用法: cd ORB_strategy && ../.venv/bin/python prepare_es.py
"""
from datetime import time

import pandas as pd
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)
HALT_START = time(17, 0)
HALT_END = time(18, 0)

AGG = dict(open=("open", "first"), high=("high", "max"),
           low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))


def _resample5(df_et: pd.DataFrame) -> pd.DataFrame:
    res = (
        df_et.resample("5min", label="left", closed="left")
        .agg(**AGG)
        .dropna(subset=["open", "high", "low", "close"])
    )
    res = res[["open", "high", "low", "close", "volume"]]
    res.index = res.index.tz_convert("UTC")
    res.index.name = "ts_event"
    return res


print("[1/2] 生成 ES 5 分钟 RTH (9:30-16:00) ...", flush=True)
df = pd.read_parquet("es_continuous_1m.parquet").tz_convert(ET)
t = df.index.time
df = df[(t >= RTH_OPEN) & (t < RTH_CLOSE)]
rth = _resample5(df)
rth.to_parquet("es_5min_rth.parquet")
print(f"      es_5min_rth.parquet: {len(rth):,} bars, {rth.index.normalize().nunique():,} 交易日")

print("[2/2] 生成 ES 5 分钟 ETH (去 17:00-18:00 停盘) ...", flush=True)
df = pd.read_parquet("es_continuous_1m.parquet").tz_convert(ET)
t = df.index.time
df = df[~((t >= HALT_START) & (t < HALT_END))]
eth = _resample5(df)
eth.to_parquet("es_5min_eth.parquet")
print(f"      es_5min_eth.parquet: {len(eth):,} bars, {eth.index.normalize().nunique():,} 日历日")
print("\n完成。")
