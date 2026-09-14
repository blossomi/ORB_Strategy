#!/usr/bin/env bash
# cTrader 引擎回测模板（需凭据 + 已编译的 .algo）
# 用法:
#   CTID=<cTID> ACCOUNT=<账户号> SYMBOL=NAS100 ALGO=/path/ORB_v8_4.algo \
#     START="01/06/2026 00:00" END="31/08/2026 00:00" ./backtest.sh
# 说明:
#   - 密码从 ~/.ctrader/pwd 读取（不放命令行；文件权限须 600）
#   - --data-mode=m1 用服务器 1 分钟数据（比 open 准）；--data-dir 持久化缓存，避免每次重下
set -euo pipefail

CTID="${CTID:?请设置 CTID（cTID 用户名或邮箱）}"
ACCOUNT="${ACCOUNT:?请设置 ACCOUNT（账户号）}"
SYMBOL="${SYMBOL:-NAS100}"
PERIOD="${PERIOD:-m5}"
PWD_FILE="${PWD_FILE:-$HOME/.ctrader/pwd}"
ALGO="${ALGO:?请设置 ALGO（.algo 文件路径）}"
START="${START:-01/06/2026 00:00}"
END="${END:-31/08/2026 00:00}"
BALANCE="${BALANCE:-25000}"
COMMISSION="${COMMISSION:-0}"     # 元/百万成交额口径，按券商设置
SPREAD="${SPREAD:-0}"             # 点数；0 = 用数据自带点差
MODE="${MODE:-m1}"
OUT_DIR="${OUT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/runs}"

[ -r "$PWD_FILE" ] || { echo "缺少密码文件: $PWD_FILE（见 README §6.2）"; exit 1; }
[ -r "$ALGO" ] || { echo "缺少 .algo: $ALGO"; exit 1; }

mkdir -p "$OUT_DIR/data"
export PATH="/opt/homebrew/bin:$PATH"

echo "== 回测 $ALGO | $SYMBOL $PERIOD | $START → $END | 余额 \$$BALANCE | mode=$MODE"
ctrader-cli backtest "$ALGO" \
  --ctid="$CTID" --pwd-file="$PWD_FILE" --account="$ACCOUNT" \
  --symbol="$SYMBOL" --period="$PERIOD" \
  --start="$START" --end="$END" \
  --balance="$BALANCE" --commission="$COMMISSION" --spread="$SPREAD" \
  --data-mode="$MODE" --data-dir="$OUT_DIR/data" \
  --report="$OUT_DIR/backtest_report.html" \
  --report-json="$OUT_DIR/backtest_report.json" 2>&1 | tee "$OUT_DIR/backtest_$(date +%Y%m%d_%H%M%S).log"
