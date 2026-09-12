# live/ —— IB 实盘主线代码

未来移植实盘只看这个文件夹，回测/研究在 `../GLM_working/`（参数结论见 `../GLM_working/results/REPORT.md`）。

## 文件

- **`live_ib_demo.py`** —— NautilusTrader + IB 实盘框架：连 IB Gateway → 订阅 5min bar → 跑 ORB 信号 → 按推荐参数下单。含滑点记录、分笔成交防重复挂止损、半日市平仓、5R 拉保本。
- **`slippage_tracker.py`** —— 「信号价 vs 实际成交价」滑点记录器，落盘 CSV 校准回测的 SLIPPAGE_TICKS。只依赖标准库。
- **`_verify_live_logic.py`** —— 上线前回归验证：用回测引擎跑同一个 `OrbLiveStrategy` 类，验证信号数/止损单比例/分笔成交/半日市平仓/保本逻辑。**改完策略逻辑必跑**（`../.venv/bin/python _verify_live_logic.py`，依次跑 A/B/C）。
- **`OPERATIONS.md`** —— ⭐ **操作手册**：每日启动/停止流程（含 caffeinate）、盘中时间线、如何看日志、阶段推进判据（DRY_RUN→paper→实盘）、故障排查、换月维护。**日常操作看它**。
- **`logs/`** —— 所有运行记录：每次运行一个 `live_<启动时刻>.log`（9:00 起每根 bar 的 OHLCV、区间、突破、信号、成交、拉保本、收盘、日结，墙钟时间戳前缀）+ `nohup_<日期>.log`（控制台全量）。**从不删除**。

## 当前参数（2026-09-12 三参数寻优推荐，见 REPORT.md）

| 参数 | 值 | 说明 |
|---|---|---|
| 合约 | **MNQ**（2026-12, `MNQZ6.CME`） | $2/点；小账户颗粒度好，参数结论与 NQ 一致 |
| 止损 | **7.5% × 前一日 14日ATR**（Wilder） | 启动时一次性拉 120 天日线自动计算（非流式请求，不受 2188 限制）；`ATR_OVERRIDE_PTS` 可手动兜底 |
| 仓位 | **0.7% 权益/笔，以损定仓** | floor 取整，上限 `MAX_QTY=50`（安全帽，实盘验证期满仓前别调大） |
| 保本 | **浮盈达 5R → 止损拉到入场价** | 之后持有到收盘（非 trailing）；触及判断用 bar.high/low |
| 时间 | 区间 9:00-9:29 / 入场 9:30-10:10 收盘价判突破 / 15:55 平仓 | 半日市（11-27、12-24）提前 12:50，每年更新 `HALF_DAYS` |

## 怎么跑

```bash
mkdir -p logs
caffeinate -s nohup ../.venv/bin/python live_ib_demo.py > logs/nohup_$(date +%Y%m%d).log 2>&1 &
tail -f logs/nohup_$(date +%Y%m%d).log
# 收盘后停止: kill -INT $(pgrep -f live_ib_demo)    # 别用 -9，会跳过日结汇总
```

前台裸跑（调试用）：`cd live && ../.venv/bin/python live_ib_demo.py`（默认 DRY_RUN）。

DRY_RUN 日志里核对三样东西：信号方向/时点、手数（=floor(权益×0.7%/止损额)）、日志打印的 ATR 与止损距离。

## 前提（踩坑记录详见 notebook.md）

- IB Gateway paper 端口 **4002**（live 4001；TWS 才是 7497/7496），API 类型选 IB API
- paper 账户 `DUQ715008`，client_id=1
- 合约 symbology=IB_SIMPLIFIED：当月主力合约，如 2026-12 → `MNQZ6.CME`（venue 是 **CME**）。**换月别忘改**：NQ 季月循环（H/M/U/Z），到期=第三个周五，成交量提前 ~1 周滚入下个季月；下次 2026-12-10 前后换 `202703`/`H7`（同时改 `CONTRACT_MONTH` 和 `LOCAL_SYMBOL`）
- **必须开通实时 streaming 行情**（CME 数据包，见 notebook 2026-09-12 条目）：延迟数据只能验证信号，不能测滑点/下单
- `use_regular_trading_hours=False`（默认已配）：盘前 9:00-9:29 的 bar 必须收到，否则区间为空、整天无信号且不报错
