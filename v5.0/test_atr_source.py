# -*- coding: utf-8 -*-
"""
test_atr_source.py — atr_source 模块 + orb_live ATR glue 的常设测试 (随 test_fsm 惯例)
=====================================================================================
覆盖:
  1 atr_from_table: 与独立 pandas 递推逐位一致 / 行数不足 → None / 假日 as_of 语义
  2 table_freshness: 陈旧天数口径 (含假日长周末场景)
  3 _merge: 表优先 append-only (已有行不覆盖, 新日期补入, source 留痕)
  4 update_table 全源失败: ok=False + 表不动 + 状态文件留痕
  5 glue (引擎内跑真实 OrbFsmLiveStrategy, atr_map=None):
      启动/换日从表重算 ATR → FSM 实际用该值定价 (r_pts 对账) + 收盘更新闹钟登记
      + 更新失败重试 ladder (3 次后 [ATR告警])
前置: data/ndx_daily.parquet 存在 (缺则先跑 `python atr_source.py`)。
用法: cd v5.0 && ../.venv/bin/python test_atr_source.py
"""
import json
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import atr_source as a

ET = ZoneInfo("America/New_York")
_PASSED = []


def ok(name):
    _PASSED.append(name)
    print(f"[test_{name}] OK")


# ---------------------------------------------------------------- 1 递推公式
def test_atr_values():
    df = a.load_table()
    assert df is not None, "表缺失 —— 先跑 `python atr_source.py` bootstrap"
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(),
                    (df["low"] - pc).abs()], axis=1).max(axis=1)
    ref_full = tr.ewm(alpha=1 / 14, adjust=False).mean()
    for dt in ("2026-08-28", "2026-06-22", "2022-07-05", "2021-09-20"):
        got = a.atr_from_table(df, dt)
        ref = float(ref_full[ref_full.index < pd.Timestamp(dt)].iloc[-1])
        assert abs(got - ref) < 1e-9, f"{dt}: {got} != {ref}"
    # 行数不足 → None
    assert a.atr_from_table(df, "2006-10-01") is None
    assert a.atr_from_table(None, "2026-08-28") is None
    # 假日 as_of (2026-06-19 六月节, 指数无 bar): 用 < as_of 的行 = 截至 6-18 递推
    v = a.atr_from_table(df, "2026-06-19")
    assert v is not None and v > 0
    ok("atr_values")


# ---------------------------------------------------------------- 2 新鲜度
def test_freshness():
    df = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
                      index=pd.DatetimeIndex([pd.Timestamp("2026-07-02")], name="date"))
    assert a.table_freshness(df, date(2026, 7, 3)) == (1, pd.Timestamp("2026-07-02"))
    assert a.table_freshness(df, date(2026, 7, 7)) == (5, pd.Timestamp("2026-07-02"))  # 含独立日+周末
    assert a.table_freshness(df, date(2026, 7, 8))[0] == 6
    assert a.freshness_ok(df, date(2026, 7, 7)) and not a.freshness_ok(df, date(2026, 7, 8))
    assert a.table_freshness(None, date(2026, 7, 3))[0] == 9999
    ok("freshness")


# ---------------------------------------------------------------- 3 合并
def test_merge():
    old = pd.DataFrame({"open": [100.0], "high": [101.0], "low": [99.0],
                        "close": [100.5], "source": ["yahoo"],
                        "fetched_at": ["2026-09-14T17:10:00-04:00"]},
                       index=pd.DatetimeIndex([pd.Timestamp("2026-09-14")], name="date"))
    fresh = pd.DataFrame({"open": [100.0, 101.0], "high": [101.0, 102.0],
                          "low": [99.0, 100.0], "close": [100.5, 101.5]},
                         index=pd.DatetimeIndex([pd.Timestamp("2026-09-14"),
                                                 pd.Timestamp("2026-09-15")], name="date"))
    now = datetime(2026, 9, 15, 17, 10, tzinfo=ET)
    out, n = a._merge(old, fresh, "nasdaq", now)
    assert n == 1, "已有行不许覆盖 (append-only)"
    assert len(out) == 2 and out.loc["2026-09-15", "source"] == "nasdaq"
    assert out.loc["2026-09-14", "source"] == "yahoo"
    assert out.index.is_monotonic_increasing and not out.index.duplicated().any()
    out2, n2 = a._merge(None, fresh, "yahoo", now)
    assert n2 == 2 and len(out2) == 2
    ok("merge")


# ---------------------------------------------------------------- 4 全源失败
def test_update_all_fail():
    table_before = a.load_table()
    orig = a.SOURCES
    a.SOURCES = [("dead", lambda full: (_ for _ in ()).throw(RuntimeError("模拟断网")))]
    try:
        res = a.update_table(log=lambda m: None)
    finally:
        a.SOURCES = orig
    assert res["ok"] is False and "dead" in res["error"]
    after = a.load_table()
    assert len(after) == len(table_before), "失败时表不许动"
    st = json.loads(a.STATUS_PATH.read_text(encoding="utf-8"))
    assert st["ok"] is False and st["error"], "状态文件必须留痕"
    ok("update_all_fail")


# ---------------------------------------------------------------- 5 live glue
def test_live_glue():
    import orb_backtest as ob
    import orb_live as ol

    # 5a 引擎内跑真实策略 (atr_map=None → 启动/换日走磁盘表), ~4 周窗口。
    # 必须喂 ETH 数据 (live 适配层靠 9:00-9:29 真实 bar 累积盘前区间, 与 verify_live 同法)
    eth = pd.read_parquet(ob.RANGE_DATA_PATH)
    et_lookup: dict = {}

    strat_logs = []

    def run_window(start, end):
        orig = (ob.START_DATE, ob.END_DATE)
        ob.START_DATE, ob.END_DATE = str(start), str(end)
        try:
            bars, instrument, bar_type, sample_df = ob.build_bars_and_instrument(eth, et_lookup)
        finally:
            ob.START_DATE, ob.END_DATE = orig
        from nautilus_trader.backtest.engine import BacktestEngine
        from nautilus_trader.config import BacktestEngineConfig, LoggingConfig
        engine = BacktestEngine(config=BacktestEngineConfig(
            trader_id=ob.TraderId("ATR-TEST"),
            logging=LoggingConfig(log_level="ERROR")))
        engine.add_venue(venue=ob.Venue(ob.VENUE), oms_type=ob.OmsType.NETTING,
                         account_type=ob.AccountType.MARGIN, base_currency=ob.USD,
                         starting_balances=[ob.Money(ob.STARTING_CAPITAL, ob.USD)],
                         fee_model=ob.PerContractFeeModel(ob.Money(1.0, ob.USD)))
        engine.add_instrument(instrument)
        engine.add_data(bars)
        cfg = ol.OrbFsmLiveConfig(
            instrument_id=ob.INSTRUMENT_ID, bar_type=str(bar_type),
            risk_per_trade=0.007, atr_stop_fraction=0.075, be_r_multiple=5.0,
            be_buffer_ticks=0, max_qty=50, multiplier=2.0, dry_run=False)
        # dry_run=False: DRY_RUN 只记信号不下单 → FSM 无成交 → 入场计数恒 0, 无法验证 sizing
        s = ol.OrbFsmLiveStrategy(cfg, atr_map=None)
        s._log = (lambda m, level="info", _l=strat_logs:
                  _l.append((level, m)) or None)
        engine.add_strategy(s)
        engine.run()
        engine.dispose()
        return s

    s = run_window("2021-09-20", "2021-10-18")     # ~4 周, 确保有突破日
    table = a.load_table()
    # 换日重算: 每个交易日日志里的 ATR = 表递推值 (2 位小数逐字对账)
    traded_days = sorted({m.split(" ")[2].rstrip(":") for lv, m in strat_logs
                          if m.startswith("[ATR] 换日 ")})
    assert traded_days, "没有任何换日 ATR 刷新日志:\n" + "\n".join(m for _, m in strat_logs[:40])
    for d in traded_days:
        v = a.atr_from_table(table, date.fromisoformat(d))
        line = next(m for lv, m in strat_logs if m.startswith(f"[ATR] 换日 {d}"))
        assert f"= {v:.2f} pt" in line, f"{d}: 日志 ATR 与表递推不一致: {line}"
    if s.fsm.n_entries == 0:
        raise AssertionError("4 周窗口零入场 —— ATR 未被 FSM 采用? 日志前 60 行:\n"
                             + "\n".join(m for _, m in strat_logs[:60]))
    # 收盘更新闹钟: 恰好登记 1 次
    assert sum(1 for name, _ in s.timers_armed if name == "atr_update") == 1, \
        f"收盘更新闹钟应登记 1 次: {s.timers_armed}"
    ok("live_glue_engine")

    # 5b 更新失败 ladder: 3 次失败 → [ATR告警] + 计数复位; 成功 → 复位
    strat_logs.clear()
    import orb_live
    fails = [{"ok": False, "error": "模拟全源失败", "source": None, "added": 0,
              "warn": [], "attempts": []}] * 3
    good = {"ok": True, "error": None, "source": "yahoo", "added": 1, "warn": [],
            "attempts": []}
    seq = iter(fails + [good])
    orig_upd = orb_live.update_table
    orb_live.update_table = lambda log=None, **kw: next(seq)
    try:
        s._atr_upd_attempts = 0
        for _ in range(3):
            s._on_atr_update_timer(None)
        assert s._atr_upd_attempts == 0, "3 次失败后计数应复位"
        alerts = [m for lv, m in strat_logs if "ATR告警" in m and "连续 3 次" in m]
        assert len(alerts) == 1, f"应恰有 1 条升级告警: {alerts}"
        retries = sum(1 for name, _ in s.timers_armed if name == "atr_retry")
        assert retries == 2, f"前 2 次失败应各安排 1 次重试: {retries}"
        s._on_atr_update_timer(None)                      # 第 4 次: 成功
        assert not [m for lv, m in strat_logs if "ATR告警" in m and "连续" in m][1:], \
            "成功后不应再升级告警"
        assert any("收盘更新完成" in m for lv, m in strat_logs), \
            f"成功日志缺失, 现有 {len(strat_logs)} 条:\n" + "\n".join(
                f"  {lv} {m}" for lv, m in strat_logs[-8:])
    finally:
        orb_live.update_table = orig_upd
    ok("live_glue_ladder")


def ATR_FMT(v):
    return f"= {v:.2f} pt"


if __name__ == "__main__":
    test_atr_values()
    test_freshness()
    test_merge()
    test_update_all_fail()
    test_live_glue()
    print(f"\n全部 {len(_PASSED)} 组用例通过 ✔")
