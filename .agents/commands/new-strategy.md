---
description: 按本项目规范脚手架一个新策略快测工作区（GLM_working/ 下自包含文件夹）
argument-hint: "[策略名，如 breakout_pullback]"
---

用户要新开一个策略研究坑：**$ARGUMENTS**

先加载 `quant-strategy-checklist` skill（所有坑以它为准），然后按以下步骤执行：

## 1. 选址
`GLM_working/<策略名>/` 自包含文件夹：脚本 + `results/` + `README.md`。不碰 `ORB_strategy/`（其 parquet 只读）。

## 2. 脚本骨架（vwap_mr_explore.py 模式）
- **argparse 全参数化**：`--instrument {nq,es}` / `--start YYYY-MM-DD` / 窗口与目标参数按策略语义加 / `--tag` / `--no-save`。默认值 = 基线行为，改参数重跑基线必须 bit-identical。
- **读数**：`ORB_strategy/` parquet，路径从脚本位置推导（`HERE.parent.parent / "ORB_strategy"`），不依赖 cwd；只用 repo 根 `.venv/bin/python`。
- **时间语义红线**：session 归属 `sess = (ts_ET + 6h).date()`（ETH 18:00 起归次日）；窗口扫描用 `start <= t < end` 扫全数组，**禁止按 time-of-day 提前 break**；有效 bar 上界用 `np.max(np.where(valid)) + 1`，**计数不能当索引**；bar 标签 = ET 左标签，区间右开 `[start, end)` 配 `<`，绝不 `<=`。
- **成本口径可切换**：MNQ 往返 1.0pt / ES 0.52pt（2×(佣金+1tick 滑)÷点值），与主线同假设（止损/目标按触发价成交）——假设显式写进 README，结果对真实执行只会更差。
- **输出**：逐笔/网格/分年 CSV 落 `results/`；终端报告含 每日净 R 年化 Sharpe（无交易日记 0，可与 ORB 的 1.54 直接对比）+ 笔级 t（对照失效监控 4.35 量级）+ 买不起/无交易天数统计。

## 3. README.md 结构
背景与问题 / 数据与口径 / 开发坑（审计记录）/ 结果 / 局限与口径注意 / 文件清单 / 复现命令。它是该坑的细节权威，notebook.md 只留一行索引。

## 4. 验收（出结果先做一致性检查）
n 与日期数对得上、胜率×n 与盈亏结构自洽、中位持仓时间合理；**"概率高得离谱"先查代码再下结论**；同参数重跑一次确认确定性。

## 5. 收尾
notebook.md 变更日志加 ≤2 行索引；若结论成立要沉淀，细节进本 README、持久结论只留指针。
