# -*- coding: utf-8 -*-
"""
prepare_5min_eth.py
===================
把 nq_continuous_1m.parquet (连续+差值后复权 1 分钟, UTC) 转成
「NQ 电子交易时段 (ETH / Globex) 的 5 分钟 K 线」, 供新版交易策略回测使用。

ETH 时段定义 (CME Globex 电子盘, 覆盖 RTH 之外的延长时段):
  - 交易: 周日 18:00 ET 开盘 → 周五 17:00 ET 收盘
  - 每日维护停盘: 17:00-18:00 ET (唯一被过滤掉的小时)
  - 即: 保留除 [17:00, 18:00) ET 之外的全部数据 (≈ 23 小时/交易日, 含 RTH 9:30-16:00)

步骤:
  1) UTC → America/New_York (ET, 自动处理夏令时)
  2) 过滤维护停盘: 去掉 17:00 <= 开盘时间 < 18:00 ET
  3) 1 分钟 → 5 分钟重采样(左闭合, 对齐 00:00), 聚合 OHLCV
  4) 输出 nq_5min_eth.parquet (索引转回 UTC, 供引擎使用)

注意: OHLCV 是"稀疏"数据, 重采样后若某 5 分钟窗口无成交会被丢弃。
用法: cd ORB_strategy && ../.venv/bin/python prepare_5min_eth.py
"""
from datetime import time

import pandas as pd
import zoneinfo

ET = zoneinfo.ZoneInfo("America/New_York")
HALT_START = time(17, 0)   # 维护停盘开始
HALT_END = time(18, 0)     # 维护停盘结束 (18:00 复牌)

print("[1/3] 读取 1 分钟连续数据 ...", flush=True)
df = pd.read_parquet("nq_continuous_1m.parquet")

print("[2/3] 转 ET 并过滤维护停盘 17:00-18:00 ...", flush=True)
df = df.tz_convert(ET)  # index 就地转时区(原地)
t = df.index.time
df = df[~((t >= HALT_START) & (t < HALT_END))]

print("[3/3] 重采样为 5 分钟 (左闭合, 对齐 00:00) ...", flush=True)
res = (
    df.resample("5min", label="left", closed="left")
    .agg(open=("open", "first"), high=("high", "max"),
         low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    .dropna(subset=["open", "high", "low", "close"])
)
res = res[["open", "high", "low", "close", "volume"]]

# ---- 摘要 (在 ET 时区统计, 更贴近交易时段) ----
n_bars = len(res)
et_dates = res.index.normalize().nunique()
# Globex 交易日 = 停盘日(17:00 收盘)所在 ET 日期; 用 "次日含 00:00-16:xx 的数据 + 前一日 18:00 后" 粗估:
# 更稳妥地按"每个交易日 18:00 开盘"数开盘次数来计交易日。
sessions = (res.index.strftime("%H:%M") == "18:00").sum()

print(f"\n===== 摘要 (ETH) =====")
print(f"5 分钟 bar 数 : {n_bars:,}")
print(f"ET 日历日数   : {et_dates}")
print(f"交易日数(按 18:00 开盘计): {sessions}")
print(f"时间范围(ET)  : {res.index.min()} → {res.index.max()}")

# 抽查某一天 18:00 开盘前后的 K 线(转回 ET 显示)
sample_day = res.index.normalize()[0]
day = res[res.index.normalize() == sample_day]
print(f"\n示例日 {sample_day.date()} 的 5 分钟 K 线 (前 3 根 + 尾 3 根, ET):")
print(pd.concat([day.head(3), day.tail(3)]).to_string())

# 转回 UTC 存储 (引擎用 UTC 纳秒时间戳)
res.index = res.index.tz_convert("UTC")
res.index.name = "ts_event"
res.to_parquet("nq_5min_eth.parquet")
print(f"\n已保存 → nq_5min_eth.parquet  (shape={res.shape})")
