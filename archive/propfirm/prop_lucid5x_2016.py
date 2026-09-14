# -*- coding: utf-8 -*-
"""
prop_lucid5x_2016.py  (propfirm/)
=================================
「N 个 LucidFlex $50K, 考核 rCH / 资金号 rFU」的总资金曲线 → 可交互网页 (ECharts)。

全参数化 —— 常用改法:
  python prop_lucid5x_2016.py                          # 默认 = 5号 / 2016起 / BE=5R / r7→r14
  python prop_lucid5x_2016.py --be 3                   # 3R 保本 (自动重跑引擎, ~90s)
  python prop_lucid5x_2016.py --start 2020-01-01       # 改回测起点 (自动重跑引擎)
  python prop_lucid5x_2016.py --ch-tier 10 --fu-tier 20  # 换两阶段生存笔数 (秒级)
  python prop_lucid5x_2016.py --accounts 3 --fee 92    # 3 个号

参数:
  --start/--end   回测窗口 (默认 2016-01-01 ~ 2026-08-30 = 数据尽头)
  --be            保本倍数 R (默认 5; -1 = 不拉保本)
  --stop-frac     ATR 止损分数 (默认 0.075)
  --ch-tier       考核阶段生存笔数 (默认 7 → 1R = 2000/7 ≈ $286)
  --fu-tier       资金号阶段生存笔数 (默认 14 → 1R = $143)
  --accounts      同时持有的账号数 (默认 5, 完全复制 = 现金流×N)
  --fee           单次进考核费用 (默认 $92)
  --q-cap         手数上限 (默认 50, Lucid micro scaling 未确认)
  --out           输出 html 文件名 (默认按参数自动命名)

引擎结果自动缓存: results/engine_be{BE}_sf{SF}_{start}_{end}.csv
  —— 同 (be, stop-frac, 窗口) 二次运行秒级; 改 BE/止损分数/窗口自动重跑 (~90s)。
注意: 1R 以损定仓 = MLL $2,000 ÷ 生存笔数, q_d = floor(1R/(当日止损pt×$2)) 上限 q-cap;
策略信号零改动 (区间/收盘价入场/持有到收盘), 滑点 1 tick + $0.5/手/边;
MLL 恒 $2,000 / 目标 $3,000 / DLL $1,200 / cons 50% (考核) / payout 50% cap $3,000 (funded)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "GLM_working"))

import prop_sim as ps  # noqa: E402
import prop_full_journey as fj  # noqa: E402

MULT = 2.0
MLL = 2000.0
FIRM = "LucidFlex $50K"


def ensure_engine_data(start: str, end: str, be: float, stop_frac: float) -> pd.DataFrame:
    """引擎逐笔数据 (带缓存): 改 BE/止损分数/窗口 会自动重跑 (~90s)。"""
    cache = HERE / "results" / f"engine_be{be:g}_sf{stop_frac:g}_{start}_{end}.csv"
    if cache.exists():
        print(f"[数据] 缓存命中 {cache.name}")
        return ps.load_trades_engine(str(cache))
    print(f"[数据] 引擎重跑 (BE={be:g}, ATR {stop_frac:.1%}, {start}~{end}) ~90s ...")
    import orb_core_v84 as core
    import prop_be_variants as pbv
    core.configure(multiplier=MULT, slippage_ticks=1.0, capital=25_000)
    data = core.build_data(start, end)
    core.ensure_bars(data)
    df = pbv.run_and_extract(be, data, stop_frac=stop_frac)
    cache.parent.mkdir(exist_ok=True)
    df.to_csv(cache, index=False)
    print(f"[数据] {len(df)} 笔 -> {cache.name}")
    return ps.load_trades_engine(str(cache))


def day_pnl_of(m: pd.DataFrame, r1: float, q_cap: int):
    g = m.groupby("date").agg(pnl_pc=("pnl_pc", "sum"), stop_pt=("stop_pt", "max"))
    g = g.reset_index()
    g["date"] = pd.to_datetime(g["date"]).dt.date
    q = np.minimum(q_cap, np.maximum(1, np.floor(r1 / (g["stop_pt"] * MULT)))).astype(int)
    return g["date"].tolist(), (q * g["pnl_pc"].to_numpy()), q


def main() -> None:
    ap = argparse.ArgumentParser(description="N×LucidFlex $50K 两阶段资金曲线网页")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-08-30")
    ap.add_argument("--be", type=float, default=5.0, help="保本倍数 R (-1=不拉保本)")
    ap.add_argument("--stop-frac", type=float, default=0.075)
    ap.add_argument("--ch-tier", type=int, default=7, help="考核生存笔数")
    ap.add_argument("--fu-tier", type=int, default=14, help="资金号生存笔数")
    ap.add_argument("--accounts", type=int, default=5)
    ap.add_argument("--fee", type=float, default=92.0)
    ap.add_argument("--q-cap", type=int, default=50)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    n_acc, fee = args.accounts, args.fee
    r_ch, r_fu = MLL / args.ch_tier, MLL / args.fu_tier
    be_eff = None if args.be < 0 else args.be
    be_tag = "无BE" if be_eff is None else f"{be_eff:g}R"

    m = ensure_engine_data(args.start, args.end, args.be, args.stop_frac)
    ch_dates, ch_pnl, ch_q = day_pnl_of(m, r_ch, args.q_cap)
    _, fu_pnl, _ = day_pnl_of(m, r_fu, args.q_cap)

    fee_model = dict(kind="per_attempt", per=fee, name=f"LucidFlex ${fee:g}/次")
    res = fj.run_journey(ch_pnl, ch_dates, 0, FIRM, fee_model, fu_pnl=fu_pnl)
    led = pd.DataFrame(res["ledger"], columns=["date", "type", "amount"])
    led["date"] = pd.to_datetime(led["date"])
    led["amount"] *= n_acc                                    # 完全复制 = 现金流 ×N
    daily = led.groupby(led["date"].dt.strftime("%Y-%m-%d"))["amount"].sum()
    cum = daily.cumsum()

    j = pd.DataFrame(res["journeys"])
    j["days"] = j["end"] - j["start"] + 1
    ch, fu = j[j["state"] == "考核"], j[j["state"] == "funded"]
    tot_pay = sum(x["payout"] for x in res["journeys"] if x["state"] == "funded") * n_acc
    tot_fee = res["fees"] * n_acc
    trough = min(float(cum.min()), 0.0)
    trough = trough if trough < 0 else -n_acc * fee           # 首批买号的日内垫资
    first_pos = cum[cum >= 0].index[0] if (cum < 0).any() else daily.index[0]

    print(f"\n{n_acc}×LucidFlex $50K | 考核 r{args.ch_tier} (1R ${r_ch:.0f}) / "
          f"funded r{args.fu_tier} (1R ${r_fu:.0f}) | BE={be_tag} | {args.start} 起")
    print(f"  交易日 {len(ch_dates)} | 考核 {len(ch)} 轮 (过 {len(ch[ch['result']=='通过'])} / "
          f"爆 {len(ch[ch['result']=='爆'])}) | funded {len(fu)} 轮")
    print(f"  总 payout ${tot_pay:,.0f} | 总费用 ${tot_fee:,.0f} | "
          f"期末累计 ${float(cum.iloc[-1]):,.0f} | 最大垫资 ${trough:,.0f}")

    monthly = daily.to_frame("flow")
    monthly.index = pd.to_datetime(monthly.index)
    mbar = monthly.resample("ME")["flow"].sum().round(0)
    stages = [dict(start=str(pd.Timestamp(ch_dates[row["start"]]).date()),
                   end=str(pd.Timestamp(ch_dates[row["end"]]).date()),
                   state=row["state"]) for _, row in j.iterrows()]
    payload = dict(
        dates=list(daily.index), cum=[round(float(v)) for v in cum],
        flow=[round(float(v)) for v in daily],
        mbar_dates=[str(d.date()) for d in mbar.index],
        mbar=[round(float(v)) for v in mbar], stages=stages,
        stats=dict(final=float(cum.iloc[-1]), trough=trough,
                   first_pos=str(first_pos), payout=tot_pay, fee=tot_fee,
                   ch_n=int(len(ch)), ch_pass=int(len(ch[ch["result"] == "通过"])),
                   fu_n=int(len(fu)), n_acc=n_acc, be=be_tag),
    )
    out = args.out or (f"lucid5x_{n_acc}acc_be{be_tag.replace('.', '')}"
                       f"_r{args.ch_tier}r{args.fu_tier}_{args.start[:4]}.html")
    out_path = HERE / "html" / out
    out_path.parent.mkdir(exist_ok=True)

    html = HTML_TEMPLATE
    for k, v in dict(
        __PAYLOAD__=json.dumps(payload, ensure_ascii=False),
        __TITLE__=f"{n_acc} × LucidFlex $50K · 考核 r{args.ch_tier}（1R ${r_ch:.0f}）→ "
                  f"资金号 r{args.fu_tier}（1R ${r_fu:.0f}） · {args.start[:7]} → {args.end[:7]}",
        __SUB__=f"首批买号 -${fee * n_acc:.0f}，每次爆号重考 -${fee * n_acc:.0f} ｜ "
                f"BE={be_tag} / ATR 止损 {args.stop_frac:.1%} / MNQ / 1 tick 滑点 ｜ "
                f"策略信号零改动 ｜ 手数上限 {args.q_cap} 手（假设，Lucid micro 上限未确认）",
        __FINAL__=f"{payload['stats']['final']:,.0f}",
        __TROUGH__=f"{trough:,.0f}", __PAYOUT__=f"{tot_pay:,.0f}",
        __FEE__=f"{tot_fee:,.0f}", __CH__=f"{payload['stats']['ch_pass']}/{len(ch)}",
        __FU__=str(len(fu)), __FIRSTPOS__=first_pos,
    ).items():
        html = html.replace(k, v)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n网页已生成: {out_path}")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>LucidFlex 多账号资金曲线</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  body { margin:0; background:#0e1117; color:#e6e6e6; font-family:"PingFang SC","Microsoft YaHei",sans-serif; }
  .head { padding:18px 28px 6px; }
  h1 { font-size:20px; margin:0 0 4px; color:#f5c451; font-weight:600; }
  .sub { font-size:12px; color:#8a93a6; line-height:1.6; }
  .cards { display:flex; gap:14px; padding:14px 28px; flex-wrap:wrap; }
  .card { background:#161b26; border:1px solid #232b3b; border-radius:10px; padding:12px 18px; min-width:130px; }
  .card .k { font-size:11px; color:#8a93a6; margin-bottom:6px; }
  .card .v { font-size:22px; font-weight:700; color:#f5c451; }
  .card .v.neg { color:#ff6b6b; }
  .card .s { font-size:11px; color:#8a93a6; margin-top:4px; }
  #chart { width:100%; height:620px; }
  .foot { padding:6px 28px 20px; font-size:11px; color:#5d6675; line-height:1.7; }
</style>
</head>
<body>
<div class="head">
  <h1>__TITLE__</h1>
  <div class="sub">__SUB__</div>
</div>
<div class="cards">
  <div class="card"><div class="k">期末累计净现金流</div><div class="v">$__FINAL__</div></div>
  <div class="card"><div class="k">最大垫资</div><div class="v neg">$__TROUGH__</div><div class="s">回正日期 __FIRSTPOS__</div></div>
  <div class="card"><div class="k">总 payout（N 号合计）</div><div class="v">$__PAYOUT__</div></div>
  <div class="card"><div class="k">总考核费</div><div class="v neg">-$__FEE__</div></div>
  <div class="card"><div class="k">考核轮（通过/总）</div><div class="v">__CH__</div></div>
  <div class="card"><div class="k">资金号轮次</div><div class="v">__FU__</div></div>
</div>
<div id="chart"></div>
<div class="foot">
  阴影带 = 账号状态（红=考核期 绿=资金号期，多号完全同步）｜ 柱 = 月度净现金流 ｜ 线 = 累计净现金流（N 号合计）。
  单一路径含运气成分；多号完全复制 = 现金流 ×N（同过同爆），低波动年手数较大，模拟按 1 tick 固定滑点假设，
  真实滑点会恶化；LucidFlex 手数上限未确认按脚本 q-cap 假设。改参数见 prop_lucid5x_2016.py 文档头。不构成投资建议。
</div>
<script>
const P = __PAYLOAD__;
const chart = echarts.init(document.getElementById('chart'), 'dark');
const areas = P.stages.map(s => ([
  {xAxis: s.start, itemStyle: {color: s.state === '考核' ? 'rgba(255,80,80,0.06)' : 'rgba(60,200,140,0.05)'}},
  {xAxis: s.end}
]));
chart.setOption({
  backgroundColor: '#0e1117',
  tooltip: {trigger: 'axis', axisPointer: {type: 'cross'},
    formatter: ps => {
      let h = ps[0].axisValue + '<br>';
      for (const p of ps) h += p.marker + p.seriesName + ': <b>' +
        (p.value >= 0 ? '$' : '-') + '$' + Math.abs(p.value).toLocaleString() + '</b><br>';
      return h;
    }},
  legend: {data: ['累计净现金流', '月度净现金流'], textStyle: {color: '#8a93a6'}, top: 4},
  grid: [{left: 70, right: 40, top: 60, height: 380},
         {left: 70, right: 40, top: 490, height: 70}],
  xAxis: [
    {type: 'category', data: P.dates, gridIndex: 0, axisLine: {lineStyle: {color: '#333'}}},
    {type: 'category', data: P.mbar_dates, gridIndex: 1, axisLabel: {show: false}, axisLine: {show: false}}
  ],
  yAxis: [
    {type: 'value', gridIndex: 0, splitLine: {lineStyle: {color: '#1c2333'}},
     axisLabel: {formatter: v => '$' + (v/1000).toFixed(0) + 'k'}},
    {type: 'value', gridIndex: 1, splitLine: {show: false}, axisLabel: {show: false}}
  ],
  dataZoom: [
    {type: 'inside', xAxisIndex: [0, 1]},
    {type: 'slider', xAxisIndex: [0, 1], top: 575, height: 22,
     borderColor: '#232b3b', backgroundColor: '#161b26',
     fillerColor: 'rgba(245,196,81,0.15)', handleStyle: {color: '#f5c451'}}
  ],
  series: [
    {name: '累计净现金流', type: 'line', data: P.cum, xAxisIndex: 0, yAxisIndex: 0,
     showSymbol: false, lineStyle: {width: 2.2, color: '#f5c451'},
     areaStyle: {color: {type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
       {offset: 0, color: 'rgba(245,196,81,0.35)'}, {offset: 1, color: 'rgba(245,196,81,0.02)'}]}},
     markArea: {silent: true, data: areas},
     markPoint: {silent: true, symbolSize: 54,
       data: [{type: 'min', name: '最大垫资', itemStyle: {color: '#ff6b6b'},
               label: {formatter: '垫资\n{c}'}}]}},
    {name: '月度净现金流', type: 'bar', data: P.mbar, xAxisIndex: 1, yAxisIndex: 1,
     itemStyle: {color: p => p.value >= 0 ? 'rgba(60,200,140,0.55)' : 'rgba(255,107,107,0.55)'}}
  ]
});
window.addEventListener('resize', () => chart.resize());
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
