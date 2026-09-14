# -*- coding: utf-8 -*-
"""
prepare_15min.py
================
把 nq_continuous_1m.parquet (连续+差值后复权 1 分钟, UTC) 转成
「NQ 常规时段 9:30-16:00 ET 的 15 分钟 K 线」, 供 NautilusTrader 回测使用。

步骤 (与 prepare_5min.py 一致, 仅粒度改 15 分钟):
  1) UTC → America/New_York (ET, 自动处理夏令时)
  2) 过滤 RTH: 09:30 <= 开盘时间 < 16:00
  3) 1 分钟 → 15 分钟重采样(左闭合, 对齐 9:30), 聚合 OHLCV
  4) 输出 nq_15min_rth.parquet (索引转回 UTC, 供引擎使用)

用法: cd ORB_strategy && ../.venv/bin/python prepare_15min.py
"""
from datetime import time

import pandas as pd
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)

print("[1/3] 读取 1 分钟连续数据 ...", flush=True)
df = pd.read_parquet("nq_continuous_1m.parquet")

print("[2/3] 转 ET 并过滤 RTH 9:30-16:00 ...", flush=True)
df = df.tz_convert(ET)
t = df.index.time
df = df[(t >= RTH_OPEN) & (t < RTH_CLOSE)]

print("[3/3] 重采样为 15 分钟 (左闭合, 对齐 9:30) ...", flush=True)
res = (
    df.resample("15min", label="left", closed="left")
    .agg(open=("open", "first"), high=("high", "max"),
         low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    .dropna(subset=["open", "high", "low", "close"])
)
res = res[["open", "high", "low", "close", "volume"]]

# 转回 UTC 存储 (引擎用 UTC 纳秒时间戳)
res.index = res.index.tz_convert("UTC")
res.index.name = "ts_event"
res.to_parquet("nq_15min_rth.parquet")

# 摘要
days = res.index.normalize().nunique()
print(f"\n===== 摘要 =====")
print(f"15 分钟 bar 数 : {len(res):,}")
print(f"交易日数      : {days}")
print(f"时间范围      : {res.index.min()} → {res.index.max()} (UTC)")
# 抽查某一天的前几根 K 线(转回 ET 显示)
sample_day = res.index.normalize()[0]
day = res[res.index.normalize() == sample_day].head(4)
print(f"\n示例日 {sample_day.date()} 前 4 根 15 分钟 K 线 (ET):")
print(day.tz_convert(ET).to_string())
