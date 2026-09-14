#!/usr/bin/env bash
# 本地编译校验：只验证 C# 能否编译（不需要 cTID 凭据，不产生 .algo）
# 用法: ./compile_check.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ="$HERE/../src/ORB_v8_4/ORB_v8_4.csproj"
export PATH="/opt/homebrew/bin:/usr/local/share/dotnet:$PATH"

echo "== 工程: $PROJ"
dotnet build "$PROJ" --nologo -v m
echo
echo "== 校验通过（注意：正式产物 .algo 必须用 cTrader CLI 生成，见 README §6.3）"
