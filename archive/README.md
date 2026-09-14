# archive/ — v8.4 时代代码全量归档（2026-09-15）

2026-09-15 工作区重组：v5.0（FSM 架构）升为唯一主线，本目录收纳上一代全部代码。

| 目录 | 原位置 | 是什么 |
|---|---|---|
| `ORB_strategy/` | 顶层 | 回测主线 `orb_backtes_v8_4.py` + 数据 parquet + 数据准备脚本 + 33 项旧版本（内层 archive/）+ html_output 产物（**逐笔 parity 基线 = `html_output/v8_4_trades_25000.csv`，v5.0/parity_check.py 在用**） |
| `live/` | 顶层 | 实盘旧主线 `live_ib_demo.py` + `slippage_tracker.py`（v5.0 已拷副本）+ `_verify_live_logic.py` + `OPERATIONS.md`（操作手册，机制仍适用）+ logs/ 运行审计 |
| `GLM_working/` | 顶层 | 搜索工作区：`orb_core_v84.py` 三参数框架 + stage/walk-forward + `results/REPORT.md`（寻优结论权威）+ `vwap_mr/`（VWAP-MR 研究，终判不开坑）+ α/β 回归 |
| `ctrader/` | 顶层 | ORB v8.4 的 C# cBot 移植（编译 0/0；2 个 P1 待修；卡 cTID 凭据） |
| `propfirm/` | 顶层 | prop firm 研究（结论：IBKR 3.6×，prop 仅零本金补充；README 为细节权威） |

**重要性质——相对几何保留**：五个目录同层搬入，旧脚本内的
`HERE.parent / "ORB_strategy"`、`../live` 等相对引用**全部继续解析**（已逐一验证）。
归档脚本理论上仍可运行；但它们引用 orb_backtes_v8_4.py / live_ib_demo.py 的文档路径
（README/CHANGELOG 行文）已过期，以「archive/ 前缀」理解。

**数据**：全部 parquet 仍在 `ORB_strategy/`（gitignore，不进库）。v5.0 自持
`../v5.0/data/`（NQ 5min RTH+ETH 两件套拷贝）。
