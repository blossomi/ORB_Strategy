# -*- coding: utf-8 -*-
"""
atr_source.py — live 仓位 ATR 的外源数据层: NDX 指数日线 → 磁盘持久表 → Wilder 14日ATR
=====================================================================================
2026-09-16 研判结论 (notebook.md 当日条 / _probe_ndx_atr*.py):
  · NDX 指数(RTH口径)日线 ATR vs 自持期货 RTH 日线 ATR: 窗口内比值中位 0.991,
    P10-P90 ±3%, 最坏 +10% 且全在指数假日后 1-2 周 (方向 = 仓位偏小, 安全侧)。
  · 同引擎只换 ATR 源 A/B: 1,444 笔完全相同、MDD/PF/胜率不变, 终值差 +20.3%
    (复利路径噪声) → **禁混源**: ATR 永远只从这一张表递推, 冗余源仅用于补缺+交叉校验。
  · 假日语义: 美股假日指数无 bar 而期货照常交易 → 当日 ATR = 「截至今晨已完整收盘的
    最后一根 bar」的递推值; 缺昨天 ≠ 故障。
  · 失败分层: 源全挂但表陈旧 ≤ ATR_STALE_DAYS 个日历日 → 告警但照常交易;
    表缺失/行数不足/陈旧超限 → atr=None → FSM 当日不交易 (orb_fsm.atr_for 现成语义)。

  必须是指数日线 (RTH 口径); Yahoo NQ=F 全时段期货口径偏差 +11.9% 不可用 (实测)。

表  data/ndx_daily.parquet:  date(index) | open high low close | source | fetched_at
状态 data/atr_status.json:   每次更新周期的结果 (供运维 grep / 启动校验参考)

用法:
  cd v5.0 && ../.venv/bin/python atr_source.py            # 更新一轮 (表缺则自动 bootstrap)
  ../.venv/bin/python atr_source.py --status              # 查看上次更新状态
  ../.venv/bin/python atr_source.py --atr                 # 打印当前 ATR 与新鲜度
网络层仅用 stdlib urllib —— 不新增任何声明外依赖。
"""
import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
ATR_PERIOD = 14

ROOT = Path(__file__).resolve().parent
TABLE_PATH = ROOT / "data" / "ndx_daily.parquet"
STATUS_PATH = ROOT / "data" / "atr_status.json"

ATR_STALE_DAYS = 5        # 表最新行距今超过 N 个日历日 → 陈旧 (覆盖 长周末+假日, 见研判)
CROSS_CHECK_TOL = 0.02    # 多源同日 close 相对差超 2% → 告警 (坏源数据进不了表)
MIN_ROWS_FULL = 300       # 表行数低于此 → 视为需要 bootstrap (全量)
BOOTSTRAP_RANGE = "20y"   # 全量拉取 (parquet 2012 起, 20y 覆盖 + 充分暖机)
INCR_RANGE = "14d"        # 日常增量 (覆盖长周末 + 假日)

_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}


# ===========================================================================
# 数据源 —— 每个 fetcher 返回 DataFrame(索引=ET 自然日, 列 open/high/low/close)
# ===========================================================================
def fetch_yahoo(full: bool = False) -> pd.DataFrame:
    """Yahoo chart API ^NDX 官方日线 (RTH 口径, _probe_ndx_atr.py A 组已验证)。"""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{urllib.parse.quote('^NDX', safe='')}?range={BOOTSTRAP_RANGE if full else INCR_RANGE}&interval=1d")
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
        d = json.loads(r.read().decode())
    r_ = d["chart"]["result"][0]
    q = r_["indicators"]["quote"][0]
    idx = pd.to_datetime(r_["timestamp"], unit="s", utc=True).tz_convert(ET)
    df = pd.DataFrame({"open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"]}, index=idx)
    df.index = df.index.normalize().tz_localize(None)
    df.index.name = "date"
    return df.dropna().astype(float)


def fetch_nasdaq(full: bool = False) -> pd.DataFrame:
    """api.nasdaq.com 官方历史端点 (需浏览器式 header; 数据带千分位)。"""
    to = datetime.now(ET).date()
    days = 3650 if full else 60
    url = (f"https://api.nasdaq.com/api/quote/NDX/historical?assetclass=index"
           f"&fromdate={to - timedelta(days=days):%Y-%m-%d}&todate={to:%Y-%m-%d}&limit=9999")
    req = urllib.request.Request(url, headers={**_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode())
    rows = (d.get("data") or {}).get("tradesTable", {}).get("rows") or []
    if not rows:
        raise RuntimeError(f"nasdaq 返回空 (status={d.get('status', {}).get('rcode')})")
    rec = {}
    for row in rows:
        dt = datetime.strptime(row["date"], "%m/%d/%Y")

        def num(k):
            v = (row.get(k) or "").replace(",", "")
            return float(v) if v not in ("", "--", "N/A") else None

        rec[pd.Timestamp(dt.date())] = {"open": num("open"), "high": num("high"),
                                        "low": num("low"), "close": num("close")}
    df = pd.DataFrame.from_dict(rec, orient="index").dropna().astype(float).sort_index()
    df.index.name = "date"
    return df


def fetch_stooq(full: bool = False) -> pd.DataFrame:
    """Stooq CSV 全史 (最简接口; 2026-09-16 起对本机 curl 挂 JS 质询, 保留做优雅降级)。"""
    url = "https://stooq.com/q/d/l/?s=%5Endx&i=d"
    with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=20) as r:
        text = r.read().decode(errors="replace")
    if text.lstrip().startswith("<"):
        raise RuntimeError("stooq 返回反爬质询页 (非 CSV)")
    from io import StringIO
    df = pd.read_csv(StringIO(text), parse_dates=["Date"], index_col="Date")
    df.index.name = "date"
    return df[["Open", "High", "Low", "Close"]].dropna().rename(
        columns=str.lower).astype(float)


# 优先级即合并优先级 (同日已有行时先到先得, 后源只补缺+交叉校验)
SOURCES = [("yahoo", fetch_yahoo), ("nasdaq", fetch_nasdaq), ("stooq", fetch_stooq)]


# ===========================================================================
# 表操作
# ===========================================================================
def load_table() -> pd.DataFrame | None:
    if not TABLE_PATH.exists():
        return None
    return pd.read_parquet(TABLE_PATH)


def table_freshness(df: pd.DataFrame | None, today=None) -> tuple[int, "pd.Timestamp | None"]:
    """(陈旧日历日数, 最新行日期)。无表/空表 → (9999, None)。"""
    today = today or datetime.now(ET).date()
    if df is None or df.empty:
        return 9999, None
    last = df.index.max()
    return (today - last.date()).days, last


def freshness_ok(df: pd.DataFrame | None, today=None) -> bool:
    stale, _ = table_freshness(df, today)
    return stale <= ATR_STALE_DAYS


def atr_from_table(df: pd.DataFrame | None, as_of,
                   period: int = ATR_PERIOD) -> float | None:
    """as_of 当日用的前一日 Wilder ATR: 只用 date < as_of 的行递推取末值。

    与回测 build_atr_map 同公式 (TR 三项取 max → ewm(alpha=1/14, adjust=False))。
    指数假日的缺 bar 不特殊处理 —— 递推天然把 prev_close 跨到上一交易日 (研判②)。
    行数 < period+1 → None (调用方降级为当日不交易)。
    """
    if df is None or df.empty:
        return None
    h = df[df.index < pd.Timestamp(as_of)]
    if len(h) < period + 1:
        return None
    pc = h["close"].shift(1)
    tr = pd.concat([h["high"] - h["low"],
                    (h["high"] - pc).abs(),
                    (h["low"] - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    return float(atr.iloc[-1])


def _merge(old: pd.DataFrame | None, fresh: pd.DataFrame, source: str,
           now: datetime) -> tuple[pd.DataFrame, int]:
    """表优先 (append-only): 只补表中没有的日期, 已有行不覆盖。返回 (新表, 新增行数)。"""
    f = fresh[~fresh.index.isin([] if old is None else old.index)].copy()
    f["source"] = source
    f["fetched_at"] = now.isoformat(timespec="seconds")
    if old is None or old.empty:
        return f.sort_index(), len(f)
    out = pd.concat([old, f]).sort_index()
    return out[~out.index.duplicated(keep="first")], len(f)


def update_table(full: bool = False, log=print) -> dict:
    """跑一轮更新: 逐源拉取→交叉校验→补缺合并→写表+状态文件。

    ok = 至少一个源成功。全挂时也写状态文件 (运维可见), 返回 ok=False。
    """
    now = datetime.now(ET)
    old = load_table()
    need_full = full or old is None or len(old) < MIN_ROWS_FULL
    res = {"ok": False, "source": None, "added": 0, "warn": [], "error": None,
           "attempts": []}

    merged = old
    for name, fn in SOURCES:
        try:
            fresh = fn(full=need_full)
        except Exception as e:                      # 单源失败不挡后源
            res["attempts"].append({"source": name, "ok": False,
                                    "error": f"{type(e).__name__}: {e}"})
            log(f"[ATR源] {name} 失败: {type(e).__name__}: {e}")
            continue
        if fresh.empty:
            res["attempts"].append({"source": name, "ok": False, "error": "empty"})
            continue
        # 交叉校验: 与表内已有同日行比 close (只告警不覆盖 —— append-only)
        if merged is not None:
            common = fresh.index.intersection(merged.index)
            rel = ((fresh.loc[common, "close"] - merged.loc[common, "close"]).abs()
                   / merged.loc[common, "close"]) if len(common) else pd.Series(dtype=float)
            for dt, v in rel[rel > CROSS_CHECK_TOL].items():
                res["warn"].append(f"{name} 与表内 {dt.date()} close 差 {v:.1%}")
        merged, n_new = _merge(merged, fresh, name, now)
        res["attempts"].append({"source": name, "ok": True, "rows": len(fresh),
                                "new": n_new})
        if res["source"] is None:
            res["source"], res["added"] = name, n_new
        else:
            res["added"] += n_new

    if not any(a["ok"] for a in res["attempts"]):
        # 全源失败: 表保持不动, 但状态文件必须留痕 (运维可见 / 上游据此告警)
        res["error"] = ("全部源失败: "
                        + "; ".join(f"{a['source']}({a.get('error')})"
                                    for a in res["attempts"]))[:300]
        _write_status(res, old, now)
        return res

    TABLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(TABLE_PATH)
    res["ok"] = True
    _write_status(res, merged, now)
    log(f"[ATR表] {len(merged)} 行, 最新 {merged.index.max().date()} "
        f"(本轮 +{res['added']}, 首选源 {res['source']})")
    return res


def _write_status(res: dict, df: pd.DataFrame | None, now: datetime) -> None:
    stale, last = table_freshness(df, now.date())
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps({
        "at": now.isoformat(timespec="seconds"), **res,
        "table_rows": 0 if df is None else len(df),
        "table_last_date": None if last is None else str(last.date()),
        "stale_days": None if stale == 9999 else stale,
    }, ensure_ascii=False, indent=1), encoding="utf-8")


# ===========================================================================
# 告警 (及时提醒: 桌面通知, 非 darwin / 无 osascript 时静默降级为仅日志)
# ===========================================================================
def notify_desktop(title: str, msg: str) -> bool:
    if sys.platform != "darwin":
        return False
    msg = msg.replace('"', "'").replace("\\", "")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Ping"'],
            timeout=10, check=False, capture_output=True)
        return True
    except Exception:
        return False


# ===========================================================================
# CLI
# ===========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="NDX 日线 ATR 表更新/查看")
    ap.add_argument("--full", action="store_true", help="强制全量 bootstrap")
    ap.add_argument("--status", action="store_true", help="只看上次更新状态")
    ap.add_argument("--atr", action="store_true", help="打印当前 ATR 与新鲜度")
    args = ap.parse_args()

    if args.status:
        print(STATUS_PATH.read_text(encoding="utf-8") if STATUS_PATH.exists()
              else f"无状态文件 {STATUS_PATH}")
        return 0

    if args.atr:
        df = load_table()
        stale, last = table_freshness(df)
        atr = atr_from_table(df, datetime.now(ET).date())
        print(f"表: {'缺失' if df is None else f'{len(df)} 行'} | 最新 {last} | "
              f"陈旧 {stale} 天 (上限 {ATR_STALE_DAYS}) | 今日 ATR = {atr}")
        return 0 if atr is not None and stale <= ATR_STALE_DAYS else 1

    res = update_table(full=args.full)
    if not res["ok"]:
        msg = f"NDX ATR 更新失败: {res['error']}"
        print(f"!! {msg}", file=sys.stderr)
        notify_desktop("ORB atr_source", msg + " —— 请检查网络/数据源")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
