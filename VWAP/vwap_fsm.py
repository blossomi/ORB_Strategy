# -*- coding: utf-8 -*-
"""
vwap_fsm.py — VWAP Trend Trading 策略显式状态机核心 (纯 Python, 无引擎依赖)
==========================================================================
论文来源: Zarattini & Aziz (2023-11) "Volume Weighted Average Price (VWAP):
The Holy Grail for Day Trading Systems?" — 策略语义:

  - VWAP = Σ(typical×vol)/Σ(vol), typical=(H+L+C)/3, 按锚定窗口日累计
    (rth 锚: 当日 RTH 首 bar 起; eth 锚: 前一日 18:00 ET 起, 含隔夜)
  - 入场: 当日第 entry_delay_bars 根 RTH bar 收盘, 收盘>VWAP 做多 / <VWAP 做空
    (入场价 = 该 bar 收盘价, 论文 = 9:31:00 价)
  - 出场: 某根 bar **收盘** 穿到 VWAP 对岸 (可加 buffer_ticks 缓冲) → 离场;
    always_in 模式同 bar 收盘反手进场 (论文语义: 全天始终在场)
    first_only 模式: 当日第一笔成交后不再入场
  - EOD: 当日最后一根 RTH bar 收盘平仓 (半日市按实际最后 bar)
  - midday_flat: 12:00-15:00 ET 不持仓 (论文 §5: 该时段无趋势特征)

与 orb_fsm.py 的关系: 同一架构风格 (冻结参数 dataclass 无默认值 / Env-Commands
端口 / FSM 只决策适配层只执行), 但 VWAP 策略没有挂单 —— 全部信号与成交都在
bar 收盘时刻确定性发生, 回测无 PENDING 态; live 适配层如毕业需自行加异步态。

⚠️ 价格全部为**原始(未回溯调整)价格**: 数据管道已在加载时用 close−close_raw
   逐 bar 还原 (加法调整的精确逆变换)。止损距离/名义价值/入场价因此与真实
   tick 对齐 —— 名义定仓 (paper sizing) 若用调整后价格会被 D 漂移污染。
"""
from dataclasses import dataclass
from datetime import date, time
from math import floor

LONG = "LONG"
SHORT = "SHORT"
FLAT = "FLAT"

# sizing 模式
SZ_FIXED = "fixed"    # 固定手数 (手数与止损距离无关; 论文法在期货上的最简等价)
SZ_PAPER = "paper"    # 论文法: 全部权益 × paper_lev, 无杠杆超额 → floor(E×lev/(px×mult))
SZ_RISK = "risk"      # 以损定仓: floor(E×risk_pct/(stop_dist×mult)), stop=|入场−VWAP|

# 锚定
ANCHOR_RTH = "rth"
ANCHOR_ETH = "eth"


# ===========================================================================
# 参数 (刻意无默认值: 漏传 = TypeError, 防 "第三份参数拷贝" 事故, 同 orb_fsm)
# ===========================================================================
@dataclass(frozen=True)
class VwapParams:
    tick: float                        # 0.25
    multiplier: float                  # $/点 (MNQ 2.0)
    anchor: str                        # 'rth' | 'eth'
    entry_delay_bars: int              # 1 = 论文 (第 1 根 RTH bar 收盘即判)
    mode: str                          # 'always_in' | 'first_only'
    buffer_ticks: float                # 出场穿越缓冲 (tick 数, 0 = 论文原味)
    sizing: str                        # 'fixed' | 'paper' | 'risk'
    fixed_qty: int                     # sizing=fixed 时的手数
    paper_lev: float                   # sizing=paper 时的目标名义杠杆
    risk_pct: float                    # sizing=risk 时单笔风险占权益比
    min_stop_atr_frac: float           # 最小入场止损距离 = frac×ATR(14); 0 = 不过滤
    max_qty: int
    max_notional_lev: float            # 名义杠杆硬帽 (risk sizing 的旋钮帽)
    midday_flat: bool                  # 12:00-15:00 ET 强制空仓
    side: str                          # 'both' | 'long_only'
    t_midday_start: time               # [start, end) 左闭右开, 转分钟整数比较
    t_midday_end: time

    def __post_init__(self):
        if self.anchor not in (ANCHOR_RTH, ANCHOR_ETH):
            raise ValueError(self.anchor)
        if self.mode not in ("always_in", "first_only"):
            raise ValueError(self.mode)
        if self.sizing not in (SZ_FIXED, SZ_PAPER, SZ_RISK):
            raise ValueError(self.sizing)
        if self.side not in ("both", "long_only"):
            raise ValueError(self.side)


# ===========================================================================
# 端口
# ===========================================================================
class VwapEnv:
    """FSM 对外部的只读依赖。"""

    def equity(self) -> float:
        raise NotImplementedError

    def atr_for(self, d: date) -> float | None:
        """前一日日线 ATR(14) (原始价格点)。None → min_stop 过滤按不过滤处理。"""
        raise NotImplementedError


class VwapCommands:
    """FSM → 适配层的全部动作。全部在 bar 收盘时刻同步执行 (回测语义)。"""

    def enter_market(self, side: str, qty: int, px: float, reason: str) -> None:
        raise NotImplementedError

    def exit_market(self, px: float, reason: str) -> None:
        raise NotImplementedError


# ===========================================================================
# FSM
# ===========================================================================
class VwapFsm:
    """逐 RTH bar 决策。VWAP 数值由适配层预计算后随 bar 传入 (含当前 bar)。"""

    def __init__(self, params: VwapParams, env: VwapEnv, cmds: VwapCommands):
        self.p = params
        self.env = env
        self.cmds = cmds
        # 午间窗口预转分钟整数 (热循环里避免 time 对象比较)
        self._mid_start = params.t_midday_start.hour * 60 + params.t_midday_start.minute
        self._mid_end = params.t_midday_end.hour * 60 + params.t_midday_end.minute
        self.reset_day_state()
        # 计数器 (干净数据断言用, 同 orb 风格)
        self.n_entries = 0
        self.n_exits_cross = 0
        self.n_exits_eod = 0
        self.n_exits_midday = 0
        self.n_skip_tight_stop = 0
        self.n_skip_cant_afford = 0
        self.n_capped_lev = 0
        self.n_capped_qty = 0

    def reset_day_state(self) -> None:
        self.pos = FLAT
        self.trades_today = 0          # 已完成的入场 (first_only 的闸门)
        self.entry_stop_dist = None    # 当前仓位的 |入场−VWAP| (适配层在 enter 时读取)
        self.day = None

    def on_new_day(self) -> None:
        self.reset_day_state()

    # ---------------- 定价 ----------------
    def _size(self, side: str, px: float, vwap: float) -> tuple[int, float] | None:
        """返回 (qty, stop_dist) 或 None (跳过入场)。"""
        p = self.p
        stop_dist = abs(px - vwap)
        min_stop = 0.0
        if p.min_stop_atr_frac > 0:
            atr = self.env.atr_for(self.day)
            if atr is not None:
                min_stop = p.min_stop_atr_frac * atr
        if stop_dist < max(min_stop, p.tick):
            self.n_skip_tight_stop += 1
            return None
        if p.sizing == SZ_FIXED:
            qty = p.fixed_qty
        elif p.sizing == SZ_PAPER:
            equity = self.env.equity()
            qty = floor(equity * p.paper_lev / (px * p.multiplier))
        else:  # SZ_RISK
            equity = self.env.equity()
            qty = floor(equity * p.risk_pct / (stop_dist * p.multiplier))
            cap_lev = floor(equity * p.max_notional_lev / (px * p.multiplier))
            if qty > cap_lev:
                if cap_lev < qty:
                    self.n_capped_lev += 1
                qty = cap_lev
        if qty > p.max_qty:
            self.n_capped_qty += 1
            qty = p.max_qty
        if qty < 1:
            self.n_skip_cant_afford += 1
            return None
        return qty, stop_dist

    def _try_enter(self, side: str, px: float, vwap: float, reason: str) -> None:
        p = self.p
        if p.side == "long_only" and side != LONG:
            return
        sized = self._size(side, px, vwap)
        if sized is None:
            return
        qty, stop_dist = sized
        # ⚠️ 先落 stop_dist 再发命令: 适配层的 enter_market 会同步读取它
        self.entry_stop_dist = stop_dist
        self.cmds.enter_market(side, qty, px, reason)
        self.pos = side
        self.n_entries += 1
        self.trades_today += 1

    # ---------------- 逐 bar 决策 ----------------
    def on_bar(self, d: date, t_min: int, bar_idx: int, c: float, vwap: float,
               is_eod: bool) -> None:
        """t_min = 当地(ET)钟点分钟数 (9:30 → 570)。is_eod 由适配层按当日实际最后 bar 给出。"""
        p = self.p
        self.day = d

        # 1) EOD: 当日最后一根 bar 收盘平仓 (半日市 = 实际最后 bar)
        if is_eod:
            if self.pos != FLAT:
                self.cmds.exit_market(c, "eod")
                self.pos = FLAT
                self.n_exits_eod += 1
            return

        # 2) 午间空仓窗口 [12:00, 15:00)
        in_midday = p.midday_flat and (self._mid_start <= t_min < self._mid_end)
        if in_midday:
            if self.pos != FLAT:
                self.cmds.exit_market(c, "midday")
                self.pos = FLAT
                self.n_exits_midday += 1
            return

        buf = p.buffer_ticks * p.tick
        # 3) 持仓: 收盘穿对岸 (含缓冲) → 离场; always_in 同 bar 反手
        if self.pos == LONG and c < vwap - buf:
            self.cmds.exit_market(c, "vwap_cross")
            self.pos = FLAT
            self.n_exits_cross += 1
            if p.mode == "always_in":
                self._try_enter(SHORT, c, vwap, "reverse")
            return
        if self.pos == SHORT and c > vwap + buf:
            self.cmds.exit_market(c, "vwap_cross")
            self.pos = FLAT
            self.n_exits_cross += 1
            if p.mode == "always_in":
                self._try_enter(LONG, c, vwap, "reverse")
            return

        # 4) 空仓: 入场判定 (第 N 根 bar 起每根尝试, 直到成交;
        #    first_only 当日已成交过 → 不再入场)
        if self.pos == FLAT and bar_idx >= p.entry_delay_bars - 1:
            if p.mode == "first_only" and self.trades_today >= 1:
                return
            if c > vwap:
                self._try_enter(LONG, c, vwap, "entry")
            elif c < vwap:
                self._try_enter(SHORT, c, vwap, "entry")
