// ============================================================================
// ORB_v8_4.cs —— NQ 5min ORB v8.4 的 cTrader (C# cBot) 复刻
// ============================================================================
// 移植源（唯一权威）：ORB_strategy/orb_backtes_v8_4.py @ 2026-09-14 工作树
//   ⚠️ 该文件当前是「实验态」（起点 2020 / BE_R=7 / T_WIN_END=10:30），
//      本 cBot 的参数默认值取 notebook.md 的「锁定推荐参数」
//      （5R / 10:10 / 0.7% / MAX_QTY=200），两者差异见 README.md「参数口径」。
//
// 策略结构（与 .py 逐条对应）：
//   区间 : 09:00-09:30 ET 六根 5min K 线的高低点（右开 [09:00,09:30)）
//   入场 : 09:30-10:10 ET，逐根收盘价判断（收盘>区间高→多 / <区间低→空），每日一次
//   止损 : 7.5% × 前一日 14 日 Wilder ATR（无未来函数），按 tick 取整
//   保本 : 浮盈达 N R → 止损拉到「入场价 + 0 tick」，一次性（BE_USE_NOMINAL_R=true）
//   平仓 : 当日标签 15:55 ET 的 K 线收盘（= 墙钟 16:00）平仓，无止盈、无 trailing
//   仓位 : floor(权益 × 0.7% / (止损点数 × 每点每手美元))，上限 MAX_QTY
//   反推止损: floor 取整后按整数手数微调止损距离（ADJUST_STOP_TO_RISK）
//
// 时间语义：cTrader 的 Bar.OpenTime = K 线开盘时刻（左标签），与 .py 的 parquet
//   标签口径逐位一致 → 本 cBot 用 Robot 的 TimeZone 属性锁到 ET，所有
//   Bars.OpenTimes / Position.EntryTime 都是美东时间，无需手工换算。
//
// 构建/运行说明见 ../README.md。
// ============================================================================

using System;
using System.Collections.Generic;
using System.Globalization;
using cAlgo.API;
using cAlgo.API.Internals;

namespace cAlgo.Robots
{
    [Robot(AccessRights = AccessRights.None, TimeZone = TimeZones.EasternStandardTime)]
    public class ORB_v8_4 : Robot
    {
        // ====================================================================
        // 参数区（对应 .py 的「★ 参数开关区」；名字即 CLI --Name=value 的键）
        // ====================================================================

        [Parameter("每笔风险 % (RISK_PER_TRADE)", Group = "仓位", DefaultValue = 0.7, MinValue = 0, MaxValue = 100, Step = 0.1)]
        public double RiskPercent { get; set; }

        [Parameter("单笔最大手数 (MAX_QTY)", Group = "仓位", DefaultValue = 200, MinValue = 0, Step = 1)]
        public double MaxLots { get; set; }

        [Parameter("止损 = % × ATR (7.5 = 7.5%)", Group = "止损", DefaultValue = 7.5, MinValue = 0.1, Step = 0.5)]
        public double AtrStopPercent { get; set; }

        [Parameter("ATR 周期 (ATR_PERIOD)", Group = "止损", DefaultValue = 14, MinValue = 2, Step = 1)]
        public int AtrPeriod { get; set; }

        [Parameter("浮盈达 N R 拉保本 (BE_R_MULTIPLE)", Group = "止损", DefaultValue = 5.0, MinValue = 0, Step = 0.5)]
        public double BeRMultiple { get; set; }

        [Parameter("保本缓冲 tick (BE_BUFFER_TICKS)", Group = "止损", DefaultValue = 0, MinValue = 0, Step = 1)]
        public int BeBufferTicks { get; set; }

        [Parameter("BE 用名义 R (BE_USE_NOMINAL_R)", Group = "止损", DefaultValue = true)]
        public bool BeUseNominalR { get; set; }

        [Parameter("反推止损 (ADJUST_STOP_TO_RISK)", Group = "止损", DefaultValue = true)]
        public bool AdjustStopToRisk { get; set; }

        [Parameter("区间开始 ET (T_RANGE_START)", Group = "区间与时段", DefaultValue = "09:00")]
        public string RangeStartEt { get; set; }

        [Parameter("区间结束 ET (T_RANGE_END, 右开)", Group = "区间与时段", DefaultValue = "09:30")]
        public string RangeEndEt { get; set; }

        [Parameter("入场窗口开始 ET (T_WIN_START)", Group = "区间与时段", DefaultValue = "09:30")]
        public string EntryStartEt { get; set; }

        [Parameter("入场窗口结束 ET (T_WIN_END, 右开)", Group = "区间与时段", DefaultValue = "10:10")]
        public string EntryEndEt { get; set; }

        [Parameter("收盘平仓 bar 标签 ET", Group = "区间与时段", DefaultValue = "15:55")]
        public string EodBarLabelEt { get; set; }

        [Parameter("安全兜底平仓 ET (tick 级)", Group = "区间与时段", DefaultValue = "16:05")]
        public string SafetyFlattenEt { get; set; }

        [Parameter("每点每手美元 (0 = 自动)", Group = "成本与校验", DefaultValue = 0.0, MinValue = 0)]
        public double PointValuePerLotOverride { get; set; }

        [Parameter("DRY_RUN（只记录信号，不下单）", Group = "运行控制", DefaultValue = false)]
        public bool DryRun { get; set; }

        [Parameter("停止时平仓", Group = "运行控制", DefaultValue = false)]
        public bool ClosePositionOnStop { get; set; }

        [Parameter("详细日志（每根 bar）", Group = "运行控制", DefaultValue = true)]
        public bool VerboseLog { get; set; }

        // ====================================================================
        // 常量 / 状态
        // ====================================================================

        private const string Label = "ORB_v8_4";

        // 对应 .py 的 RTH 日线口径：日线 OHLC 只用 09:30-16:00 ET 的 K 线聚合
        private static readonly TimeSpan AtrRthStart = new TimeSpan(9, 30, 0);
        private static readonly TimeSpan AtrRthEnd = new TimeSpan(16, 0, 0);

        private TimeSpan _rangeStart, _rangeEnd, _entryStart, _entryEnd, _eodLabel, _safetyFlatten;

        // 日线聚合（ATR 用）
        private class DayOhlc
        {
            public DateTime Day;
            public double High = double.MinValue;
            public double Low = double.MaxValue;
            public double Close;
            public bool HasData;
        }

        private readonly List<DayOhlc> _days = new List<DayOhlc>();  // 按日期升序，仅已收盘(=非今日)的日子进入 ATR
        private readonly Dictionary<DateTime, DayOhlc> _dayByDate = new Dictionary<DateTime, DayOhlc>();
        private double _atrPrev;          // 前一日 14 日 Wilder ATR（今日要用的值）
        private bool _atrSeeded;
        private DateTime _atrLastDay = DateTime.MinValue;   // 已并入 ATR 的最后一天

        // 当日状态
        private DateTime _todayEt = DateTime.MinValue;
        private double _rangeHigh = double.NaN, _rangeLow = double.NaN;
        private int _rangeBars;
        private bool _enteredToday;
        private bool _cantAffordToday;
        private bool _rangeWarnedToday;

        // 持仓状态
        private Position _pos;
        private double _stopDistActual;    // 反推后、实际挂单的止损距离（价格单位）
        private double _stopDistNominal;   // 反推前的名义 ATR 止损距离（BE 判定可用）
        private bool _beMoved;
        private double _entryPx;

        // 统计（对齐 .py 的计数器）
        private int _nEntries, _nBeMoves, _nStopped, _nBeExits, _nEod, _nNoTrade, _nCantAfford, _nCapped, _nLotExact, _nNoRange;

        private DateTime _lastProcessedBarOpen = DateTime.MinValue;
        private bool _barTimingDiagLogged;
        private int _selfClosePending;     // 由本策略主动发起的平仓计数（用于区分平台侧的止损成交）

        // ====================================================================
        // 生命周期
        // ====================================================================

        protected override void OnStart()
        {
            ParseTimeOrThrow(RangeStartEt, "区间开始", out _rangeStart, new TimeSpan(9, 0, 0));
            ParseTimeOrThrow(RangeEndEt, "区间结束", out _rangeEnd, new TimeSpan(9, 30, 0));
            ParseTimeOrThrow(EntryStartEt, "入场开始", out _entryStart, new TimeSpan(9, 30, 0));
            ParseTimeOrThrow(EntryEndEt, "入场结束", out _entryEnd, new TimeSpan(10, 10, 0));
            ParseTimeOrThrow(EodBarLabelEt, "收盘 bar 标签", out _eodLabel, new TimeSpan(15, 55, 0));
            ParseTimeOrThrow(SafetyFlattenEt, "安全平仓时间", out _safetyFlatten, new TimeSpan(16, 5, 0));

            Log("========== ORB v8.4 (cTrader 复刻) 启动 ==========");
            Log("品种={0} 周期={1} 账户={2} Equity={3:F2} {4}",
                SymbolName, Bars.TimeFrame, Account.Number, Account.Equity, Account.Currency);
            Log("品种规格: TickSize={0} TickValue={1} PipSize={2} PipValue={3} LotSize={4} Digits={5}",
                Symbol.TickSize, Symbol.TickValue, Symbol.PipSize, Symbol.PipValue, Symbol.LotSize, Symbol.Digits);
            Log("成交量: Min={0} Step={1} Max={2} | 每点每手美元={3:F4} ({4})",
                Symbol.VolumeInUnitsMin, Symbol.VolumeInUnitsStep, Symbol.VolumeInUnitsMax,
                PointValuePerLot, PointValuePerLotOverride > 0 ? "手工覆盖" : "TickValue/TickSize 自动");
            Log("时段: 区间[{0:hh\\:mm},{1:hh\\:mm}) 入场[{2:hh\\:mm},{3:hh\\:mm}) 收盘bar={4:hh\\:mm} 兜底={5:hh\\:mm}",
                _rangeStart, _rangeEnd, _entryStart, _entryEnd, _eodLabel, _safetyFlatten);
            Log("参数: 风险={0}% 最大手数={1} 止损={2}%×{3}日ATR 保本={4}R 缓冲={5}tick 反推止损={6} 名义R={7} DRY_RUN={8}",
                RiskPercent, MaxLots, AtrStopPercent, AtrPeriod, BeRMultiple, BeBufferTicks,
                AdjustStopToRisk ? "开" : "关", BeUseNominalR ? "是" : "否", DryRun ? "是" : "否");

            if (Bars.TimeFrame != TimeFrame.Minute5)
                Log("⚠️ 警告: 图表周期是 {0}，但策略语义要求 5 分钟 K 线（区间=6 根、入场窗口=10 根）", Bars.TimeFrame);

            TimeZoneSelfTest();
            Positions.Closed += OnPositionsClosed;
            RebuildHistory();
            RebuildTodayState();

            Log("历史重建完成: 日线聚合={0} 天, 前一日ATR={1:F2} 点, 今日区间={2}",
                _days.Count, _atrPrev,
                double.IsNaN(_rangeHigh) ? "未形成" : string.Format(CultureInfo.InvariantCulture, "[{0:F2}, {1:F2}] ({2} 根)", _rangeLow, _rangeHigh, _rangeBars));
        }

        protected override void OnBarClosed()
        {
            ProcessClosedBar("OnBarClosed");
        }

        protected override void OnBar()
        {
            // 兜底：若平台/回测模式只触发 OnBar，同样按「已收盘 bar」处理（OpenTime 去重保证幂等）
            ProcessClosedBar("OnBar");
        }

        protected override void OnTick()
        {
            // 1) 隔夜残留防护：新的一天仍有持仓 → 立即平掉并告警
            var nowEt = NowEt();
            if (_pos == null)
                _pos = Positions.Find(Label, SymbolName);

            if (_pos != null && _pos.EntryTime.Date != nowEt.Date)
            {
                CloseByUs(_pos, "隔夜残留防护");
                _pos = null;
                return;
            }

            // 2) EOD 兜底平仓：bar 级平仓没触发（缺 bar / 半日 / 断线）时兜住
            if (_pos != null && nowEt.TimeOfDay >= _safetyFlatten && nowEt.TimeOfDay < new TimeSpan(23, 59, 0))
            {
                CloseByUs(_pos, string.Format(CultureInfo.InvariantCulture, "EOD 兜底平仓 (bar 级未触发) ET={0:HH:mm:ss}", nowEt));
                LogEodCounters();
                _pos = null;
            }
        }

        protected override void OnStop()
        {
            if (ClosePositionOnStop && _pos != null)
            {
                Log("OnStop: 按参数平掉持仓");
                CloseByUs(_pos, "OnStop");
                _pos = null;
            }
            else if (_pos != null)
            {
                Log("⚠️ OnStop: 仍有持仓 {0} {1} 手 @{2:F2} SL={3}（服务器端止损仍然有效）",
                    _pos.TradeType, _pos.Quantity, _pos.EntryPrice,
                    _pos.StopLoss.HasValue ? _pos.StopLoss.Value.ToString("F2", CultureInfo.InvariantCulture) : "无");
            }

            Log("========== 统计 ==========");
            Log("入场 {0} | 浮盈达 {1}R 拉保本 {2} 次 | 出场: 初始止损 {3} / 保本止损 {4} / 收盘 {5}",
                _nEntries, BeRMultiple, _nBeMoves, _nStopped, _nBeExits, _nEod);
            Log("当日无突破 {0} 天 | 有突破买不起手数 {1} 天 | 被最大手数压制 {2} 天 | 手数整除跳过反推 {3} 天 | 区间缺失 {4} 天",
                _nNoTrade, _nCantAfford, _nCapped, _nLotExact, _nNoRange);
        }

        // ====================================================================
        // 主流程（对应 .py 的 on_bar）
        // ====================================================================

        private void ProcessClosedBar(string callback)
        {
            Bar bar;
            if (!TryGetLastClosedBar(callback, out bar))
                return;

            var et = bar.OpenTime;                 // TimeZone=ET → 已是美东时间
            var tod = et.TimeOfDay;
            var day = et.Date;

            if (day != _todayEt)
                OnNewDay(day);

            // ---- 1) 区间累计 [rangeStart, rangeEnd) ----
            if (tod >= _rangeStart && tod < _rangeEnd)
            {
                if (double.IsNaN(_rangeHigh))
                {
                    _rangeHigh = bar.High;
                    _rangeLow = bar.Low;
                }
                else
                {
                    _rangeHigh = Math.Max(_rangeHigh, bar.High);
                    _rangeLow = Math.Min(_rangeLow, bar.Low);
                }
                _rangeBars++;
                if (VerboseLog)
                    Log("[bar] {0:HH:mm} O={1:F2} H={2:F2} L={3:F2} C={4:F2} | 区间=[{5:F2},{6:F2}] ({7} 根)",
                        et, bar.Open, bar.High, bar.Low, bar.Close, _rangeLow, _rangeHigh, _rangeBars);
            }
            else if (VerboseLog)
            {
                Log("[bar] {0:HH:mm} O={1:F2} H={2:F2} L={3:F2} C={4:F2}{5}",
                    et, bar.Open, bar.High, bar.Low, bar.Close, _pos != null ? " [持仓中]" : "");
            }

            // ---- 2) 入场窗口 [entryStart, entryEnd) ----
            if (!_enteredToday && tod >= _entryStart && tod < _entryEnd)
            {
                if (!_rangeWarnedToday && _rangeBars != 6)
                {
                    Log("⚠️ {0:yyyy-MM-dd} 盘前区间 bar 数 = {1}（期望 6 根）—— .py 记录过这类数据缺口日",
                        day, _rangeBars);
                    _rangeWarnedToday = true;
                }
                if (double.IsNaN(_rangeHigh))
                {
                    if (!_rangeWarnedToday)
                    {
                        Log("⚠️ {0:yyyy-MM-dd} 无盘前区间（0 根 bar）→ 当日不交易", day);
                        _rangeWarnedToday = true;
                    }
                }
                else
                {
                    var close = bar.Close;
                    if (close > _rangeHigh)
                        Enter(TradeType.Buy, bar, "收盘 {0:F2} > 区间高 {1:F2}".ToStringInvariant(close, _rangeHigh));
                    else if (close < _rangeLow)
                        Enter(TradeType.Sell, bar, "收盘 {0:F2} < 区间低 {1:F2}".ToStringInvariant(close, _rangeLow));
                    else if (VerboseLog)
                        Log("[区间内] {0:HH:mm} 收盘 {1:F2} ⊂ [{2:F2},{3:F2}] → 等下一根", et, close, _rangeLow, _rangeHigh);
                }
            }

            // ---- 3) 保本（当日最后一根 bar 之前）----
            if (_pos != null && tod < _eodLabel)
                CheckBreakEven(bar);

            // ---- 4) 收盘平仓：标签 15:55 的 K 线 ----
            if (tod == _eodLabel)
            {
                if (_pos != null)
                {
                    CloseByUs(_pos, string.Format(CultureInfo.InvariantCulture, "收盘平仓（{0:HH:mm} 标签 bar 收盘）", et));
                    _nEod++;
                    _pos = null;
                }
                if (!_enteredToday)
                {
                    if (_cantAffordToday)
                        _nCantAfford++;
                    else if (double.IsNaN(_rangeHigh) || _rangeBars == 0)
                        _nNoRange++;
                    else
                        _nNoTrade++;
                }
                LogEodCounters();
            }
        }

        private void OnNewDay(DateTime day)
        {
            // 上一交易日收口 → 并入 ATR 序列
            FinalizeDaysBefore(day);

            _todayEt = day;
            _rangeHigh = double.NaN;
            _rangeLow = double.NaN;
            _rangeBars = 0;
            _enteredToday = false;
            _cantAffordToday = false;
            _rangeWarnedToday = false;
            _beMoved = false;

            if (_pos != null && _pos.EntryTime.Date != day)
            {
                CloseByUs(_pos, string.Format(CultureInfo.InvariantCulture, "换日残留平仓（入场 {0}）", _pos.EntryTime));
                _pos = null;
            }

            Log("—— 新交易日 {0:yyyy-MM-dd} (ET) | 前一日 {1} 日 ATR = {2:F2} 点 (−> 止损距离 {3:F2} 点) ——",
                day, AtrPeriod, _atrPrev, StopDistanceFromAtr());
        }

        // ====================================================================
        // 入场（对应 .py 的 _enter）
        // ====================================================================

        private void Enter(TradeType side, Bar bar, string reason)
        {
            var stopDistNominal = StopDistanceFromAtr();
            if (double.IsNaN(stopDistNominal) || stopDistNominal <= 0)
            {
                Log("⚠️ {0:yyyy-MM-dd} 无前一日 ATR → 跳过交易", bar.OpenTime.Date);
                _enteredToday = true;
                return;
            }

            var entry = bar.Close;                                   // 入场参考价 = 突破 bar 收盘价
            var stopPrice = side == TradeType.Buy ? TickRound(entry - stopDistNominal)
                                                  : TickRound(entry + stopDistNominal);
            var actualDist = Math.Abs(entry - stopPrice);

            Log("[突破] {0:HH:mm} {1} | {2} | 名义止损距离 {3:F2} 点",
                bar.OpenTime, side == TradeType.Buy ? "做多" : "做空", reason, stopDistNominal);

            // ---- 以损定仓 ----
            var equity = Account.Equity;
            var riskAmount = equity * RiskPercent / 100.0;
            var lotsRaw = riskAmount / (actualDist * PointValuePerLot);
            if (MaxLots > 0 && lotsRaw > MaxLots)
                _nCapped++;
            var lots = Math.Floor(Math.Min(lotsRaw, MaxLots > 0 ? MaxLots : double.MaxValue));
            var lotExact = Math.Abs(lotsRaw - Math.Round(lotsRaw)) < 1e-9 * Math.Max(1.0, lotsRaw);
            if (lotExact)
                _nLotExact++;

            var volume = Symbol.QuantityToVolumeInUnits(lots);
            volume = Symbol.NormalizeVolumeInUnits(volume, RoundingMode.Down);

            if (lots < 1 || volume < Symbol.VolumeInUnitsMin)
            {
                Log("⚠️ {0:yyyy-MM-dd} 有突破但买不起 1 手 (预算 ${1:F2} / 止损 {2:F2} 点 × ${3:F4}/点 = ${4:F2}) → 当日放弃",
                    bar.OpenTime.Date, riskAmount, actualDist, PointValuePerLot, actualDist * PointValuePerLot);
                _cantAffordToday = true;
                _enteredToday = true;
                return;
            }

            // ---- 反推止损（ADJUST_STOP_TO_RISK）----
            var stopDistFinal = actualDist;
            if (AdjustStopToRisk && (MaxLots <= 0 || lots < MaxLots) && !lotExact)
            {
                var target = riskAmount / (lots * PointValuePerLot);
                target = Math.Min(target, stopDistNominal * 1.5);
                target = Math.Max(Symbol.TickSize, TickRound(target));
                if (target > actualDist)
                {
                    stopPrice = side == TradeType.Buy ? TickRound(entry - target) : TickRound(entry + target);
                    stopDistFinal = Math.Abs(entry - stopPrice);
                    if (VerboseLog)
                        Log("[反推止损] 手数 {0:F0} → 止损距离 {1:F2} → {2:F2} 点 (放宽 {3:P2})",
                            lots, actualDist, stopDistFinal, stopDistFinal / actualDist - 1);
                }
            }

            var moneyRisk = stopDistFinal * PointValuePerLot * lots;
            Log("[信号] {0} {1} 手 ({2} units) 参考价 {3:F2} 止损 {4:F2} (距离 {5:F2} 点) 预算风险 ${6:F2}≈实际 ${7:F2} ({8:P3} 权益) 点差 {9} 点",
                side, lots, volume, entry, stopPrice, stopDistFinal, riskAmount, moneyRisk,
                moneyRisk / equity, Symbol.Spread);

            _enteredToday = true;

            if (DryRun)
            {
                Log("[DRY_RUN] 不下单（仅记录信号）");
                return;
            }

            // 挂单用 pips 传初始止损（先有保护），成交后按实际成交价改成绝对价
            var slPips = stopDistFinal / Symbol.PipSize;
            var res = ExecuteMarketOrder(side, SymbolName, volume, Label, slPips, null);
            if (!res.IsSuccessful)
            {
                Log("⚠️ 带止损下单被拒 ({0}) → 改为先按市价成交、随后按绝对价挂止损", res.Error);
                res = ExecuteMarketOrder(side, SymbolName, volume, Label);
                if (!res.IsSuccessful)
                {
                    Log("❌ 下单失败: {0}", res.Error);
                    return;
                }
            }

            _pos = res.Position;
            _entryPx = _pos.EntryPrice;
            _stopDistActual = stopDistFinal;
            _stopDistNominal = stopDistNominal;
            _beMoved = false;
            _nEntries++;

            // 按实际成交价重挂绝对止损
            var realStop = side == TradeType.Buy ? TickRound(_entryPx - stopDistFinal) : TickRound(_entryPx + stopDistFinal);
            if (Math.Abs(realStop - stopPrice) > 1e-9)
                ModifyPosition(_pos, realStop, null);

            Log("[入场] {0} {1} 手 @{2:F2} 止损 {3:F2} (R={4:F2} 点, 名义R={5:F2}) | 达 {6}R 拉保本",
                _pos.TradeType, _pos.Quantity, _entryPx, realStop, stopDistFinal, stopDistNominal, BeRMultiple);
            Log("[日内] 本日入场完成 → 持有到收盘（无 trailing、无止盈）");
        }

        // ====================================================================
        // 保本（对应 .py 的 _check_be / _move_stop_to_be）
        // ====================================================================

        private void CheckBreakEven(Bar bar)
        {
            if (_beMoved || _pos == null)
                return;

            var r = BeUseNominalR ? _stopDistNominal : _stopDistActual;
            if (r <= 0)
                return;

            var trigger = BeRMultiple * r;
            var hit = _pos.TradeType == TradeType.Buy
                ? bar.High >= _entryPx + trigger
                : bar.Low <= _entryPx - trigger;
            if (!hit)
                return;

            var buffer = BeBufferTicks * Symbol.TickSize;
            var bePx = _pos.TradeType == TradeType.Buy ? TickRound(_entryPx + buffer) : TickRound(_entryPx - buffer);

            Log("[BE] {0:HH:mm} 触及 {1}R ({2:F2} 点, {3} R 口径) → 止损 {4:F2} 移到 {5:F2}",
                bar.OpenTime, BeRMultiple, trigger, BeUseNominalR ? "名义" : "实际",
                _pos.StopLoss.HasValue ? _pos.StopLoss.Value : double.NaN, bePx);

            ModifyPosition(_pos, bePx, null);
            _beMoved = true;
            _nBeMoves++;
        }

        // ====================================================================
        // ATR / 日线聚合（对应 .py 的 build_atr_map：RTH 日线 + Wilder）
        // ====================================================================

        private void RebuildHistory()
        {
            // 尽量多拉历史（ATR 是 14 日 Wilder，需要足够多的交易日才能收敛到回测口径）
            try
            {
                for (var pass = 0; pass < 6 && _dayByDate.Count < 60; pass++)
                {
                    var added = Bars.LoadMoreHistory();
                    if (added <= 0)
                        break;
                }
            }
            catch (Exception ex)
            {
                Log("(LoadMoreHistory 不可用，忽略: {0})", ex.Message);
            }

            for (var i = 0; i < Bars.Count; i++)
            {
                var b = Bars[i];
                var tod = b.OpenTime.TimeOfDay;
                if (tod < AtrRthStart || tod >= AtrRthEnd)
                    continue;

                DayOhlc d;
                if (!_dayByDate.TryGetValue(b.OpenTime.Date, out d))
                {
                    d = new DayOhlc { Day = b.OpenTime.Date };
                    _dayByDate[b.OpenTime.Date] = d;
                    _days.Add(d);
                }
                d.High = d.HasData ? Math.Max(d.High, b.High) : b.High;
                d.Low = d.HasData ? Math.Min(d.Low, b.Low) : b.Low;
                d.Close = b.Close;
                d.HasData = true;
            }
            _days.Sort((x, y) => x.Day.CompareTo(y.Day));
            // 注意：这里不并入 ATR —— 用哪一天的历史由 RebuildTodayState 里的
            // FinalizeDaysBefore(_todayEt) 决定，避免把「今天这根未收完的日线」算进去。
            if (_dayByDate.Count < AtrPeriod * 3)
                Log("⚠️ 可用日线仅 {0} 天（{1} 日 ATR 需要更多历史才能收敛到回测口径），ATR 可能偏早期值",
                    _dayByDate.Count, AtrPeriod);
        }

        /// <summary>把 day 之前（严格早于）的所有已聚合日并入 Wilder ATR 序列。</summary>
        private void FinalizeDaysBefore(DateTime day)
        {
            foreach (var d in _days)
            {
                if (!d.HasData || d.Day >= day || d.Day <= _atrLastDay)
                    continue;

                double prevClose = double.NaN;
                if (_atrLastDay != DateTime.MinValue)
                {
                    // 找上一有数据的日子的收盘
                    for (var k = _days.Count - 1; k >= 0; k--)
                    {
                        if (_days[k].HasData && _days[k].Day == _atrLastDay)
                        {
                            prevClose = _days[k].Close;
                            break;
                        }
                    }
                }

                var tr = d.High - d.Low;
                if (!double.IsNaN(prevClose))
                    tr = Math.Max(tr, Math.Max(Math.Abs(d.High - prevClose), Math.Abs(d.Low - prevClose)));

                _atrPrev = _atrSeeded ? _atrPrev + (tr - _atrPrev) / AtrPeriod : tr;   // Wilder 递推（对齐 pandas ewm adjust=False）
                _atrSeeded = true;
                _atrLastDay = d.Day;
            }
        }

        private double StopDistanceFromAtr()
        {
            if (!_atrSeeded || _atrPrev <= 0)
                return double.NaN;
            return Math.Max(Symbol.TickSize, TickRound(AtrStopPercent / 100.0 * _atrPrev));
        }

        // ====================================================================
        // 启动时重建当日状态（区间 / 是否已入场）
        // ====================================================================

        private void RebuildTodayState()
        {
            if (Bars.Count == 0)
                return;

            _todayEt = Bars[Bars.Count - 1].OpenTime.Date;

            // 今日要用的 ATR = 严格早于今天的日线序列（对齐 .py 的 atr.shift(1)）
            FinalizeDaysBefore(_todayEt);

            for (var i = 0; i < Bars.Count; i++)
            {
                var b = Bars[i];
                if (b.OpenTime.Date != _todayEt)
                    continue;
                var tod = b.OpenTime.TimeOfDay;
                if (tod >= _rangeStart && tod < _rangeEnd)
                {
                    _rangeHigh = double.IsNaN(_rangeHigh) ? b.High : Math.Max(_rangeHigh, b.High);
                    _rangeLow = double.IsNaN(_rangeLow) ? b.Low : Math.Min(_rangeLow, b.Low);
                    _rangeBars++;
                }
            }

            // 已入场判定：持仓 或 今日已有带本标签的成交历史（重启场景）
            var open = Positions.FindAll(Label, SymbolName);
            if (open.Length > 0)
            {
                _pos = open[0];
                _enteredToday = true;
                _entryPx = _pos.EntryPrice;
                _stopDistActual = _pos.StopLoss.HasValue ? Math.Abs(_entryPx - _pos.StopLoss.Value) : 0;
                _stopDistNominal = _stopDistActual;
                Log("重启检测到持仓: {0} {1} 手 @{2:F2} SL={3}（按已入场处理，不再开新仓）",
                    _pos.TradeType, _pos.Quantity, _entryPx,
                    _pos.StopLoss.HasValue ? _pos.StopLoss.Value.ToString("F2", CultureInfo.InvariantCulture) : "无");
            }
            else
            {
                try
                {
                    var todays = History.FindAll(Label, SymbolName);
                    foreach (var t in todays)
                    {
                        if (t.EntryTime.Date == _todayEt)
                        {
                            _enteredToday = true;
                            Log("重启检测到今日已成交历史 (入场 {0}) → 当日不再开新仓", t.EntryTime);
                            break;
                        }
                    }
                }
                catch (Exception ex)
                {
                    Log("(历史读取失败，忽略: {0})", ex.Message);
                }
            }
        }

        // ====================================================================
        // 出场统计（对应 .py 的 on_order_filled 出场分支）
        // ====================================================================

        private void OnPositionsClosed(PositionClosedEventArgs args)
        {
            var p = args.Position;
            if (p.Label != Label || p.SymbolName != SymbolName)
                return;

            if (_selfClosePending > 0)
            {
                _selfClosePending--;
                return;                       // 我们自己发起的平仓，已在调用处计数
            }

            if (args.Reason == PositionCloseReason.StopLoss)
            {
                if (_beMoved)
                {
                    _nBeExits++;
                    Log("[出场] 保本止损成交 @{0:F2} (入场 {1:F2}, 净盈亏 {2:F2})", p.EntryPrice, _entryPx, p.NetProfit);
                }
                else
                {
                    _nStopped++;
                    Log("[出场] 初始止损成交 @{0:F2} (入场 {1:F2}, 净盈亏 {2:F2})", p.EntryPrice, _entryPx, p.NetProfit);
                }
                _pos = null;
            }
            else if (args.Reason == PositionCloseReason.StopOut)
            {
                Log("⚠️ StopOut（保证金不足强制平仓）@{0:F2} 净盈亏 {1:F2}", p.EntryPrice, p.NetProfit);
                _pos = null;
            }
            else
            {
                Log("[出场] 平台侧平仓 ({0}) @{1:F2} 净盈亏 {2:F2}", args.Reason, p.EntryPrice, p.NetProfit);
                _pos = null;
            }
        }

        /// <summary>本策略主动平仓：登记后调用 ClosePosition，避免与止损成交混淆。</summary>
        private void CloseByUs(Position p, string why)
        {
            if (p == null)
                return;
            _selfClosePending++;
            Log("[平仓] {0}: {1} {2} 手 @{3:F2} 浮盈 {4:F2}", why, p.TradeType, p.Quantity, p.EntryPrice, p.NetProfit);
            ClosePosition(p);
        }

        // ====================================================================
        // 工具
        // ====================================================================

        /// <summary>每点每手美元（= TickValue / TickSize；可由参数覆盖）。</summary>
        private double PointValuePerLot
        {
            get
            {
                if (PointValuePerLotOverride > 0)
                    return PointValuePerLotOverride;
                return Symbol.TickValue / Symbol.TickSize;
            }
        }

        private double TickRound(double price)
        {
            var t = Symbol.TickSize;
            if (t <= 0)
                return price;
            return Math.Round(Math.Round(price / t) * t, Symbol.Digits);
        }

        private DateTime NowEt()
        {
            // 与 Robot 的 TimeZone 属性无关，恒为准确的 ET 墙钟
            var tz = TimeZoneInfo.FindSystemTimeZoneById(OperatingSystem.IsWindows() ? "Eastern Standard Time" : "America/New_York");
            return TimeZoneInfo.ConvertTimeFromUtc(DateTime.SpecifyKind(Server.TimeInUtc, DateTimeKind.Utc), tz);
        }

        private void TimeZoneSelfTest()
        {
            try
            {
                var now = NowEt();
                Log("时区自检: Server.Time={0:yyyy-MM-dd HH:mm:ss} (ET 口径) | Server.TimeInUtc={1:yyyy-MM-dd HH:mm:ss}Z | 换算 ET={2:yyyy-MM-dd HH:mm:ss} ({3})",
                    Server.Time, Server.TimeInUtc, now, now.IsDaylightSavingTime() ? "EDT" : "EST");
            }
            catch (Exception ex)
            {
                Log("⚠️ 时区自检失败: {0}", ex.Message);
            }
        }

        /// <summary>
        /// 取「最后一根已收盘」的 K 线并对同一根去重（幂等）。
        /// OnBarClosed / OnBar 两种回调语义差异由「开盘时间 + bar 跨度 ≤ ET 现在」判定吸收；
        /// 找不到时回退为倒数第二根（假定新 bar 已形成）。
        /// </summary>
        private bool TryGetLastClosedBar(string callback, out Bar bar)
        {
            bar = default(Bar);
            if (Bars.Count < 2)
                return false;

            var newest = Bars[Bars.Count - 1].OpenTime;
            var prev = Bars[Bars.Count - 2].OpenTime;
            var span = newest - prev;
            if (span <= TimeSpan.Zero)
                span = TimeSpan.FromMinutes(5);

            var nowEt = NowEt();
            Bar candidate = default(Bar);
            var found = false;
            for (var i = Bars.Count - 1; i >= 0 && i >= Bars.Count - 5; i--)
            {
                var b = Bars[i];
                if (b.OpenTime + span <= nowEt + TimeSpan.FromSeconds(2))
                {
                    candidate = b;
                    found = true;
                    break;
                }
            }
            if (!found)
                candidate = Bars[Bars.Count - 2];

            if (!_barTimingDiagLogged)
            {
                _barTimingDiagLogged = true;
                Log("[bar节奏] 回调={0} Bars={1} 最新bar={2:HH:mm} 倒数第二={3:HH:mm} ET现在={4:HH:mm:ss} 判定收盘bar={5:HH:mm}",
                    callback, Bars.Count, newest, prev, nowEt, candidate.OpenTime);
            }

            if (candidate.OpenTime <= _lastProcessedBarOpen)
                return false;
            _lastProcessedBarOpen = candidate.OpenTime;
            bar = candidate;
            return true;
        }

        private void LogEodCounters()
        {
            Log("[日结] {0:yyyy-MM-dd} 入场={1} | 累计: 入场 {2} / 保本 {3} / 初始止损 {4} / 保本止损 {5} / 收盘 {6} / 无突破 {7} / 买不起 {8}",
                _todayEt, _enteredToday ? "有" : "无", _nEntries, _nBeMoves, _nStopped, _nBeExits, _nEod, _nNoTrade, _nCantAfford);
        }

        private void Log(string format, params object[] args)
        {
            Print("[ORB] " + string.Format(CultureInfo.InvariantCulture, format, args));
        }

        private static void ParseTimeOrThrow(string text, string name, out TimeSpan value, TimeSpan fallback)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                value = fallback;
                return;
            }
            text = text.Trim();
            if (TimeSpan.TryParse(text, CultureInfo.InvariantCulture, out value))
                return;
            if (text.Length == 4 && text.IndexOf(':') < 0)
            {
                value = new TimeSpan(int.Parse(text.Substring(0, 2)), int.Parse(text.Substring(2, 2)), 0);
                return;
            }
            throw new ArgumentException(string.Format("无法解析 {0} 时间 \"{1}\"，期望 HH:mm", name, text));
        }
    }

    internal static class StringExt
    {
        public static string ToStringInvariant(this string format, params object[] args)
        {
            return string.Format(CultureInfo.InvariantCulture, format, args);
        }
    }
}
