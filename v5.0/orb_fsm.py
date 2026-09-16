# -*- coding: utf-8 -*-
"""
orb_fsm.py — v8.4 ORB 策略显式状态机核心 (纯 Python, 无 nautilus 依赖)
====================================================================

动机 (2026-09-15, v8.4 状态管理重构):
  原版 (archive/ORB_strategy/orb_backtes_v8_4.py / archive/live/live_ib_demo.py) 的
  持仓状态散落在十几个平行变量里
  (entered_today / _trade{7字段} / pending_entry / _entry_filled / stop_moved /
  _stop_order ...), 状态转换规则隐含在事件回调的执行顺序里 —— 回测与 live 各养一套,
  已经各自踩过坑 (回测部分成交漏挂止损 → -11R 假尾部; live is_open 误判 → 31 笔挂
  62 张止损单), live 还挂着 3 个已识别未修的状态缺口 (TODO·P1-1/P1-2/P2-1)。

  本模块把策略决策收敛为一个显式有限状态机 (参考 LEAN「单一 Algorithm 类跑回测+
  实盘」与 vnpy 事件引擎的分层):
    - FSM 只做决策: 吃事件 (bar/成交/定时器/换日), 吐命令 (下单/改单/撤单/平仓)
    - 适配层只做执行: 把命令翻译成 nautilus 调用, 把引擎事件翻译回 FSM 事件
    - 环境端口 (FsmEnv): 权益/ATR/区间/收盘语义 —— 回测与 live 各自注入

持仓状态机 (position):
    FLAT ──signal──▶ PENDING_ENTRY ──first fill──▶ IN_POSITION ──stop/EOD──▶ FLAT
                        │ (live 异步, 当日未成交 → 换日复位)
    IN_POSITION ──止损单被撤/被拒──▶ (重挂止损, 失败告警)     ← 原版没有的防护
    IN_POSITION ──换日仍持仓──▶ 强平 + error 告警             ← P1-2
    任意状态 ──EOD 闹钟(16:00:02)──▶ 幂等平仓                 ← P1-1

行为契约 (与原版逐笔对齐, parity_check.py 验证):
  - 信号: 入场窗口内逐根 bar 收盘价 vs 区间高低; 提交入场单才占用当日名额
  - 定价: tick_round 0.25 网格; 以损定仓 floor; MAX_QTY 帽; 整除跳过反推;
          反推止损 ≤ 1.5×名义距离 —— 全部照抄原版数学, 含 epsilon
  - BE: 浮盈触及 N R (bar.high/low) → 收盘时 modify 止损到保本+缓冲, 下一根生效
  - EOD: 回测=当日最后一根 bar; live=flat_at (15:55/半日 12:50) + 定时闹钟双保险
  - 重入安全: 回测引擎在 submit 内同步成交 → on_entry_fill 会在 submit_entry_market
    返回前被调回, FSM 不假设调用后的状态
"""
from dataclasses import dataclass, field
from datetime import date, time
from math import floor

# 持仓状态
FLAT = "FLAT"
PENDING = "PENDING_ENTRY"
HOLD = "IN_POSITION"


# ===========================================================================
# 参数 (与 v8.4「参数开关区」一一对应, 由适配层注入)
#   ⚠️ 全部字段**必须显式传入**, 刻意不给默认值: 本文件只定义决策逻辑, 不持有
#   策略参数的"第三份拷贝"。若给默认值, 新适配层 (新平台/临时脚本) 漏传某字段会
#   静默拿到一个陈旧值 —— 2026-09-15 就发生过 (回测盘上被改成实验态 7R/10:30
#   而实盘是 5R/10:10, 两边语义不一致却零报警)。现在漏传 = TypeError。
#   生产值来源: orb_backtest.py 参数区 / orb_live.py 参数区; 单测用 test_fsm.py
#   的 TEST_PARAMS。
# ===========================================================================
@dataclass(frozen=True)
class FsmParams:
    tick: float
    multiplier: float                  # $/点 (MNQ 2.0)
    risk_per_trade: float
    atr_stop_fraction: float
    max_qty: int
    leverage_cap: float | None        # 名义杠杆帽: qty×入场价×乘数 ≤ cap×权益; None=不设
    be_r_multiple: float
    be_buffer_ticks: int
    be_use_nominal_r: bool             # BE 判定用名义 ATR 距离 (与 csv r_multiple 口径一致)
    adjust_stop_to_risk: bool          # 反推止损
    t_win_start: time
    t_win_end: time

    def tick_round(self, px: float) -> float:
        """取整到 tick 网格 (与原版 tick_round 完全一致)。"""
        return round(round(px / self.tick) * self.tick, 2)


@dataclass
class EntryPlan:
    """一次入场信号的全部定价结果 (原版 _enter 的中间量, 抽出来便于单测)。"""
    qty: int
    stop_price: float
    actual_dist: float        # 实际止损距离 (反推后)
    nominal_dist: float       # 名义 ATR 止损距离 (反推前)
    lot_exact: bool
    capped: bool


# ===========================================================================
# 端口定义 (适配层继承/实现)
# ===========================================================================
class FsmEnv:
    """FSM 对外部的全部只读依赖。回测/live 各自实现 —— 这也是两边语义差异的收口处。"""

    def equity(self) -> float:
        raise NotImplementedError

    def atr_for(self, d: date) -> float | None:
        """前一日 ATR (点)。None → 当日不交易。"""
        raise NotImplementedError

    def range_for(self, d: date) -> tuple[float, float] | None:
        """盘前区间 (high, low)。None → 无区间数据, 不判突破。"""
        raise NotImplementedError

    def be_ok(self, d: date, t: time) -> bool:
        """这根 bar 是否还查 BE (回测: 非当日最后一根; live: t < flat_at)。"""
        raise NotImplementedError

    def flatten_now(self, d: date, t: time) -> bool:
        """这根 bar 是否触发收盘平仓 (回测: == 当日最后一根; live: >= flat_at)。"""
        raise NotImplementedError

    def on_new_day(self, d: date) -> None:
        """换日钩子 (live 适配层重置盘前区间/刷新 ATR; 回测 no-op, 区间来自映射)。"""
        pass


class FsmCommands:
    """FSM → 适配层的全部动作。适配层实现; FSM 不接触任何下单 API。"""

    def submit_entry_market(self, side: str, qty: int, ref: str) -> None:
        raise NotImplementedError

    def place_stop_market(self, exit_side: str, qty: int, trigger: float, ref: str) -> None:
        raise NotImplementedError

    def resize_stop(self, qty: int) -> None:
        """分笔成交 → 已有止损单只改数量 (绝不能重复 submit)。"""
        raise NotImplementedError

    def modify_stop_trigger(self, trigger: float) -> None:
        raise NotImplementedError

    def cancel_all(self) -> None:
        raise NotImplementedError

    def flatten_position(self, reason: str, ref: str) -> None:
        """市价平掉当前净头寸 (回测实现可用引擎 close_all_positions)。"""
        raise NotImplementedError

    def arm_eod_timer(self, d: date) -> None:
        """live: 为新交易日上 EOD 闹钟 (flat_at+5min+2s); 回测: no-op。"""
        pass

    def fsm_log(self, msg: str, level: str = "info") -> None:
        pass


# ===========================================================================
# 状态机
# ===========================================================================
class OrbFsm:
    def __init__(self, params: FsmParams, env: FsmEnv, cmds: FsmCommands,
                 audit: bool = False):
        self.p = params
        self.env = env
        self.cmds = cmds
        self.audit: list[tuple] | None = [] if audit else None

        # ---- 持仓状态 (原版 _trade dict + 平行布尔 的显式化) ----
        self.state = FLAT
        self.entry_side: str | None = None
        self.entry_px: float | None = None   # 首笔成交价 (R/保本 的锚)
        self.r_pts: float | None = None      # 实际止损距离 (反推后, 挂单用)
        self.r_nominal: float | None         # 名义 ATR 距离 (BE 判定可选)
        self.filled_qty = 0
        self.stop_trigger: float | None = None
        self.stop_alive = False              # 止损单在场 (含 SUBMITTED, 对齐 live 的 not is_closed 语义)
        self.stop_moved = False              # 已拉保本
        self.peak_r = 0.0                    # 持仓期浮盈峰值 (记录用)

        # ---- 日状态 ----
        self.day: date | None = None
        self.entered_today = False           # 已提交入场 (当日名额占用)
        self.cant_afford_today = False
        self.day_closed = False              # 收盘平仓/日终记账 已做 (幂等闸)

        # ---- 计数 (与原版同名) ----
        self.n_signals = 0
        self.n_entries = 0
        self.n_be_moves = 0
        self.n_stopped = 0
        self.n_be_exits = 0
        self.n_eod = 0
        self.n_no_trade = 0
        self.n_cant_afford = 0
        self.n_capped = 0
        self.n_lot_exact = 0
        self.n_lev_capped = 0
        # 新防护路径的计数 (干净数据下恒为 0 —— parity 断言用)
        self.n_overnight_flattens = 0
        self.n_stop_replaces = 0
        self.n_timer_flattens = 0
        self.n_late_entry_flattens = 0

    # ---------------- 内部工具 ----------------
    def _log(self, msg, level="info"):
        self.cmds.fsm_log(msg, level)

    def _audit(self, ts_ns, what, **kw):
        if self.audit is not None:
            self.audit.append((ts_ns, what, self.state, kw))

    def _clear_trade(self):
        self.state = FLAT
        self.entry_side = None
        self.entry_px = None
        self.r_pts = None
        self.r_nominal = None
        self.filled_qty = 0
        self.stop_trigger = None
        self.stop_alive = False
        self.stop_moved = False
        self.peak_r = 0.0

    # =========================================================================
    # 事件 ①: bar
    # =========================================================================
    def on_bar(self, ts_ns: int, d: date, t: time,
               o: float, h: float, l: float, c: float) -> None:
        p = self.p

        # 新交易日 (原版: 只重置标记; 本版: 多了残留仓位强平防护)
        if d != self.day:
            self._on_new_day(ts_ns, d)

        # ① 入场窗口: 逐根 bar 收盘价判突破 (对齐原版 on_bar 顺序: 窗口 → BE → 收盘)
        if p.t_win_start <= t < p.t_win_end and not self.entered_today:
            rng = self.env.range_for(d)
            if rng is not None:
                hi, lo = rng
                if c > hi:
                    self._signal("BUY", ts_ns, d, c)
                elif c < lo:
                    self._signal("SELL", ts_ns, d, c)
                # 收盘在区间内 → 等下一根

        # ② 持仓: 记浮盈进度 + BE 检查
        if self.env.be_ok(d, t):
            if self.state == HOLD and self.r_pts:
                if self.entry_side == "BUY":
                    self.peak_r = max(self.peak_r, (h - self.entry_px) / self.r_pts)
                else:
                    self.peak_r = max(self.peak_r, (self.entry_px - l) / self.r_pts)
            self._check_be(h, l)

        # ③ 收盘平仓
        if self.env.flatten_now(d, t):
            self._close_day(ts_ns, d, t, c)

    # =========================================================================
    # 事件 ②: 换日 (P1-2 落地处)
    # =========================================================================
    def _on_new_day(self, ts_ns: int, d: date):
        if self.state != FLAT:
            # TODO·P1-2 (原版遗留): 隔夜残留仓位 —— 昨日收盘平仓失败(bar 缺失/断线)
            # 时仓位带着今天裸奔一整天。这里立即撤单+市价平掉 + error 告警。
            self._log(f"[P1-2] 换日 {d} 但仍持仓 {self.filled_qty} 手 "
                      f"(side={self.entry_side}) → 撤单 + 市价强平", "error")
            self.cmds.cancel_all()
            self.cmds.flatten_position("overnight_residue", f"residue-{ts_ns}")
            self.n_overnight_flattens += 1
            self._clear_trade()
        if self.day is not None:
            self._log(f"[日结] {self.day} 信号 {self.n_signals} | 入场 {self.n_entries} | "
                      f"拉保本 {self.n_be_moves} | 止损出场 {self.n_stopped} | "
                      f"保本出场 {self.n_be_exits} | 收盘平仓 {self.n_eod}")
        self.day = d
        self.entered_today = False
        self.cant_afford_today = False
        self.day_closed = False
        self._clear_trade()      # FLAT 时即复位散字段 (原版依赖 EOD 分支复位, 此处统一)
        self._audit(ts_ns, "new_day", day=str(d))
        self.env.on_new_day(d)             # live: 重置盘前区间 + 刷新 ATR (回测 no-op)
        self.cmds.arm_eod_timer(d)

    # =========================================================================
    # 信号与定价 (原版 _enter 逐行对应)
    # =========================================================================
    def _signal(self, side: str, ts_ns: int, d: date, close_px: float):
        atr = self.env.atr_for(d)
        if atr is None or atr <= 0:
            return                          # 原版: 直接 return, 不占当日名额
        plan = self.plan_entry(side, close_px, atr, self.env.equity())
        if plan is None:
            return                          # actual_dist<=0, 同原版
        if plan.qty < 1:
            self.cant_afford_today = True   # 有突破但买不起 1 手
            return
        self.entered_today = True           # 原版: submit 之后才置位; fill 路径不读它, 先置等价
        self.n_signals += 1
        self.entry_side = side
        self.r_pts = plan.actual_dist
        self.r_nominal = plan.nominal_dist
        self.state = PENDING
        self._audit(ts_ns, "signal", side=side, qty=plan.qty,
                    stop=plan.stop_price, dist=plan.actual_dist)
        # ⚠️ 重入: 回测引擎在 submit 内同步成交, 返回时 state 可能已是 HOLD
        self.cmds.submit_entry_market(side, plan.qty, f"entry-{ts_ns}")

    def plan_entry(self, side: str, entry_px: float, atr: float,
                   equity: float) -> EntryPlan | None:
        """以损定仓 + 反推止损 —— 与原版 _enter 数学逐行一致 (含 epsilon), 便于单测。"""
        p = self.p
        stop_dist = max(p.tick, p.tick_round(p.atr_stop_fraction * atr))
        stop_price = p.tick_round(entry_px - stop_dist) if side == "BUY" \
            else p.tick_round(entry_px + stop_dist)
        actual_dist = abs(entry_px - stop_price)
        if actual_dist <= 0:
            return None

        risk_qty = equity * p.risk_per_trade / (actual_dist * p.multiplier)
        if risk_qty > p.max_qty:
            self.n_capped += 1
        # 名义杠杆帽 (README 已知风险③: 复利后期 0.7% 风险 + 宽止损可达 ~29× 名义)。
        # 只压手数, 不改其他定价; 帽子生效时**跳过反推止损** —— 反推的前提是
        # "风险预算因 floor 没花完", 而被杠杆帽压掉的手数不是 floor 损失, 反推只会
        # 把止损无故放宽 (风险预算并没有多出来)。
        lev_qty = float("inf")
        if p.leverage_cap is not None:
            lev_qty = p.leverage_cap * equity / (entry_px * p.multiplier)
        lev_binding = lev_qty < min(risk_qty, p.max_qty)
        if lev_binding:
            self.n_lev_capped += 1
        qty = int(floor(min(risk_qty, p.max_qty, lev_qty)))
        if qty < 1:
            # 原版: 买不起 1 手 → cant_afford, 不进反推分支 (调用方置标记)
            return EntryPlan(qty=0, stop_price=stop_price, actual_dist=actual_dist,
                             nominal_dist=stop_dist, lot_exact=False,
                             capped=risk_qty > p.max_qty)

        lot_exact = abs(risk_qty - round(risk_qty)) < 1e-6 * max(1.0, risk_qty)
        if lot_exact:
            self.n_lot_exact += 1

        if p.adjust_stop_to_risk and qty < p.max_qty and not lot_exact and not lev_binding:
            target_dist = equity * p.risk_per_trade / (qty * p.multiplier)
            target_dist = min(target_dist, stop_dist * 1.5)
            target_dist = max(p.tick, p.tick_round(target_dist))
            if target_dist > actual_dist:
                stop_price = p.tick_round(entry_px - target_dist) if side == "BUY" \
                    else p.tick_round(entry_px + target_dist)
                actual_dist = abs(entry_px - stop_price)

        return EntryPlan(qty=qty, stop_price=stop_price, actual_dist=actual_dist,
                         nominal_dist=stop_dist, lot_exact=lot_exact,
                         capped=risk_qty > p.max_qty)

    # =========================================================================
    # 事件 ③: 成交回报
    # =========================================================================
    def on_entry_fill(self, px: float, qty: int, ts_ns: int):
        """入场市价单成交 (可分笔)。第一笔挂止损, 后续只改数量 —— 原版 2026-09-03 修复。"""
        first = (self.state == PENDING)
        if first:
            self.entry_px = px
            self.n_entries += 1
            self.state = HOLD
            self._audit(ts_ns, "entry_fill", px=px, qty=qty)
        self.filled_qty += qty

        if self.day_closed:
            # 新防护: live 迟到成交 (EOD 后才 filled) —— 原版会挂一张已过期的止损单
            # 然后裸奔, 这里直接平掉 + 告警。干净回测永远走不到。
            self._log(f"[防护] 入场成交迟到 (day_closed={self.day_closed}) → 立即平仓", "error")
            self.cmds.flatten_position("late_entry_fill", f"late-{ts_ns}")
            self.n_late_entry_flattens += 1
            self._clear_trade()
            return

        exit_side = "SELL" if self.entry_side == "BUY" else "BUY"
        trig = self.p.tick_round(self.entry_px - self.r_pts) if self.entry_side == "BUY" \
            else self.p.tick_round(self.entry_px + self.r_pts)
        if not self.stop_alive:
            self.cmds.place_stop_market(exit_side, self.filled_qty, trig, f"stop-{ts_ns}")
            self.stop_alive = True
            self.stop_trigger = trig
        else:
            self.cmds.resize_stop(self.filled_qty)
        if first:
            self._log(f"入场 {self.entry_side} qty={self.filled_qty} px={px:.2f} "
                      f"止损距离={self.r_pts:.2f}pt, 达 {self.p.be_r_multiple:g}R 拉保本")

    def on_stop_fill(self, px: float, qty: int, ts_ns: int):
        """止损/保本止损成交 → 空仓。超大手数下止损也会分笔成交: 部分成交只减仓
        (剩余止损单量仍在场保护剩余仓位), 全部成交才计一次出场。"""
        if qty < self.filled_qty:
            self.filled_qty -= qty
            self._audit(ts_ns, "stop_partial_fill", px=px, left=self.filled_qty)
            return
        if self.stop_moved:
            self.n_be_exits += 1
        else:
            self.n_stopped += 1
        self._audit(ts_ns, "stop_fill", px=px, be=self.stop_moved)
        self._clear_trade()

    def on_stop_dead(self, ts_ns: int, reason: str):
        """止损单被撤/被拒 (原版没有的事件)。持仓还在 → 立即重挂, 赶不上就裸奔到收盘。"""
        self.stop_alive = False
        if self.state != HOLD:
            return                           # 我们自己 EOD 撤单后的回音, 无需处理
        self._log(f"[防护] 止损单{reason}而仓位还在 → 重挂 {self.filled_qty} 手 "
                  f"@ {self.stop_trigger}", "error")
        exit_side = "SELL" if self.entry_side == "BUY" else "BUY"
        self.cmds.place_stop_market(exit_side, self.filled_qty,
                                    self.stop_trigger, f"restop-{ts_ns}")
        self.stop_alive = True
        self.n_stop_replaces += 1

    # =========================================================================
    # BE (原版 _check_be / _move_stop_to_be)
    # =========================================================================
    def _check_be(self, h: float, l: float):
        if self.state != HOLD or self.stop_moved or not self.stop_alive:
            return
        r = self.r_nominal if self.p.be_use_nominal_r else self.r_pts
        entry = self.entry_px
        if self.entry_side == "BUY":
            if h >= entry + self.p.be_r_multiple * r:
                self._move_stop_to_be()
        else:
            if l <= entry - self.p.be_r_multiple * r:
                self._move_stop_to_be()

    def _move_stop_to_be(self):
        buffer_pts = self.p.be_buffer_ticks * self.p.tick
        be_px = self.p.tick_round(self.entry_px + buffer_pts) if self.entry_side == "BUY" \
            else self.p.tick_round(self.entry_px - buffer_pts)
        self.cmds.modify_stop_trigger(be_px)
        self.stop_trigger = be_px
        self.stop_moved = True
        self.n_be_moves += 1
        self._log(f"浮盈达 {self.p.be_r_multiple:g}R (峰值 {self.peak_r:.2f}R) → "
                  f"止损移到保本 = {be_px:.2f}")

    # =========================================================================
    # 收盘 (原版 EOD 分支 + live _flatten + P1-1 闹钟)
    # =========================================================================
    def _close_day(self, ts_ns: int, d: date, t: time, close_px: float):
        if self.day_closed:
            return                           # live: flat_at 之后的每根 bar 都会进来, 幂等
        self.day_closed = True
        self.cmds.cancel_all()
        if self.state == HOLD:
            self.cmds.flatten_position("eod", f"eod-{ts_ns}")
            self.n_eod += 1
            self._log(f"[收盘] {t:%H:%M} 平仓信号 @ {close_px:.2f}")
            self._clear_trade()
        if not self.entered_today:
            if self.cant_afford_today:
                self.n_cant_afford += 1
            else:
                self.n_no_trade += 1

    def on_eod_timer(self, ts_ns: int):
        """P1-1: clock 闹钟 (flat_at+5min+2s)。bar 流断了也能平仓; 与 bar 路径幂等。"""
        if self.state == HOLD:
            self._log("[P1-1] EOD 闹钟触发 (bar 兜底未到) → 撤单 + 市价平仓", "error")
            self.cmds.cancel_all()
            self.cmds.flatten_position("eod_timer", f"eodt-{ts_ns}")
            self.n_eod += 1
            self.n_timer_flattens += 1
            self.day_closed = True
            self._clear_trade()
        elif not self.day_closed:
            # bar 正常先到 (闹钟是 16:00:02, 常规日 15:55 bar 已平过) → 只补记账
            self.day_closed = True
            if not self.entered_today:
                if self.cant_afford_today:
                    self.n_cant_afford += 1
                else:
                    self.n_no_trade += 1

    # =========================================================================
    def stats(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k.startswith("n_")}
