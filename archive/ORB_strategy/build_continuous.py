# -*- coding: utf-8 -*-
"""
build_continuous.py (v1)
=========================
从本地 GLBX.MDP3 (parent 符号, ohlcv-1m) 构建 NQ 期货的「连续 + 差值后复权」1 分钟序列。

流程:
  1) 流式抽取 NQ 原始合约的 1 分钟 K 线 (过滤掉 ES 与连续映射符号)
  2) 按「每日成交量最大」选出当日主力合约 (数据驱动, 无需硬编码换月规则)
  3) 拼接主力合约 → 差值后复权 (消除换月跳空, 保留"点数差")
  4) 输出 Parquet

注意:
  - Databento 时间戳是 UTC; 本脚本只做数据整理, 时段/时区(9:30-16:00 ET)在 ORB 移植阶段处理。
  - OHLCV-1m 是"稀疏"K线(仅在有成交的分钟产出), 5 分钟聚合时需处理缺口(后续步骤)。
  - 后复权是"加法(差值)"而非"乘法(比值)": 因为期货 P&L 是"点数 × 乘数",
    差值法严格保留任意两时点间的点数差, 与 ORB 的"止损点数/10R"逻辑一致。

用法: cd ORB_strategy && ../.venv/bin/python build_continuous.py
"""
import numpy as np
import pandas as pd
from databento import DBNStore

# ---- 配置 ----
DBN_PATH = "../GLBX-20260831-A8UNFK5CD7/glbx-mdp3-20100606-20260830.ohlcv-1m.dbn.zst"
OUT_PATH = "nq_continuous_1m.parquet"
CHUNK = 1_000_000  # 每批解码的记录数(控制内存)

print("[1/4] 流式解码 DBN, 抽取 NQ 原始合约 1 分钟 K 线 ...", flush=True)
store = DBNStore.from_file(DBN_PATH)

chunks: list[pd.DataFrame] = []
for i, chunk in enumerate(store.to_df(count=CHUNK)):
    # 只保留 NQ 原始合约: NQ + 月份码(H/M/U/Z) + 单位年, 例如 NQU0
    # 排除 ES 系列与连续映射符号(如 "NQM0-NQU0")
    nq = chunk[chunk["symbol"].str.match(r"^NQ[HMUZ]\d$", na=False)]
    if len(nq):
        chunks.append(nq[["instrument_id", "symbol", "open", "high", "low", "close", "volume"]])
    if (i + 1) % 5 == 0:
        total = sum(len(c) for c in chunks)
        print(f"  已处理 {i+1} 批, 累计 NQ bars: {total:,}", flush=True)

df = pd.concat(chunks).sort_index()
df.index.name = "ts_event"
print(f"[1/4] 完成, NQ 原始 bars 总数: {len(df):,}, "
      f"时间范围 {df.index.min()} → {df.index.max()}")

# ---------------------------------------------------------------------------
# 2) 每日主力合约: 按 (日历日, instrument_id) 汇总成交量, 取每日最大
#    用 instrument_id 而非 symbol 作为键, 规避单字符年份(symbol)跨 10 年重名问题
# ---------------------------------------------------------------------------
print("[2/4] 计算每日主力合约(按成交量) ...", flush=True)
df["date"] = df.index.normalize()  # 日历日(UTC 零点)
daily = (
    df.groupby(["date", "instrument_id"], as_index=False)["volume"]
    .sum()
    .sort_values(["date", "volume"], ascending=[True, False])
)
front = daily.drop_duplicates("date").set_index("date")["instrument_id"]
print(f"[2/4] 完成, 覆盖 {len(front)} 个交易日")

# ---------------------------------------------------------------------------
# 3) 只保留当日主力合约的 bar → 连续序列
# ---------------------------------------------------------------------------
print("[3/4] 拼接主力合约 K 线 ...", flush=True)
ser = df[df["instrument_id"] == df["date"].map(front)].copy()
ser = ser.sort_index()
ser = ser.drop(columns=["date"])
# 给每个合约一个可读标签: symbol + 实际年份(用首根 bar 的年份消除单字符年份重名)
ser["_year"] = ser.index.year
_label = ser.groupby("instrument_id").agg(sym=("symbol", "first"), yr=("_year", "first"))
ser["contract"] = (
    ser["instrument_id"].map(_label["sym"]) + "-" + ser["instrument_id"].map(_label["yr"]).astype(str)
)
ser = ser.drop(columns=["_year"])
print(f"[3/4] 完成, 连续序列 bars: {len(ser):,}")

# ---------------------------------------------------------------------------
# 4) 差值后复权: 反向遍历, 跨合约处把"新旧收盘价差"累加到更早的数据上
#    最新合约保持真实价格(调整量=0), 越往历史累积的调整量越大
# ---------------------------------------------------------------------------
print("[4/4] 差值后复权 ...", flush=True)
inst = ser["instrument_id"].to_numpy()
close = ser["close"].to_numpy()
n = len(ser)
adj = np.empty(n, dtype=float)
cum = 0.0
for i in range(n - 1, -1, -1):
    adj[i] = cum                        # 存"调整量", 不是调整后的价格
    if i > 0 and inst[i - 1] != inst[i]:
        # 在 i 处发生换月: 旧合约(更早)整体平移 gap = 新收盘 - 旧收盘
        cum += close[i] - close[i - 1]

adj_series = pd.Series(adj, index=ser.index)
ser["close_raw"] = ser["close"]          # 保留未复权收盘价供核对
for col in ["open", "high", "low", "close"]:
    ser[col] = ser[col] + adj_series

# 输出列
out = ser[["instrument_id", "contract", "open", "high", "low", "close", "volume", "close_raw"]]
out.to_parquet(OUT_PATH)
print(f"[4/4] 已保存 → {OUT_PATH}  (shape={out.shape})")

# ---------------------------------------------------------------------------
# 摘要
# ---------------------------------------------------------------------------
rolls = (inst[1:] != inst[:-1]).sum()
n_contracts = ser["instrument_id"].nunique()
print("\n===== 摘要 =====")
print(f"合约数量       : {n_contracts}")
print(f"换月次数       : {rolls}")
print(f"时间范围       : {ser.index.min()} → {ser.index.max()}")
print(f"价格范围(复权) : {ser['close'].min():.2f} ~ {ser['close'].max():.2f}")
print(f"最近主力合约   : {ser['contract'].iloc[-1]}")
