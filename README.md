# ORB_Strategy — NQ 期货日内开盘区间突破：研究 → 回测 → 实盘

> 一套围绕 **CME 纳指期货（NQ/MNQ）5 分钟 ORB（Opening Range Breakout）日内策略** 的完整工程：
> 参数研究、NautilusTrader 引擎回测、显式状态机策略核心、IB 实盘执行与回归验证，
> 以及一套「引用任何数字必须带口径」的结论纪律。
> 策略语义定型于 v8.4；当前代码架构版本 **v5.0**（FSM 显式状态机，回测与实盘共用一份决策逻辑）。

| | |
|---|---|
| **阶段** | 回测定型 ✅ → **实盘验证期（DRY_RUN）** ← 当前在这里 |
| **标的** | MNQ（Micro Nasdaq，$2/点），当前合约 MNQZ6（~2026-12-10 换月 H7） |
| **技术栈** | Python 3.12 + NautilusTrader 1.231 + Interactive Brokers（Gateway/API） |
| **回测成绩** | 推荐参数 2019 起：年化 75.4% / MDD -27.8% / Sharpe 1.54（口径纪律见 §5） |
| **实盘状态** | paper 账户，DRY_RUN 待在 v5.0 上重启；真单前门槛见 §7 |

---

## 1. 安装（全新机器，用 uv，从零到跑通）

前提只有一个：**uv**（Rust 写的 Python 包管理器，负责装 Python、建虚拟环境、装依赖，
macOS / Linux 同一套命令）。

```bash
# ① 装 uv（二选一）
curl -LsSf https://astral.sh/uv/install.sh | sh    # 通用脚本（装完重开 shell）
brew install uv                                     # macOS 有 Homebrew 的话

# ② clone 仓库
git clone git@github.com:blossomi/ORB_Strategy.git   # 没配 SSH key 就用 HTTPS 地址
cd ORB_Strategy

# ③ Python 3.12 + 虚拟环境 + 依赖（本机没有 Python 也没关系，uv 会自动下载 3.12）
uv venv .venv --python 3.12
uv pip install -p .venv -r v5.0/requirements.txt     # 5 个包 ~200MB，官方 wheel 免编译

# ④ 拷数据（唯一不在 git 里的东西：data/*.parquet 被 gitignore）
#    从已有机器把 NQ 两件套拷进 v5.0/data/（在本机上就是 cp，跨机器用 scp）
mkdir -p v5.0/data
scp 旧机器:~/ORB_Strategy/archive/ORB_strategy/nq_5min_{rth,eth}.parquet v5.0/data/

# ⑤ 冒烟验证：单测（秒级、零数据依赖）→ 回测（全样本 ~10s）
.venv/bin/python v5.0/test_fsm.py
cd v5.0 && ../.venv/bin/python orb_backtest.py
# 数字随 v5.0/orb_backtest.py 顶部参数块走。HEAD 的锁定口径（2019 起 / 5R / 10:10）
# 应得到：1,951 笔 / $25k→$1,619,830 / 年化 72.5% / MDD -28.3%。对不上先看参数块。
```

到这里回测链路就通了。**实盘（可选）**另需 IB Gateway（headless 安装，paper 端口 4002，
~1GB RAM；真单还要求 CME 实时行情订阅——延迟数据收得到但不能用于下单），运行
`cd v5.0 && ../.venv/bin/python orb_live.py`（默认 DRY_RUN 只记信号不下单），
日常操作手册见 `archive/live/OPERATIONS.md`。

## 1b. 用 pixi 管理环境（可选；多机 / Linux VPS 推荐）

仓库根的 `pixi.toml` + `pixi.lock` 把「Python 版本 + 全部依赖」锁成一份**跨平台**环境：
mac 上解析一次就同时锁定了 Linux（`nautilus_trader-…-manylinux_2_35_x86_64.whl` 已在 lock 里）。
不需要 sudo、不需要 `conda activate`，cron / systemd 里直接 `pixi run <task>`。

```bash
# ① 装 pixi（一次性，装到 ~/.pixi/bin，不需要 sudo）
curl -fsSL https://pixi.sh/install.sh | bash

# ② 在仓库根建环境（按 pixi.lock 复现；本机已缓存 ~6s，全新机器 1-2 分钟）
pixi install

# ③ 日常：四个任务覆盖整条验证链
pixi run test         # 单测（秒级、零数据依赖）
pixi run backtest     # 回测（需 v5.0/data/*.parquet）
pixi run verify       # live 验证网 A/B/C
pixi run check-deps   # 校验 pixi.toml 与 v5.0/requirements.txt 无版本漂移

# ④ 只有要跑 archive/ 里的画图/统计脚本时才需要第二个环境（+matplotlib/scipy）
pixi install -e research

# 环境本体在 .pixi/envs/default/bin/python（已 gitignore，可随时删了重建）
```

**维护约定（防漂移）**
- `pixi.lock` **必须提交**；换机 / CI / cron 用 `pixi install --locked`，不自作主张升级。
- 依赖只有两处声明，改一处必须同步另一处：改完 `pixi.toml` 跑 `pixi run check-deps`，
  它逐包比对 `v5.0/requirements.txt`，不一致直接退出码 1 —— **漂移 = 报错，不靠人记**。
- 升级依赖是显式动作：`pixi update <包名>` → 重跑 `pixi run test && pixi run backtest && pixi run verify`。
- `requirements.txt` 保留：不用 pixi 的机器（或临时容器）仍可 `uv pip install -r` 走通。

**Linux VPS 的两条硬约束**
- 需要 **glibc ≥ 2.35**（Ubuntu 22.04+ / Debian 12+）。`nautilus-trader` 只有
  `manylinux_2_35_*` wheel，低于这个版本会被放弃 → 退回源码编译 → 失败。`pixi.toml`
  的 `platforms` 里已显式声明 `glibc = "2.35"`（不声明时 pixi 按默认 `__glibc=2.28` 拒 wheel，
  这是它首装失败的**唯一**原因，见文件头注释）。
- IB Gateway 不在 pixi 管辖范围：官方 Linux 安装器（自带 JRE）+ Xvfb/IBC + systemd 自行部署。
- 数据仍要单独拷：`data/*.parquet` 被 gitignore（见 §1 第④步）。

## 2. 快速运行（日常）

```bash
cd v5.0
../.venv/bin/python orb_backtest.py      # 回测 → results/v5_trades_25000.csv
../.venv/bin/python parity_check.py      # 逐笔 vs 归档基线（1,713 笔必须全同）
../.venv/bin/python verify_live.py A     # live 逻辑回归（B=分笔+GTD，C=半日市）
```
纪律：改过 `orb_fsm.py`（策略核心）→ 三条 + 单测全跑；只改适配层 → 跑 `verify_live.py` + 相关入口。

## 3. 策略规则（v8.4 语义，五句话讲完）

1. **区间**：盘前 9:00–9:30（ETH 数据，6 根 5m K 线，右开写法 `[9:00, 9:30)` 配 `<`）的最高/最低价。
2. **入场**：9:30–10:10 窗口内逐根 K 线用**收盘价**判突破——收盘 > 区间高做多、< 区间低做空、区间内等下一根；窗口结束无突破当日放弃。
3. **止损**：7.5% × 前一日 14 日 ATR（Wilder，无未来函数）。
4. **保本**：浮盈触及 5R（推荐口径）→ 止损拉到入场价 + 0 tick，一次性，之后持有到收盘。
5. **出场**：无止盈、无 trailing，**持有到收盘**（当日实际最后一根 K 线，半日市提前）；仓位 = floor(权益 × 0.7% / (止损点数 × $2))，复利。

**edge 本体**：胜率只有 ~15-21%、单笔中位 -1.06R；全部利润来自右尾大赢家（P95 +9.6R / P99 +16.2R），左尾被止损封底（≈ -1.1R）。**任何截断右尾的工具都被实测证伪**（10R 止盈、1R 保本、trailing 均负贡献）；回撤来自连续小亏（最大连败 25-41 笔），不是单笔大亏。

## 4. 仓库布局

| 位置 | 内容 |
|---|---|
| [`v5.0/`](v5.0/) | **唯一主线**。`orb_fsm.py` 显式状态机核心（纯 Python，回测/live 共用）+ 两个薄适配层（`orb_backtest.py` / `orb_live.py`）+ 三层验证网（`test_fsm.py` / `verify_live.py` / `parity_check.py`）+ 自持数据 `data/`。详见 [v5.0/README.md](v5.0/README.md) |
| [`archive/`](archive/) | v8.4 时代全部旧代码：ORB_strategy（v1-v8.5 版本史 + 数据 + parity 基线）、live（旧实盘主线 + OPERATIONS.md 运维手册）、GLM_working（参数搜索框架 + 研究结论 REPORT.md + VWAP-MR 研究）、ctrader（C# 移植）、propfirm（prop 研究）。详见 [archive/README.md](archive/README.md) |
| [`notebook.md`](notebook.md) | **项目笔记本（结论唯一权威）**：「当前状态」活页 +「持久结论」A-F 区 + append-only 变更日志。AI 托管 |
| `.venv/` | Python 环境（uv 管理；重建 = 上文安装步骤 ③） |

**架构要点（为什么有 v5.0）**：旧版回测和 live 各养一份手写状态逻辑，先后踩过同类的坑（回测部分成交漏挂止损 → -11R 假尾部；live `is_open` 误判 → 31 笔挂 62 张止损单）。v5.0 把决策收敛为一个 FSM（`FLAT → PENDING_ENTRY → IN_POSITION → FLAT` + 安全迁移），回测/live 只是适配层——**改一处逻辑两边生效，一套验证网兜底**。

## 5. 回测结论（全部数字带口径）

### 5.1 主结果：推荐参数 7.5%ATR × 5R 保本 × 0.7% 风险（MNQ / $25k / 1 tick 滑点 + $0.5 手续费每边）

| 窗口 | 笔数 | 年化 | MDD | Sharpe | 用途 |
|---|---|---|---|---|---|
| **2019 起** | 1,951 | **75.4%** | **-27.8%** | **1.54**（Sortino 6.02，PF 1.38，胜率 15.9%） | 报「当前 regime 可交易性」 |
| 2018 起 | 2,204 | 71.2% | -28.2% | 1.49 | 对照（9 个年度全正） |
| **2016 起** | 2,709 | 56.9% | **-45.7%** | 1.30 | 报「全样本真实水平」（2017 唯一亏损年 -10.0%，全样本 MDD 几乎全部来自它） |

**稳健性**：walk-forward（5 训 1 测 × 6 窗口）**6/6 段样本外盈利**，固定参数不调参最差年 +16%、单年 MDD ≤ -26%；年度分解 2016 起仅 2017 为负（2018 +82.6% / 2019 +125.2% / 2020 +22.4% / 2021 +47.5% / 2022 +201.9% / 2023 +64.5% / 2024 +65.8% / 2025 +11.7% / 2026YTD +60.1%）。

**⚠️ 口径纪律**：2020 起 / 7R / 窗口至 10:30 是磁盘上的**实验态**（$25k→$656,612，年化 63.4%），不是推荐参数；跳过 2017 属于样本选择而非策略改进。**任何两个数字对比前先核对起点/止损/BE/滑点/本金五个口径**。

### 5.2 已验证的关键结论

- **参数面**：止损 5%-10%×ATR 合适（0.5%-1% 灾难性爆仓——紧止损小于单根噪声）；BE 是弱参数（5R 推荐，1-2R 有害，8R+ ≈ 不拉）；风险 0.3%-1.0% 只放大年化/MDD 不动 Sharpe（1.5% 档 MDD -65% 不可接受）。
- **已证伪**：10R 止盈、1R 保本、trailing（负收益不降回撤）、15 分钟 K 线、区间后移到开盘后（edge 大幅衰减）、开盘首根计入区间（PF 1.04 灾难）、**VWAP 均值回归旁支**（NQ+ES 三轮 ~2,800 笔全负，终判不开坑）。
- **成本敏感性（最大的不真实来源）**：滑点按 tick 固定而止损按 ATR 缩放 → 低波动年成本占 1R 比例爆炸（2017 年 1 tick 来回吃掉 ~13% 的 1R）。引擎口径 1→2→3 tick：终值 -34% / -56%。**实盘滑点实测（slippage_tracker）出来多少 tick，就用多少 tick 复测边界档位**。
- **α/β**：全样本 α = **+52.3%/年（p=3.5e-05）**，β = -0.068（不显著），R² = 0.001——收益与大盘方向无关，做空腿有独立价值。
- **分布**：E[R] = +0.42，对数正态 ≫ 正态（Shapiro 拒绝），左尾截断 -1.10R，VaR(1%) = -1.02R——Sortino ≫ Sharpe，**降权 Sharpe、看 Sortino/PF**。
- **反推止损**（`ADJUST_STOP_TO_RISK`）：中位只放宽 +1.75% 却引入两个 R 的口径分裂，可评估关掉（待办）。

### 5.3 硬规则（每条都是踩过的坑，见 `.hermes.md`）

1. 回测起点 ≥2016（2010-2015 数据本身稀疏）；2. 任何回测先查「能否买得起 1 手」（否则零交易段污染样本）；3. 突破判定只用收盘价（high/low 触及有双边歧义）；4. 大单分笔成交时止损必须按累计已成交数量挂（v8.4 部分成交 bug 曾制造 -11R 假左尾）；5. 参数结论必须报 walk-forward，不只报全样本最优；6. 不许为好看调低成本假设。

## 6. 实盘系统（v5.0）

**架构**：FSM 核心 + IB 适配层；三层验证网全绿——回测 1,713 笔逐笔 parity 全同（Σpnl 与归档基线分毫不差）、live 与归档原版同引擎双跑 A/B/C 逐笔全同、11 组状态机单测；全程 12.7s→10.0s（数据管道 ~5×）。

**v5.0 补齐的实盘防护**（旧版全部缺失）：

| 防护 | 场景 | 行为 |
|---|---|---|
| EOD 定时闹钟 | bar 流断线，持仓过 15:55 | `set_time_alert(flat_at+5min+2s)` 幂等平仓 |
| 隔夜残留强平 | 换日仍有仓位 | 撤单 + 市价强平 + error 告警 |
| 止损单 GTD | 平仓失败止损单跨日残留 | flat_at+2min 自动过期 |
| 止损死亡重挂 | 持仓中止损被撤/被拒 | 立即重挂 + 告警 |
| 迟到成交防护 | EOD 后 fill 才到 | 立即平仓，不挂隔夜单 |
| 部分止损减仓 | 超大手数分笔触发止损 | 只减仓，全成交才计一次出场 |

**已知风险（诚实清单）**：① IB paper 成交是模拟的（触价即成、无排队），paper「滑点 ≈0」只是链路下限，**真实滑点必须用小额真单采集**；② 回测 1 tick 滑点假设对末期大仓位（复利后期 150-200 手市价单）明显乐观，是当前数字最大的不真实来源；③ 复利后期杠杆未建模（0.7%+5pt 止损可达 ~29× 名义），待加 min(风险, 保证金) 帽；④ 半日市日历靠人维护（已有 P1-1 闹钟兜底）；⑤ macOS 系统睡眠杀进程 → 必须 `caffeinate -s`，进程须活到 ~16:05 确认平仓。

## 7. 当前阶段与推进门控

```
[已完成] 回测定型（推荐参数 + WF 6/6）→ 代码 v5.0（parity 全绿 + 全部防护落地）
[当前]   ① DRY_RUN 5 个交易日（v5.0/orb_live.py，验证信号时点/手数/止损价 + bar 推送节奏）
   → ② DRY_RUN=false paper 5 天（真实下单链路：止损/分笔改量/EOD 平仓）
   → ③ MNQ 真单 1 手 × 30 笔（采集真实滑点 → 校准回测 SLIPPAGE_TICKS → 边界参数复测）
   → ④ 校准后放大（0.7% 风险复利）
前置链：账户入金 ≥$500 → CME 交易权限 → CME Real-Time 订阅（$1.55/mo，延迟数据不能用于真单）
纪律：DRY_RUN 观察期内不改引擎文件；每完成一项跑 verify_live.py；换月/半日/改码必跑回归。
```

## 8. 路线图

**近期（实盘验证期）**
- 走完 §7 四阶段门控，产出真实滑点样本并校准回测
- 上线即启用失效监控：-45R 硬停机线、41 连亏警报、2 tick 滑点 > 15%×1R 雷达、季度逐笔 diff（分执行损耗 vs edge 衰退）；期望锚 = WF 最差年 +16%，不是全样本

**中期**
- **云部署**：阿里云 US Virginia（零代码改动、无 VPN；2C4G；阻塞项 = Gateway 2FA 重登 + 无心跳告警）——本地 DRY_RUN gate 通过后迁移
- **研究待办**：名义杠杆帽敏感性（论文 4x 帽的期货版）；K1 方向入场 vs 盘前区间突破的同引擎 A/B；ES+NQ 组合权益曲线
- **反推止损关停评估**（`ADJUST_STOP_TO_RISK=False`，降低系统复杂度）

**长期 / 旁支**
- ctrader CFD 通道（`archive/ctrader/`，C# cBot 编译零警告，卡 cTID 凭据；其 2 个待修 P1 可直接抄 v5.0 FSM 的迁移表）
- prop firm 仅作零本金补充通道（已定论：同手数下 IBKR 自有资金 = prop 的 3.6 倍，规则墙吃掉 ~72% edge）

## 9. 文档索引

| 想了解 | 去哪 |
|---|---|
| 结论的唯一权威（策略/数据/实盘/论文/工作区/cTrader） | [notebook.md](notebook.md)「持久结论」A-F |
| v5.0 架构、验证细节、防护语义 | [v5.0/README.md](v5.0/README.md) |
| 旧代码导航（版本史 v1-v8.5、搜索框架、prop 研究） | [archive/README.md](archive/README.md) |
| 实盘每日操作（启动/停止/日志 grep/故障排查） | archive/live/OPERATIONS.md |
| 状态机设计动机与端口语义 | v5.0/orb_fsm.py 模块头注 |
