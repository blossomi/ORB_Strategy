# GLM_working

GLM 专用代码工作区，与 `ORB_strategy/` 隔离：实验性 / 新写的代码放这里，不污染现有回测脚本。

- 复用根目录 `./.venv`（NautilusTrader 等依赖已装好）
- 数据 parquet 在 `../ORB_strategy/`，只读
- 硬规则见 `../.hermes.md`（回测起点 ≥2016、先查能否买得起 1 手、收盘价判突破、walk-forward 报结果等）
