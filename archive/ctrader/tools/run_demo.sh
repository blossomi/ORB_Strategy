#!/usr/bin/env bash
# 模拟盘常驻运行模板（需凭据 + 已编译的 .algo）
# 用法:
#   CTID=<cTID> ACCOUNT=<模拟号> SYMBOL=NAS100 ALGO=/path/ORB_v8_4.algo ./run_demo.sh
#   DRY_RUN=1 ... ./run_demo.sh      # 首日建议：只记录信号不下单
# 纪律（对齐 live/ 的实盘纪律）:
#   - 用 caffeinate -s 保持进程存活（必须活到 16:05 ET 兜底平仓）
#   - 日志落 runs/，按启动时刻命名、从不覆盖
#   - 先 DRY_RUN 一天核对 [时区自检]/[bar节奏]/[突破]/[信号] 时间戳与手数，再开真下单
set -euo pipefail

CTID="${CTID:?请设置 CTID}"
ACCOUNT="${ACCOUNT:?请设置 ACCOUNT（模拟盘账户号）}"
SYMBOL="${SYMBOL:-NAS100}"
PERIOD="${PERIOD:-m5}"
PWD_FILE="${PWD_FILE:-$HOME/.ctrader/pwd}"
ALGO="${ALGO:?请设置 ALGO（.algo 文件路径）}"
DRY_RUN="${DRY_RUN:-0}"
MAX_LOTS="${MAX_LOTS:-200}"
RISK_PCT="${RISK_PCT:-0.7}"
BE_R="${BE_R:-5}"
STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/runs"
LOG="$RUN_DIR/demo_${STAMP}.log"

[ -r "$PWD_FILE" ] || { echo "缺少密码文件: $PWD_FILE（见 README §6.2）"; exit 1; }
[ -r "$ALGO" ] || { echo "缺少 .algo: $ALGO"; exit 1; }
mkdir -p "$RUN_DIR"
export PATH="/opt/homebrew/bin:$PATH"

DRY_FLAG=false; [ "$DRY_RUN" = "1" ] && DRY_FLAG=true

echo "== 模拟盘运行 | $SYMBOL $PERIOD | DRY_RUN=$DRY_FLAG | 日志 $LOG"
caffeinate -s ctrader-cli run "$ALGO" \
  --ctid="$CTID" --pwd-file="$PWD_FILE" --account="$ACCOUNT" \
  --symbol="$SYMBOL" --period="$PERIOD" \
  --DryRun="$DRY_FLAG" --RiskPercent="$RISK_PCT" --MaxLots="$MAX_LOTS" --BeRMultiple="$BE_R" \
  2>&1 | tee -a "$LOG"
