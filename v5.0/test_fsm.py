# -*- coding: utf-8 -*-
"""
test_fsm.py — orb_fsm.py 单元测试 (纯 Python, 无 nautilus, 秒级)
================================================================
状态机重构的核心价值: 原版的状态逻辑 (部分成交/BE 竞态/隔夜残留/EOD 幂等) 只能
拉起整个回测引擎才能间接观察, 这里全部变成可直接断言的显式用例。

跑法: ../.venv/bin/python test_fsm.py   (v5.0 在仓库根, 兼容 pytest: pytest test_fsm.py)
"""
from datetime import date, time

from orb_fsm import FLAT, HOLD, PENDING, EntryPlan, FsmCommands, FsmEnv, FsmParams

D = date(2026, 9, 15)

# ---------------------------------------------------------------------------
# 测试自持参数 (orb_fsm.FsmParams 已改为全字段必填, 不再有默认值)
#   刻意与生产参数解耦: 这些是**重构当时的确切值**, 让断言(7R/10:30 等)保持原样,
#   不与 orb_backtest::FSM_PARAMS / orb_live 的锁定推荐(5R/10:10)耦合。
#   要覆盖生产口径, 另写用例并显式传参 —— 不要改这份。
# ---------------------------------------------------------------------------
TEST_PARAMS = FsmParams(
    tick=0.25, multiplier=2.0, risk_per_trade=0.007, atr_stop_fraction=0.075,
    max_qty=200, leverage_cap=None, be_r_multiple=7.0, be_buffer_ticks=0,
    be_use_nominal_r=True, adjust_stop_to_risk=True,
    t_win_start=time(9, 30), t_win_end=time(10, 30),
)


# ---------------------------------------------------------------------------
# 假件
# ---------------------------------------------------------------------------
class FakeCmds(FsmCommands):
    def __init__(self, fsm=None, sync_fill_px=None):
        self.calls = []                    # ("方法", kwargs...) 顺序记录
        self.fsm = fsm                     # 模拟回测引擎同步成交
        self.sync_fill_px = sync_fill_px   # submit 后立刻回调的成交价

    def _rec(self, name, **kw):
        self.calls.append((name, kw))

    def submit_entry_market(self, side, qty, ref):
        self._rec("entry", side=side, qty=qty, ref=ref)
        if self.sync_fill_px is not None:  # 回测引擎: submit 内同步成交 (重入!)
            self.fsm.on_entry_fill(self.sync_fill_px, qty, ts_ns=1)

    def place_stop_market(self, exit_side, qty, trigger, ref):
        self._rec("stop", exit_side=exit_side, qty=qty, trigger=trigger, ref=ref)

    def resize_stop(self, qty):
        self._rec("resize", qty=qty)

    def modify_stop_trigger(self, trigger):
        self._rec("be", trigger=trigger)

    def cancel_all(self):
        self._rec("cancel_all")

    def flatten_position(self, reason, ref):
        self._rec("flatten", reason=reason)

    def fsm_log(self, msg, level="info"):
        self._rec("log", msg=msg, level=level)

    def names(self):
        return [c[0] for c in self.calls]


class FakeEnv(FsmEnv):
    def __init__(self, equity=1_000_000.0, atr=100.0, rng=(200.0, 100.0),
                 last=time(15, 55)):
        self._equity, self._atr, self._rng, self.last = equity, atr, rng, last
        self.flat_called = False           # be_ok/flatten_now 只允许各一次语义由调用方保证

    def equity(self):
        return self._equity

    def atr_for(self, d):
        return self._atr

    def range_for(self, d):
        return self._rng

    def be_ok(self, d, t):
        return t < self.last

    def flatten_now(self, d, t):
        return t == self.last


def make(params=None, env=None, cmds=None, sync_fill_px=None, audit=True):
    p = params or TEST_PARAMS
    e = env or FakeEnv()
    c = cmds or FakeCmds(sync_fill_px=sync_fill_px)
    f = OrbFsmFactory(p, e, c, audit)
    return f, c


def OrbFsmFactory(p, e, c, audit):
    from orb_fsm import OrbFsm
    f = OrbFsm(p, e, c, audit=audit)
    c.fsm = f
    return f


def bar(f, t, o, h, l, c, d=D, ts=None):
    """便捷: 按 5min bar 时间给 FSM 喂事件 (ts 用递增假值)。"""
    ts = ts or int(t.hour * 100 + t.minute)
    f.on_bar(ts, d, t, o, h, l, c)


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------
def test_sizing_math():
    """以损定仓 + 反推止损的数学: 与原版 _enter 中间量逐项对齐。"""
    f, _ = make()
    # equity 1e6 × 0.7% = $7000 风险预算; ATR=100 → 名义止损 7.5pt → 名义手数
    # 7000/(7.5×2)=466.67 > max_qty 200 → 触发 capped, qty=200
    plan = f.plan_entry("BUY", 150.0, 100.0, 1_000_000.0)
    assert plan.capped and plan.qty == 200
    assert plan.stop_price == 142.5 and plan.nominal_dist == 7.5
    assert f.n_capped == 1

    # 反推: equity 20000 × 0.7% = $140; 7.5pt×$2 = $15/手 → risk_qty 9.33
    # → qty 9, target = 140/(9×2) = 7.78 → tick_round 7.75; 7.75 > actual 7.5 → 生效
    f2, _ = make()
    plan2 = f2.plan_entry("BUY", 150.0, 100.0, 20_000.0)
    assert plan2.qty == 9 and not plan2.capped and not plan2.lot_exact
    assert plan2.actual_dist == 7.75, plan2

    # 整除跳过: 让 risk_qty 恰为整数 → 反推被显式拦掉 (原版 71e4111 的跳过条件)
    # 7.5pt×$2=$15/手, $150 预算 → 恰 10 手
    f3, _ = make()
    plan3 = f3.plan_entry("BUY", 150.0, 100.0, 150 / 0.007)
    assert plan3.lot_exact and plan3.actual_dist == 7.5 and plan3.qty == 10
    assert f3.n_lot_exact == 1

    # 买不起: equity 100 → risk_qty 6.67 手... 不对, $0.7/$15 < 1 手
    f4, _ = make()
    plan4 = f4.plan_entry("BUY", 150.0, 100.0, 100.0)
    assert plan4.qty == 0

    # 反推帽: 预算极大但 qty 恰在 max 之下时 target ≤ 1.5×名义距离
    # 构造: 名义距离 7.5 → 帽 11.25; risk_qty 巨大 → capped 不进反推分支 —— 换个构造:
    # qty = max_qty-1 = 199 → target = budget/(199×2); budget 7000 → 17.59 → min(…, 11.25)
    f5, _ = make()
    plan5 = f5.plan_entry("BUY", 150.0, 100.0, 1_000_000.0)
    # (capped 情形不反推, actual_dist 保持 7.5 —— 对齐原版条件 qty < max_qty)
    assert plan5.actual_dist == 7.5
    print("  sizing math OK")


def test_leverage_cap():
    """名义杠杆帽: 只压手数; 帽子生效时跳过反推止损 (dist 保持名义值), 也能压到买不起。"""
    base = dict(tick=0.25, multiplier=2.0, risk_per_trade=0.007,
                atr_stop_fraction=0.075, max_qty=200, be_r_multiple=7.0,
                be_buffer_ticks=0, be_use_nominal_r=True,
                adjust_stop_to_risk=True,
                t_win_start=time(9, 30), t_win_end=time(10, 30))
    # 现实量级: NQ 26000 点, 权益 $25k, ATR 100 → 名义止损 7.5pt
    #   风险手数 = 25000×0.7%/(7.5×$2) = 11.67; 4x 帽 → 4×25000/(26000×2) = 1.92 手
    f, _ = make(params=FsmParams(leverage_cap=4.0, **base))
    p1 = f.plan_entry("BUY", 26000.0, 100.0, 25_000.0)
    assert p1.qty == 1 and p1.actual_dist == 7.5 and f.n_lev_capped == 1

    # 帽宽松 (50x 不约束) ≡ 无帽: qty 11 + 反推到 8.0, 两者逐项相等
    f2, _ = make(params=FsmParams(leverage_cap=50.0, **base))
    f3, _ = make(params=FsmParams(leverage_cap=None, **base))
    p2 = f2.plan_entry("BUY", 26000.0, 100.0, 25_000.0)
    p3 = f3.plan_entry("BUY", 26000.0, 100.0, 25_000.0)
    assert p2.qty == p3.qty == 11 and p2.actual_dist == p3.actual_dist == 8.0
    assert f2.n_lev_capped == f3.n_lev_capped == 0

    # 帽极紧 (1x): 1 手名义 $52k > $25k → 买不起
    f4, _ = make(params=FsmParams(leverage_cap=1.0, **base))
    assert f4.plan_entry("BUY", 26000.0, 100.0, 25_000.0).qty == 0
    print("  leverage cap OK")


def test_signal_to_position_reentrant():
    """信号 → (回测同步成交重入) → 一张止损单; BE 可在入场当根 bar 触发。"""
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=150.0)
    # 9:30 bar 收盘 205 > 区间高 200 → 信号; 同步成交 150.0... 等等, 成交价应是 bar
    # 收盘价 —— FakeCmds.sync_fill_px 设 205.0 才符合「入场=信号 bar 收盘价」语义
    c.sync_fill_px = 205.0
    # 巨阳线: high 高过 entry+7×7.75 → 入场当根就够 BE (原版允许: 回测 submit 同步成交)
    bar(f, time(9, 30), o=201.0, h=270.0, l=200.5, c=205.0)
    names = c.names()
    assert names[0] == "entry"
    assert "stop" in names, names            # 重入: submit 返回前已挂止损
    assert f.state == HOLD and f.filled_qty == 9
    stop_call = [x for x in c.calls if x[0] == "stop"][0][1]
    assert stop_call["exit_side"] == "SELL" and stop_call["qty"] == 9
    assert stop_call["trigger"] == f.p.tick_round(205.0 - 7.75)   # 197.25
    assert "be" in names, f"入场当根 BE 应触发: {names}"          # h=270 ≥ 205+7×7.75=259.25
    assert f.stop_moved and f.n_be_moves == 1
    assert f.n_entries == 1 and f.entered_today
    print("  reentrant signal→fill→stop→BE OK")


def test_partial_fills_single_stop():
    """部分成交: 第一笔挂止损, 后续只 resize —— 绝不重复 submit (live 踩过的坑)。"""
    f, c = make(env=FakeEnv(equity=20_000.0))
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)   # 信号, 但不同步成交
    assert f.state == PENDING                 # live 异步语义
    f.on_entry_fill(205.0, 3, ts_ns=2)        # 分笔 1
    f.on_entry_fill(205.25, 6, ts_ns=3)       # 分笔 2
    stops = [x for x in c.calls if x[0] == "stop"]
    resizes = [x for x in c.calls if x[0] == "resize"]
    assert len(stops) == 1, "分笔成交绝不能挂第二张止损"
    assert stops[0][1]["qty"] == 3            # 第一笔按累计数量挂
    assert [r[1]["qty"] for r in resizes] == [9]
    assert f.filled_qty == 9 and f.entry_px == 205.0   # 锚=首笔价
    print("  partial fills OK")


def test_be_race_stop_first():
    """同一根 bar 先触 7R 又回落打初始止损: 引擎先处理止损 → FSM 收 stop_fill 在 bar 前。"""
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)   # 入场, stop@197.25
    # 下一根: 引擎处理 bar 时止损已触发 (l < 197.25) → 先收 stop_fill, 再收 on_bar
    f.on_stop_fill(197.25, 9, ts_ns=4)
    bar(f, time(9, 35), o=204.0, h=259.5, l=196.0, c=198.0)   # high 够 7R, 但止损先成交
    assert f.state == FLAT and f.n_stopped == 1 and f.n_be_moves == 0
    assert "be" not in c.names()
    print("  BE race (stop first) OK")


def test_eod_close_and_counters():
    """收盘平仓 + 无交易日/买不起日 计数。"""
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    for t in (time(9, 35), time(10, 0), time(12, 0)):
        bar(f, t, o=205.0, h=208.0, l=204.0, c=206.0)
    assert f.state == HOLD
    bar(f, time(15, 55), o=206.0, h=207.0, l=205.0, c=205.5)   # 当日最后一根
    assert f.state == FLAT and f.n_eod == 1
    flats = [x for x in c.calls if x[0] == "flatten"]
    assert len(flats) == 1 and flats[0][1]["reason"] == "eod"
    assert f.n_no_trade == 0
    # 无信号的一天
    bar(f, time(9, 30), o=150.0, h=151.0, l=149.0, c=150.0, d=date(2026, 9, 16))
    bar(f, time(15, 55), o=150.0, h=151.0, l=149.0, c=150.0, d=date(2026, 9, 16))
    assert f.n_no_trade == 1 and f.n_eod == 1
    # 幂等: 收盘后再来 bar 不重复平
    bar(f, time(15, 55), o=150.0, h=151.0, l=149.0, c=150.0, d=date(2026, 9, 16))
    assert f.n_eod == 1
    print("  EOD close + counters OK")


def test_overnight_residue_p12():
    """P1-2: 换日仍持仓 → 撤单+强平+告警 (原版没有的防护)。"""
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    # 模拟: EOD bar 丢失 (断线), 直接跳到次日 9:30
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0, d=date(2026, 9, 16))
    flats = [x for x in c.calls if x[0] == "flatten"]
    assert any(x[1]["reason"] == "overnight_residue" for x in flats)
    assert f.n_overnight_flattens == 1
    # 强平必须发生在当日新信号之前 (换日处置先于窗口判断), 之后正常重新入场
    names = c.names()
    res_flat = [i for i, x in enumerate(c.calls)
                if x[0] == "flatten" and x[1]["reason"] == "overnight_residue"][0]
    entries = [i for i, n in enumerate(names) if n == "entry"]
    assert len(entries) == 2 and res_flat < entries[1], (res_flat, entries)
    errs = [x for x in c.calls if x[0] == "log" and x[1].get("level") == "error"]
    assert errs, "必须有 error 级告警"
    assert f.n_entries == 2, "残留强平后当日新信号应正常入场"
    print("  P1-2 overnight residue OK")


def test_eod_timer_p11():
    """P1-1: EOD 闹钟幂等 —— bar 先到只记账; bar 断流时兜底平仓。"""
    # 场景 A: 正常日, bar 已平仓 → 闹钟只补记账
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    bar(f, time(15, 55), o=206.0, h=207.0, l=205.0, c=205.5)
    n_flat_before = len([x for x in c.calls if x[0] == "flatten"])
    f.on_eod_timer(ts_ns=999)
    assert len([x for x in c.calls if x[0] == "flatten"]) == n_flat_before
    assert f.n_timer_flattens == 0
    # 场景 B: bar 断流, 16:00:02 闹钟来时仍持仓
    f2, c2 = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f2, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    f2.on_eod_timer(ts_ns=999)
    flats = [x for x in c2.calls if x[0] == "flatten"]
    assert any(x[1]["reason"] == "eod_timer" for x in flats)
    assert f2.n_timer_flattens == 1 and f2.state == FLAT and f2.day_closed
    # 闹钟之后再补来的 EOD bar 不得重复平 (幂等)
    bar(f2, time(15, 55), o=206.0, h=207.0, l=205.0, c=205.5)
    assert f2.n_eod == 1 and len([x for x in c2.calls if x[0] == "flatten"]) == 1
    print("  P1-1 EOD timer OK")


def test_stop_dead_replace():
    """新防护: 持仓中止损单被撤/被拒 → 重挂 (原版会裸奔到收盘)。"""
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    f.on_stop_dead(ts_ns=5, reason="被拒")
    stops = [x for x in c.calls if x[0] == "stop"]
    assert len(stops) == 2, "止损死亡必须重挂"
    assert stops[1][1]["trigger"] == stops[0][1]["trigger"]   # 同触发价
    assert f.n_stop_replaces == 1 and f.state == HOLD
    # EOD 自己撤单后的回音: 已 FLAT, 不得重挂
    bar(f, time(15, 55), o=206.0, h=207.0, l=205.0, c=205.5)
    n_stops = len([x for x in c.calls if x[0] == "stop"])
    f.on_stop_dead(ts_ns=6, reason="撤单回音")
    assert len([x for x in c.calls if x[0] == "stop"]) == n_stops
    print("  stop-dead replace OK")


def test_late_entry_fill():
    """新防护: live 迟到成交 (day_closed 之后才 filled) → 立即平仓。"""
    f, c = make(env=FakeEnv(equity=20_000.0))
    bar(f, time(10, 25), o=201.0, h=206.0, l=200.5, c=205.0)   # 窗口末根才信号
    bar(f, time(15, 55), o=206.0, h=207.0, l=205.0, c=205.5)   # 日终 (仍 PENDING)
    f.on_entry_fill(205.0, 9, ts_ns=8)                          # 16:01 才成交
    flats = [x for x in c.calls if x[0] == "flatten"]
    assert any(x[1]["reason"] == "late_entry_fill" for x in flats)
    assert f.state == FLAT and f.n_late_entry_flattens == 1
    assert not any(x[0] == "stop" for x in c.calls), "迟到成交不得再挂隔夜止损"
    print("  late entry fill OK")


def test_partial_stop_fills():
    """止损单分笔成交 (超大手数): 部分只减仓, 全部成交才计一次出场。"""
    f, c = make(env=FakeEnv(equity=20_000.0), sync_fill_px=205.0)
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    assert f.filled_qty == 9
    f.on_stop_fill(197.25, 4, ts_ns=7)          # 部分止损成交
    assert f.state == HOLD and f.filled_qty == 5, "部分止损成交不得清仓"
    assert f.n_stopped == 0 and f.stop_trigger is not None
    f.on_stop_fill(197.25, 5, ts_ns=8)          # 剩余全部成交
    assert f.state == FLAT and f.filled_qty == 0
    assert f.n_stopped == 1, "分笔止损只计一次出场"
    print("  partial stop fills OK")


def test_no_atr_no_quota():
    """ATR 缺失 → 不占当日名额 (原版行为: 后续 bar 可再试)。"""
    class NoAtrEnv(FakeEnv):
        def atr_for(self, d):
            return None
    f, c = make(env=NoAtrEnv())
    bar(f, time(9, 30), o=201.0, h=206.0, l=200.5, c=205.0)
    assert f.state == FLAT and not f.entered_today and f.n_signals == 0
    bar(f, time(9, 35), o=202.0, h=207.0, l=201.0, c=206.0)
    assert f.n_signals == 0
    print("  no-ATR no-quota OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        print(f"[{fn.__name__}]")
        fn()
    print(f"\n全部 {len(tests)} 组用例通过 ✔")
