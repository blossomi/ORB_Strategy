# -*- coding: utf-8 -*-
"""slippage_tracker.py —— 「信号价 vs 实际成交价」滑点记录器

为什么要它
----------
回测里的 SLIPPAGE_TICKS 是一个**假设**($0.50/手/边)，没人验证过。这个记录器把每次
下单的「信号价」和券商回报的「实际成交价」配成一对，算出真实滑点并落盘 CSV，
用来校准回测参数。只依赖标准库, 不 import nautilus, 便于单独测试。

怎么用(策略里三行)
------------------
    from slippage_tracker import SlippageTracker
    self.slip = SlippageTracker("live_slippage.csv", tick=0.25, multiplier=2.0)

    # ① 下单**之前**登记信号价 (触发信号那根 bar 的收盘价 / 止损单的触发价)
    ref = f"entry-{bar.ts_event}"
    self.slip.note_signal(ref, kind="entry", side="BUY", qty=20,
                          signal_px=bar.close.as_double(), signal_ts_ns=bar.ts_event)

    # ② 成交回报里登记实际成交价, 自动配对 + 写盘
    self.slip.note_fill(ref, fill_px=event.last_px.as_double(), fill_qty=int(event.last_qty),
                        fill_ts_ns=event.ts_event, order_type="MARKET", trigger_px=None)

滑点符号约定: **不利方向为正**。买单价高于信号价 = 正滑点; 卖单价低于信号价 = 正滑点。
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

FIELDS = [
    "ref", "kind", "side", "order_type", "qty", "signal_px", "trigger_px",
    "fill_px", "fill_qty", "cum_fill_qty", "cum_avg_px",
    "slip_pt", "slip_ticks", "slip_usd", "cum_slip_pt",
    "bar_ts_et", "signal_ts_et", "fill_ts_et", "latency_ms", "stale", "note",
]


def _to_et(ns) -> str:
    """纳秒时间戳 -> 美东时间字符串。

    必须显式指定 America/New_York: 之前用 .astimezone() 取的是**本机时区**(CST/UTC+8),
    字段却叫 *_et, 差 12-13 小时, 核对日志时会误导。
    """
    if ns is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    except Exception:                                            # noqa: BLE001
        return str(ns)


class SlippageTracker:
    def __init__(self, path: str = "live_slippage.csv", tick: float = 0.25,
                 multiplier: float = 2.0, stale_ms: float = 2000.0):
        """
        path       : 落盘路径 (追加写, 不存在则写表头)
        tick       : 最小变动价位 (NQ/MNQ = 0.25)
        multiplier : 每点美元数 (MNQ=2, NQ=20)
        stale_ms   : 信号到成交超过这个毫秒数就标 stale=True ——
                     说明行情是延迟的 / 下单链路卡了, 该样本不能当滑点用
        """
        self.path = path
        self.tick = tick
        self.multiplier = multiplier
        self.stale_ms = stale_ms
        self._pending: dict[str, dict] = {}      # ref -> 信号登记
        self._written_header = os.path.exists(path) and os.path.getsize(path) > 0

    # ------------------------------------------------------------------ #
    def note_signal(self, ref: str, *, kind: str, side: str, qty: float,
                    signal_px: float, signal_ts_ns=None,
                    bar_ts_ns=None, trigger_px: float | None = None, note: str = "") -> None:
        """下单前调用。

        signal_px   : 决策依据价 (触发信号那根 bar 的收盘价)
        signal_ts_ns: **下单那一刻的墙钟时间** —— 必须用 clock.timestamp_ns(), 不能用
                      bar.ts_event! IB adapter 给的是 bar 的"开始时间"(见
                      market_data.py 的 _ib_bar_to_ts_event), 而 bar 是在结束时才推送,
                      用 bar.ts_event 会让每笔的 latency 虚增一整根 bar(5 分钟),
                      全部样本被标成 stale。
        bar_ts_ns   : 信号所属 bar 的时间戳, 只用于事后核对(不参与 latency 计算)
        """
        self._pending[str(ref)] = {
            "ref": str(ref), "kind": kind, "side": side.upper(), "qty": float(qty),
            "signal_px": float(signal_px), "trigger_px": trigger_px,
            "signal_ts_ns": signal_ts_ns, "bar_ts_ns": bar_ts_ns, "note": note,
            "cum_qty": 0.0, "cum_notional": 0.0,
        }

    # ------------------------------------------------------------------ #
    def note_fill(self, ref: str, *, fill_px: float, fill_qty: float,
                  fill_ts_ns=None, order_type: str = "MARKET",
                  trigger_px: float | None = None, note: str = "") -> dict | None:
        """成交回报里调用。分笔成交会自动累加, 每次写一行(含累计口径)。"""
        ref = str(ref)
        p = self._pending.get(ref)
        if p is None:
            # 没登记过信号(例如手动下单 / 程序重启) —— 仍然记一行, 滑点留空
            p = {"ref": ref, "kind": "unknown", "side": "", "qty": 0.0,
                 "signal_px": None, "trigger_px": trigger_px, "signal_ts_ns": None,
                 "note": "无信号登记", "cum_qty": 0.0, "cum_notional": 0.0}
            self._pending[ref] = p
        if trigger_px is not None:
            p["trigger_px"] = trigger_px

        fill_px = float(fill_px)
        fill_qty = float(fill_qty)
        p["cum_qty"] += fill_qty
        p["cum_notional"] += fill_px * fill_qty
        cum_avg = p["cum_notional"] / p["cum_qty"] if p["cum_qty"] else fill_px

        base = p["trigger_px"] if p["trigger_px"] is not None else p["signal_px"]
        side = p["side"]
        slip_pt = None
        if base is not None and side in ("BUY", "SELL"):
            slip_pt = (fill_px - base) if side == "BUY" else (base - fill_px)
        cum_slip = None
        if base is not None and side in ("BUY", "SELL"):
            cum_slip = (cum_avg - base) if side == "BUY" else (base - cum_avg)

        latency = None
        if p["signal_ts_ns"] is not None and fill_ts_ns is not None:
            latency = (int(fill_ts_ns) - int(p["signal_ts_ns"])) / 1e6

        row = {
            "ref": ref, "kind": p["kind"], "side": side, "order_type": order_type,
            "qty": p["qty"], "signal_px": p["signal_px"], "trigger_px": p["trigger_px"],
            "fill_px": fill_px, "fill_qty": fill_qty, "cum_fill_qty": p["cum_qty"],
            "cum_avg_px": cum_avg,
            "slip_pt": slip_pt, "slip_ticks": (slip_pt / self.tick if slip_pt is not None else None),
            "slip_usd": (slip_pt * fill_qty * self.multiplier if slip_pt is not None else None),
            "cum_slip_pt": cum_slip,
            "bar_ts_et": _to_et(p.get("bar_ts_ns")),
            "signal_ts_et": _to_et(p["signal_ts_ns"]), "fill_ts_et": _to_et(fill_ts_ns),
            "latency_ms": latency,
            "stale": ("" if latency is None else (latency > self.stale_ms)),
            "note": (note or p["note"]),
        }
        self._append(row)
        return row

    # ------------------------------------------------------------------ #
    def _append(self, row: dict) -> None:
        need_header = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if need_header:
                w.writeheader()
            w.writerow({k: ("" if row.get(k) is None else row[k]) for k in FIELDS})

    # ------------------------------------------------------------------ #
    def note_skipped(self, ref: str, note: str = "DRY_RUN 未下单") -> None:
        """DRY_RUN / 只记录信号时调用: 落一行空滑点记录, 便于核对信号时点。"""
        p = self._pending.pop(str(ref), None) or {}
        row = {k: "" for k in FIELDS}
        row.update({
            "ref": str(ref), "kind": p.get("kind", "signal"), "side": p.get("side", ""),
            "qty": p.get("qty", ""), "signal_px": p.get("signal_px", ""),
            "trigger_px": p.get("trigger_px") or "",
            "bar_ts_et": _to_et(p.get("bar_ts_ns")),
            "signal_ts_et": _to_et(p.get("signal_ts_ns")), "note": note,
        })
        self._append(row)

    # ------------------------------------------------------------------ #
    @staticmethod
    def summarize(path: str) -> str:
        """读回落盘文件, 按 kind 汇总滑点分布(中位/均值/分位), 过滤掉 stale 样本。"""
        if not os.path.exists(path):
            return f"{path} 不存在"
        import statistics as st
        buckets: dict[str, list[float]] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if not r.get("slip_ticks") or r.get("stale") == "True":
                    continue
                buckets.setdefault(r["kind"], []).append(float(r["slip_ticks"]))
        if not buckets:
            return "没有有效样本 (全部 stale 或还没成交)"
        out = [f"滑点统计 (单位 tick, 正=不利; 已剔除 stale 样本)  来源 {path}"]
        for k, v in sorted(buckets.items()):
            v.sort()
            out.append(f"  {k:<8} n={len(v):<5} 中位 {st.median(v):+.2f}  均值 {st.mean(v):+.2f}  "
                       f"25% {v[int(len(v)*0.25)]:+.2f}  75% {v[int(len(v)*0.75)]:+.2f}  最差 {v[-1]:+.2f}")
        return "\n".join(out)


if __name__ == "__main__":
    # 自测: 构造 4 类典型样本, 跑一遍落盘 + 汇总
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "slip_selftest.csv")
    if os.path.exists(tmp):
        os.remove(tmp)
    t = SlippageTracker(tmp, tick=0.25, multiplier=2.0, stale_ms=2000)
    # 入场: 买 20 手, 信号价 29500.00, 实际成交 29500.25 (1 tick 不利)
    t.note_signal("e1", kind="entry", side="BUY", qty=20, signal_px=29500.00, signal_ts_ns=1_700_000_000_000_000_000)
    t.note_fill("e1", fill_px=29500.25, fill_qty=20, fill_ts_ns=1_700_000_000_120_000_000)
    # 入场分两笔成交
    t.note_signal("e2", kind="entry", side="SELL", qty=20, signal_px=29400.00, signal_ts_ns=1_700_000_100_000_000_000)
    t.note_fill("e2", fill_px=29399.75, fill_qty=12, fill_ts_ns=1_700_000_100_090_000_000)
    t.note_fill("e2", fill_px=29399.50, fill_qty=8, fill_ts_ns=1_700_000_100_150_000_000)
    # 止损: 触发价 29450.00, 实际成交 29448.75 (SELL 卖得更低 = 1.25pt = 5 tick 不利)
    t.note_signal("s1", kind="stop", side="SELL", qty=20, signal_px=29450.00,
                  signal_ts_ns=1_700_000_200_000_000_000, trigger_px=29450.00)
    t.note_fill("s1", fill_px=29448.75, fill_qty=20, fill_ts_ns=1_700_000_200_300_000_000, order_type="STOP_MARKET")
    # 延迟样本: 信号到成交 15 分钟 -> stale
    t.note_signal("d1", kind="entry", side="BUY", qty=20, signal_px=29500.00, signal_ts_ns=1_700_000_300_000_000_000)
    t.note_fill("d1", fill_px=29520.00, fill_qty=20, fill_ts_ns=1_700_000_300_000_000_000 + 900_000_000_000)
    print(SlippageTracker.summarize(tmp))
    print("\n落盘内容:")
    with open(tmp, encoding="utf-8") as f:
        print(f.read())
