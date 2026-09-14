# -*- coding: utf-8 -*-
"""验证 OrbStrategyConfig 已改为「漏传即报错」。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

import orb_backtes_v8_4 as V

BAR_TYPE = "NQ.GLBX-5-MINUTE-LAST-EXTERNAL"
FULL = dict(
    instrument_id=V.INSTRUMENT_ID, bar_type=BAR_TYPE,
    risk_per_trade=V.RISK_PER_TRADE, multiplier=V.MULTIPLIER,
    atr_stop_fraction=V.ATR_STOP_FRACTION, max_qty=V.MAX_QTY,
    be_r_multiple=V.BE_R_MULTIPLE, be_buffer_ticks=V.BE_BUFFER_TICKS,
)

print("1) 漏传每一个字段, 是否报错:")
for missing in ("risk_per_trade", "multiplier", "atr_stop_fraction",
                "max_qty", "be_r_multiple", "be_buffer_ticks"):
    kw = dict(FULL)
    kw.pop(missing)
    try:
        cfg = V.OrbStrategyConfig(**kw)
        got = getattr(cfg, missing)
        print(f"   漏传 {missing:<18} *** 没报错 ***  -> 静默取值 {got}")
    except Exception as exc:                                    # noqa: BLE001
        print(f"   漏传 {missing:<18} OK 报错: {type(exc).__name__}: {str(exc)[:110]}")

print("\n2) 全部传齐, 是否正常:")
cfg = V.OrbStrategyConfig(**FULL)
for k in ("risk_per_trade", "multiplier", "atr_stop_fraction", "max_qty",
          "be_r_multiple", "be_buffer_ticks"):
    print(f"   {k:<18} = {getattr(cfg, k)}")

print("\n3) 少传一个 (真实的漏传场景):")
try:
    V.OrbStrategyConfig(instrument_id=V.INSTRUMENT_ID, bar_type=BAR_TYPE)
    print("   *** 没报错 —— 改失败了 ***")
except Exception as exc:                                        # noqa: BLE001
    print(f"   OK 报错: {type(exc).__name__}: {str(exc)[:200]}")
