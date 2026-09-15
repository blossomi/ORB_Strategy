# v5.0 — ORB 主线（FSM 显式状态机架构）

> 策略语义 = v8.4 零变化（信号/定价/BE/仓位/成本逐行照抄）；变的是**代码架构**：
> 状态管理、回测/live 关系、性能。代码版本从本目录起记为 v5.0。
> 上一代代码（v8.4 单文件回测 + live/ 手写状态）全量归档于 `../archive/`，逐笔基线可复验。

## 1. 目录

| 文件 | 角色 |
|---|---|
| `orb_fsm.py` | **状态机核心**（纯 Python，无 nautilus 依赖）。状态 `FLAT → PENDING_ENTRY → IN_POSITION → FLAT` + 安全迁移；环境端口 `FsmEnv`（权益/ATR/区间/收盘语义——回测/live 差异唯一收口处）+ 命令端口 `FsmCommands`（下单/改单/撤单/平仓/闹钟）。**回测与 live 共用这一个文件** |
| `orb_backtest.py` | 回测主线：FSM 适配层 + 快数据管道 |
| `orb_live.py` | live 主线：FSM 适配层 + IB 接入 + 滑点记录；**P1-1/P1-2/P2-1 已实现** + 三个原版没有的防护 |
| `slippage_tracker.py` | 滑点记录（自持副本，源 = archive/live/） |
| `test_fsm.py` | 11 组纯 FSM 单元测试（秒级，不需要引擎） |
| `verify_live.py` | 引擎内回归 A/B/C：v5.0 live vs 原版（archive/live）**同数据双跑逐笔 diff** |
| `parity_check.py` | 回测逐笔 CSV 裁判：v5.0 输出 vs 归档原版基线（archive/ORB_strategy/html_output/v8_4_trades_25000.csv） |
| `data/` | 自持数据：`nq_5min_rth.parquet` + `nq_5min_eth.parquet`（不进库；重建 = 从 archive/ORB_strategy/ 拷） |
| `results/` | 回测产物 CSV（进库） |
| `logs/` | live 运行日志（每次运行一个文件，从不覆盖；不进库） |

**回测与 live 的文件关系**：策略决策只有一份（`orb_fsm.py`），两个入口都 import 它；
只属于某一侧的行为（数据来源、收盘语义、IB 参数、日志）在各自的适配层文件里。
改 `orb_fsm.py` = 同时改两边，必须跑完整验证链；只想动某一侧，改对应适配层。

## 2. 跑法

> 环境安装（uv 全流程、全新机器）见根 [README §1](../README.md)；用 pixi 的一键环境见根 [README §1b](../README.md)（`pixi run test|backtest|verify|check-deps`，跨 mac/Linux 同一份 lockfile）。`data/*.parquet` 不进 git，clone 后拷入 `v5.0/data/` 才能跑回测。

```bash
cd v5.0
../.venv/bin/python test_fsm.py          # 单测 11 组（秒级）
../.venv/bin/python orb_backtest.py      # 回测 ~10s → results/v5_trades_25000.csv
../.venv/bin/python parity_check.py      # 逐笔 vs 归档基线（基线是 2020/7R/10:30 口径，
                                         #  磁盘参数变了必然 FAIL —— 见 §3）
../.venv/bin/python verify_live.py A     # live 回归（B=分笔+GTD，C=半日市）
../.venv/bin/python orb_live.py          # 实盘（默认 DRY_RUN，先连 IB Gateway）
```
纪律：改过 `orb_fsm.py` → ①②③④ 全跑；只改适配层 → 至少 ④ + 相关入口。
改回测参数先重跑归档原版刷新 parity 基线（两边同参数才有可比性）。
**注**：`orb_backtest.py` 顶部参数块是**使用者手动改的活页**（HEAD = 锁定推荐 2019 起 / 5R / 10:10 → 1,951 笔 / 年化 72.5% / MDD -28.3%），跑出来的数字随它走 —— 引用前先确认参数块。

## 3. 验证结果（2026-09-15，搬迁后全链重跑全绿）

**回测 parity**（磁盘参数态 = 实验态：2020 起 / 7R / 窗口至 10:30 / MNQ $2 / 0.7% / 7.5%ATR / 1 tick）：
```
✔ PARITY PASS — 1,713 笔逐字段全同 (Σpnl 基线 631,612.00 vs 候选 631,612.00, 差 +0.0000)
```
计数器全同（BE 234 / 初始止损 1,385 / 保本止损 35 / 收盘 293 / 整除跳过 2）；
新防护触发 = 0（干净数据断言）。

> ⚠️ 上面这段是**当时实验态参数**（2020/7R/10:30）的记录。2026-09-15 磁盘参数已回退到锁定推荐
> （2019/5R/10:10），并在该口径下重跑了同一套逐笔 parity：**1,951 笔逐字段全同，Σpnl 1,594,830.00**。
> 但默认基线 `archive/…/v8_4_trades_25000.csv` 仍是 2020/7R/10:30 口径的产物 —— 所以磁盘参数
> 不是那一套时，`parity_check.py` 直接跑**必然 FAIL**（配置不同，不是回归）。要比对就用同参数重跑
> 归档原版生成新基线（`pixi run parity <基线> <候选>` 可显式传路径）。

**live vs 原版（verify_live.py 同引擎双跑）**：
- A 常规窗口 2020Q1：64 笔逐笔全同；BE/出场分类一致（= 旧 TODO·P2-2 的 D 组）；止损单 1:1；
  EOD 闹钟登记 64 天、bar 正常时 0 次抢跑（= 旧 TODO·P2-2 的 E 组）
- B 超大手数逼分笔（$5,000 万 / 1% / 20,000 手帽）：32 笔逐笔全同；止损 1:1；STOP 单 TIF 全部 GTD
- C 半日市（2025-11~12）：27 笔逐笔全同；命中日全部 12:50 平仓

**性能**（全样本 3 次均值）：原版 12.7s → v5.0 **10.0s（-21%）**；
数据管道 ~4.9s → 0.9s（parquet 读 4→1 次、ET 时间预计算字典、免 from_str 解析）；
引擎 ~7.5s 不动（语义等价的红线）。

## 4. v5.0 相对旧版补上的状态缺口

| 项 | 场景 | 旧版行为 | v5.0 行为 |
|---|---|---|---|
| P1-1 EOD 闹钟 | bar 流断（断线/延迟），持仓过 15:55 | 裸奔到下根 bar 或隔夜 | `set_time_alert(flat_at+5min+2s)` 幂等平仓；`on_stop` cancel_timer |
| P1-2 隔夜残留 | 换日时 `net_position≠0` | 只撤单不平仓，裸奔一整天 | 撤单 + 市价强平 + error 告警 |
| P2-1 止损 GTD | 平仓失败后止损单跨日残留 | GTC 永远挂着 | `flat_at+2min` 过期，双保险 |
| 止损单死亡 | 持仓中止损被撤/被拒/过期 | 无事件处理，静默裸奔 | 立即重挂 + error |
| 迟到入场成交 | live 的 fill 在 EOD 后才到 | 挂已过期的止损单再裸奔 | 立即平仓 + 告警 |
| 止损部分成交 | 超大手数分笔触发止损 | 出场计数虚增 | 部分只减仓，全成交才计一次 |

**实测彩蛋**（重构有效性的现场证据）：2020-02-17 总统日半日市不在旧版硬编码 `HALF_DAYS`
里——旧版会持仓裸奔到 18:00 夜盘 bar 才平（-$863），v5.0 的 P1-1 闹钟 16:00:02 平掉
（+$93）。半日历靠人维护必有遗漏，闹钟是兜底的兜底。

## 5. 版本口径（引用数字前必读）

- **代码版本 v5.0**：架构号；**策略语义 = v8.4**，edge 零变化（parity 逐笔全同是证明）。
- **磁盘参数 = 实验态**（2020 起 / 7R / 10:30 / 1 tick），**非锁定推荐参数**
  （2019 起 / 5R / 10:10）。两个口径的数字不能混比，详见 notebook 持久结论 A「窗口口径纪律」。
- 本目录的架构与验证史（重构过程抓到的 3 个自引入 bug 等）见 git 历史
  （GLM_working/orb_v84s → v5.0）。

## 6. 下一步

- live DRY_RUN 在 v5.0/orb_live.py 上重启（旧 TODO·P2-3 的 bar 节奏核对依旧适用；
  操作手册 = archive/live/OPERATIONS.md，机制不变，注意路径已归档）。
- 后续策略研究的新实验直接基于 `orb_fsm.py` 写适配层，状态逻辑不再各养一份。
- archive/ctrader（C# 移植）的两个 P1（多日 ATR 冻结 / `_selfClosePending` 泄漏）
  对应本 FSM 的 `on_new_day` ATR 刷新与出场路径显式化，移植时可抄 FSM 迁移表。
