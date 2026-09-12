# live/ —— IB 实盘主线代码

未来移植实盘只看这个文件夹，回测/研究在 `../ORB_strategy/`（参数结论见 `../notebook.md`）。

## 文件

- **`live_ib_demo.py`** —— NautilusTrader + IB 实盘框架：连 IB Gateway → 订阅 NQ 5min bar → 跑 ORB 信号 → 下单。ORB 信号逻辑已内嵌（与 `ORB_strategy/orb_backtes_v8_4.py` 对齐，含 6 处上线前修复）。
- **`slippage_tracker.py`** —— 「信号价 vs 实际成交价」滑点记录器，落盘 CSV 校准回测的 SLIPPAGE_TICKS。只依赖标准库。
- **`_verify_live_logic.py`** —— 上线前回归验证：用回测引擎跑同一个 `OrbLiveStrategy` 类，验证信号数/止损单/分笔成交/半日市平仓。**改完策略逻辑必跑**。

## 怎么跑

```bash
cd live && ../.venv/bin/python live_ib_demo.py        # 默认 DRY_RUN，只记信号不下单
cd live && ../.venv/bin/python _verify_live_logic.py  # 回归验证
```

## 前提（踩坑记录详见 notebook.md）

- IB Gateway paper 端口 **4002**（live 4001；TWS 才是 7497/7496），API 类型选 IB API
- paper 账户 `DUQ715008`，client_id=1
- 合约 symbology=IB_SIMPLIFIED：NQ 当月合约 = `NQU6.CME`（venue 是 **CME**）
- **必须开通实时 streaming 行情**（CME 数据包，见 notebook 2026-09-12 条目）：延迟数据只能验证信号，不能测滑点/下单
