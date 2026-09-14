# ctrader/ —— ORB v8.4 的 cTrader (C# cBot) 移植

**目标**：把 `ORB_strategy/orb_backtes_v8_4.py`（NautilusTrader 回测主线）逐条复刻成
cTrader 上能跑的 C# cBot，先在 **cTrader 模拟盘**跑几天验证信号与执行，作为 CFD 侧的实盘通道。

**状态（2026-09-14）**：源码完成 + 本地编译零错误零警告；**尚未**在 cTrader 里编译成
`.algo` 与跑回测/模拟盘 —— 那两步需要 cTID 凭据（见「构建 / 运行」）。

---

## 1. 目录

```
ctrader/
├── README.md                 ← 本文（设计 + 保真映射 + 偏差 + 操作步骤）
├── CHANGELOG.md              ← 版本记录（append-only）
├── src/ORB_v8_4/
│   ├── ORB_v8_4.cs           ← cBot 主体（策略逻辑）
│   └── ORB_v8_4.csproj       ← 编译用工程（引用 cTrader 自带 cAlgo.API.dll）
├── tools/
│   ├── compile_check.sh      ← 本地编译校验（不需要凭据）
│   ├── backtest.sh           ← cTrader 引擎回测模板（需凭据）
│   └── run_demo.sh           ← 模拟盘常驻启动模板（需凭据）
├── docs/
│   └── ctrader-cli-notes.md  ← cTrader CLI 事实核查笔记（命令/凭据/坑）
└── runs/                     ← 回测与运行产物（不进版本库）
```

## 2. 策略结构（对照 `.py`）

| 环节 | `orb_backtes_v8_4.py` | cBot 实现 | 一致 |
|---|---|---|---|
| 区间 | `[09:00, 09:30)` 六根 5min 高低点 | 同（`Bars.OpenTime` 时间部分判定，右开） | ✅ |
| 入场窗口 | `[09:30, 10:10)` 逐根收盘价判断 | 同（默认 10:10，`.py` 当前被手动改成 10:30，见 §5） | ✅ |
| 方向 | 收盘 > 区间高 → 多；< 区间低 → 空；区间内等下一根 | 同 | ✅ |
| 每日一次 | `entered_today` 标记 | 同（另有重启场景的持仓/历史去重） | ✅ |
| 止损 | `max(TICK, tick_round(0.075 × 前一日 14 日 Wilder ATR))` | 同（tick = `Symbol.TickSize`） | ✅ |
| 止盈 | 无（持有到收盘） | 无 | ✅ |
| 保本 | 浮盈达 `BE_R_MULTIPLE` R → 止损拉到入场价 + 缓冲 tick，一次性 | 同（`BE_USE_NOMINAL_R=true` 用名义 ATR 距离） | ✅ |
| 平仓 | 当日实际最后一根 5min K 线（标签 15:55） | 标签 15:55 的 K 线收盘平仓 + 16:05 ET tick 级兜底 | ✅ 更严 |
| 仓位 | `floor(equity×0.7% / (止损点数×每点值))`，上限 200 | 同（每点值 = `TickValue/TickSize`，可参数覆盖） | ✅ |
| 反推止损 | `ADJUST_STOP_TO_RISK`（1.5× 夹板 / 手数整除跳过 / 被上限压制跳过） | 同（三条护栏全实现） | ✅ |
| 成本 | 1 tick 滑点 + $0.5/手/边（脚本内折算） | 走真实点差 + 佣金（回测用 CLI 的 `--spread/--commission`） | ⚠️ 见 §4 |

## 3. 时间语义（移植的关键，已核对）

`.py` 的 parquet 标签 = **ET 开盘时间（左标签）**：RTH 每日 78 根、标签 `09:30…15:55`。
cTrader 的 `Bar.OpenTime` 同样是 **K 线开盘时刻**，本 cBot 用
`[Robot(TimeZone = TimeZones.EasternStandardTime)]` 把整个 algo 锁到美东时间 ⇒
两个平台的时段判定可以逐字相同（含 DST）：

- 区间 = 标签 `09:00…09:25` 六根
- 入场 = 标签 `09:30…10:05` 十根（墙钟 9:35 起才有成交，与回测一致）
- 收盘 = 标签 `15:55` 的 K 线（其 close = 墙钟 16:00 价）

启动时会打印 `[时区自检]`（`Server.Time` / `Server.TimeInUtc` / 换算 ET 三者对照）与
`[bar节奏]`（回调名 / 最新 bar / 倒数第二根 / ET 现在 / 判定收盘 bar），
首次回测或首日实盘用它核对回调语义（对齐 `live/` 的 P2-3 纪律）。

## 4. 已知偏差（与回测口径不同，动手前必读）

1. **tick 网格**：`.py` 硬编码 NQ 的 `TICK=0.25`；cBot 用 `Symbol.TickSize`（CFD 常见 0.1/0.01）。
   止损/保本的取整粒度因此不同（量级 ≤1 tick，方向随机）。
2. **每点值**：`.py` 用 MNQ 的 `$2/点`；cBot 用 `TickValue/TickSize` 推算（指数 CFD 通常
   `1 手 = $1/点`，`LotSize` 多为 1）。启动日志会打印该值 + 每笔「预算风险 vs 实际风险」，
   若与券商实际不符可用 `每点每手美元` 参数覆盖。**这是一处必须用真实账户核对的口径。**
3. **成交价**：`.py` 里市价单成交在突破 K 线的**收盘价**；实盘是收盘后第一个 tick。
   偏差 ≈ 半价差 + 滑点，与 `.py` 建模的 1 tick 滑点同量级。
4. **成本量级**：MNQ 1 tick = 0.25 点；NAS100 CFD 点差通常 1~2 点（≈ 4~8 个 NQ tick）。
   2026 年止损中位 ~32 点 → 点差成本约 3~6% 的 1R（MNQ 1 tick 来回约 1.5%）。
   **CFD 侧更贵，不能直接期待同一年化**；跑几天主要是验证信号与执行链路，不是复现收益。
5. **ATR 数据源**：`.py` 用 RTH 5min（9:30-16:00）重采样成日线；cBot 用同一时间窗从**图表
   K 线**聚合（同样排除 CFD 的隔夜 bar）。差异只在**历史预热深度**：回测有 10 年、cTrader
   启动时只加载有限根数，故启动时 `LoadMoreHistory()` 尽力多拉，并打印「日线聚合天数」，
   < 42 天（3×ATR 周期）时告警。
6. **部分成交**：`.py` 曾因大单被拆导致「止损只盖第一笔」的假左尾（已修）。cTrader 侧止损挂在
   整个仓位上（服务器端 SL），结构性不存在该 bug，故 cBot 里没有对应计数。
7. **半日市**：`.py` 按当日实际最后一根 K 线平仓（修复跨夜）。CFD 指数一般不设半日市，
   cBot 用「标签 15:55 平仓 + 16:05 兜底 + 换日残留强平」三重保护覆盖该风险。

## 5. 参数口径（⚠️ 与磁盘上的 `.py` 存在有意差异）

`.py` 当前是**实验态**（`notebook.md` 2026-09-14 末尾条目明确记录）：
起点 2020 / `BE_R_MULTIPLE=7` / `T_WIN_END=10:30`。本 cBot 默认值取**锁定推荐参数**：

| 参数 | cBot 默认 | `.py` 文件当前值 | 锁定推荐（notebook） |
|---|---|---|---|
| 入场窗口结束 | **10:10** | 10:30（实验） | 10:10（10:30 会让终值 −13.5%） |
| 保本倍数 R | **5.0** | 7（实验） | 5.0 |
| 风险 % | **0.7** | 0.7 | 0.7 |
| 止损 | **7.5% × 14 日 ATR** | 同 | 同 |
| 最大手数 | **200** | 200 | 200 |
| 反推止损 | 开 | 开 | 开（可评估关闭） |

要复现实验态：`--BeRMultiple=7 --EntryEndEt=10:30`。参数名即 CLI 的 `--Name=value` 键，
`ctrader-cli metadata <algo>` 可列出全部参数。

## 6. 构建 / 运行

### 6.1 本地编译校验（无需凭据）
```bash
cd ctrader/tools && ./compile_check.sh
```

### 6.2 凭据（一次性，密码由用户自己输入，AI 不接触）
```bash
mkdir -p ~/.ctrader && printf '%s' '你的cTID密码' > ~/.ctrader/pwd && chmod 600 ~/.ctrader/pwd
```
`--ctid` 用 cTID 用户名或邮箱（本机 `~/cTrader/.config/Spotware/Ctid/` 下已存在 `9241041`）。
只读探测（先跑这两条，再碰任何下单命令）：
```bash
export PATH="/opt/homebrew/bin:$PATH"
ctrader-cli accounts --ctid=<cTID> --pwd-file=~/.ctrader/pwd
ctrader-cli symbols  --ctid=<cTID> --pwd-file=~/.ctrader/pwd --account=<账户号>
```

### 6.3 编译成 `.algo`
```bash
ctrader-cli build ctrader/src/ORB_v8_4            # 产出 .algo 后：
ctrader-cli metadata <algo 文件>                  # 确认参数可被 CLI 覆盖
```
（`.algo` 是 cTrader 自己的容器格式，只能用官方工具生成；等价路径是在 cTrader 桌面版
Algo 里新建/粘贴源码再编译。）

### 6.4 cTrader 引擎回测（同引擎、含真实点差与佣金）
见 `tools/backtest.sh`；关键是 `--data-mode=m1`（或 `ticks`）+ 持久化 `--data-dir` 复用数据。

### 6.5 模拟盘跑几天
见 `tools/run_demo.sh`。建议顺序：
1. **DRY_RUN 1 天**（`--DryRun=true`）：只记录信号不下单，核对 `[时区自检]`/`[bar节奏]`/
   `[突破]`/`[信号]` 的时间戳与手数是否符合预期；
2. 再 `--DryRun=false` 实跑，`caffeinate -s` 保持进程存活到 16:05 ET；
3. 每天用 `ctrader-cli deals` / `positions` 对账，产物落 `runs/`。

## 7. 未验证项（诚实清单）

- `.algo` 尚未生成（缺凭据）→ `build` 对我们这种手搓工程目录的接受度待验；
- cTrader 回测尚未跑 → 与 `.py` 的逐笔对齐（信号数/入场时间）未做；
- `TickValue/TickSize` 在目标 CFD 上的每点值未用真实账户核对；
- `Bars.LoadMoreHistory()` 在 CLI 无界面模式下是否生效未验（启动日志会显示实际天数）；
- `OnBarClosed` vs `OnBar` 的实际触发语义以 `[bar节奏]` 日志为准。
