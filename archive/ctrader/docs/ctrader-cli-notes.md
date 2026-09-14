# cTrader CLI 事实笔记（2026-09-14 核实）

来源：官方文档（`help.ctrader.com/ctrader-cli/*`，本机经代理可 curl）+ 本机实测（ctrader-cli 5.9.0.3，Homebrew）。
写下来是为了以后不用重新查：**命令以本机 `ctrader-cli --commands` / `--help` 输出为准**。

## 安装与版本
- `brew tap spotware/tap https://github.com/spotware/homebrew-tap && brew install spotware/tap/ctrader-cli`
  （本机安装时 brew 报过 "untrusted tap" 提示，但实际装成功，二进制在 `/opt/homebrew/bin/ctrader-cli`）
- 版本 `5.9.0.3`；随 CLI 附带 `cAlgo.API.dll` + `cAlgo.API.xml` + `algohost.netcore`：
  `/opt/homebrew/Cellar/ctrader-cli/5.9.0/libexec/`
  → 这两个文件让我们能**离线**做编译校验，并从 XML 里查任意 API 成员的签名/说明。
- cBot 需要 .NET 8；本机 `dotnet` 8.0.301 + 10.0.400 均在。
- cTrader 桌面版（本机 5.9）也自带一份 CLI：右键 cBot 实例 → 「Start in external process」，
  进程在 CLI 里继续跑，桌面版可关闭。

## 认证：两套约定，不能混用
| 模式 | 需要 | 适用命令 |
|---|---|---|
| batch（非交互） | `--ctid` + `--pwd-file` | `periods` `accounts` `symbols` `metadata` `run` `backtest` |
| interactive | `--ctid` + `--password` + `-q` | `account*` `orders` `price` `candles` `positions` `exposure` `alerts` `cbots` `stop` … |

- batch 命令给 `--password` 会报 `Parameter password is not allowed`；反之亦然。
- 无头名（`accounts`/`symbols`/`metadata`/`run`/`backtest`）按传的旗标自动路由两种模式。
- `--pwd-file` 指向一个纯文本文件，里面只有密码（无首尾空格）。**密码不写进命令行、不进版本库。**
- 多券商同账号号时加 `--broker=<name>`。
- 只读探测命令：`ctrader-cli --version`、`periods`（无需登录）、`accounts`、`symbols`、`sessions`。

## cBot 生命周期
```
ctrader-cli create <kind> <name>            # 脚手架（需凭据）
ctrader-cli build <project-path>            # 编译工程 → .algo（需凭据）
ctrader-cli metadata <algo>                 # 列出参数名/类型（CLI 覆盖参数要用这些名字）
ctrader-cli run <algo> --account=.. --symbol=.. --period=.. [<cbotset>] [--Name=value ...]
                    [--full-access] [--exit-on-stop]
ctrader-cli cbots ...                       # 列出运行中的实例
ctrader-cli stop --instance=<id> / stop all yes
ctrader-cli backtest <algo> [<cbotset>] --account=.. --symbol=.. --period=..
                    --start="DD/MM/YYYY HH:mm" --end=".." --data-mode=<mode>
                    [--balance=10000 --commission=30 --commission-type=UsdPerMillionUsdVolume
                     --spread=1 --data-dir=<持久目录> --report --report-json]
```
- `--data-mode`：`open`（快、粗）/ `m1` / `m1-csv` / `tick-csv` / `ticks`（最准最慢）。
- 回测产物写在 `.algo` 同级的 `data/{cBotName}/{实例ID}/Backtesting/`：Events(JSON)、Log(TXT)、Parameters(.cbotset)、Report(HTML)。
- `run` 是长驻进程，需要进程守护（systemd/Docker/`--exit-on-stop`）；其它命令都是一次性返回。
- 无界面下行为差异：`MessageBox` 返回 None、`Window` 忽略、`Notifications.PlaySound` 忽略、`Chart.TakeChartshot` 返回 null。
- Docker 镜像 `ghcr.io/spotware/ctrader-console:latest`，用 `-e CTID/PWD-FILE/ACCOUNT/SYMBOL/PERIOD` + `--environment-variables`。
- cTrader 官方还提供 agent 技能仓库 `spotware/ctrader-skills`（`npx skills add spotware/ctrader-skills --agent '*' --skill ctrader-cli --yes --global`，需 Node ≥22.20）。

## cAlgo API 关键事实（用于写 cBot）
- `Bar` 是**值类型（struct）**，不能 `= null`；`Bars` 没有 `LastValue`/`Last(int)` 可用（用 `Bars[Count-1]` 索引）；
  价格序列（`Bars.ClosePrices`）才有 `Last(n)`。
- `Robot.OnBarClosed()` 文档定义 =「新 bar 打开时对**上一根已收盘**的 bar 调用」；`OnBar()` 是「每根 bar 到达时」。
  两者语义在回测/实盘可能有差异 → 本工程两个回调都接、按 `OpenTime` 去重，并打印 `[bar节奏]` 自检。
- `TimeZone = TimeZones.EasternStandardTime` 可让 algo 内所有时间（`Bar.OpenTime`、`Position.EntryTime`）为 ET，DST 由平台处理。
- `Positions.Closed` 事件带 `PositionCloseReason`（`Closed`/`StopLoss`/`TakeProfit`/`StopOut`）→ 可靠的出场归因。
- `ExecuteMarketOrder` 的止损参数单位是 **pips**（不是价格）；绝对价要 `ModifyPosition(pos, sl, tp)`。
- `Symbol`：`TickSize/TickValue/PipSize/PipValue/LotSize/Digits/Spread/VolumeInUnitsMin/Step/Max`、
  `QuantityToVolumeInUnits(lots)`、`VolumeInUnitsToQuantity`、`NormalizeVolumeInUnits(vol, RoundingMode.Down)`。
  ⚠️ `TickValue/PipValue` 的「每手还是每单位」口径在文档里没写死 → 本工程打印派生值并在实盘核对。
- `History.FindAll(label, symbol)` / `LocalStorage` 可用（重启去重、状态持久化）。
- `Bars.LoadMoreHistory()` 返回新增根数（CLI 无界面模式下是否生效需实测）。

## 坑
- 文档站 `help.ctrader.com` 在本机解析到 198.18.0.x（Clash fake-IP）→ `web_extract` 会以
  "private network address" 拒绝；用 `curl` 抓 HTML 再剥标签可行。
- 所有下单类命令都有 `yes` 后缀用于跳过交互确认（如 `order place-market ... yes`）。
- 「先跑只读命令」：`accounts` / `symbols` / `price` 通过后再动 `run`/`order`。
