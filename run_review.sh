#!/bin/bash
# 本机跑每日/每周回顾 —— LLM 走 DSH 自己那份 MiniMax 凭据（~/.dsh/.credentials.yaml）。
#
# 为什么搬回本机：CI 里那两个 key 都不好用（主 key 403 无权限，
# fallback 的端点跟 SDK 格式对不上），而 DSH 本机跑的 LLM 一直是好的。
set -u
cd /home/averypi/Projects/my-project-management
# ── 单实例租约（抄 CapGen 的做法：文件系统租约保证一个 profile 只有一个调度者）
# 没有它，两个 DSH 进程共用一个 profile 时会重复跑（回顾会重复建 issue）。
LOCK_DIR="$HOME/.dsh/project-console/locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/review.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "⏭️  已有同一任务在跑（租约被占），跳过"
  exit 0
fi

eval "$(python3 - <<'PYEOF'
import yaml, io
d = yaml.safe_load(io.open('/home/averypi/.dsh/.credentials.yaml', encoding='utf-8'))
key = (d.get('refs') or {}).get('MINIMAX_CN_API_KEY', '')
print(f"export LLM_PRIMARY_API_KEY='{key}'")
print("export LLM_PRIMARY_BASE_URL='https://api.minimaxi.com/anthropic'")
print("export LLM_PRIMARY_MODEL='MiniMax-M3'")
PYEOF
)"
[ -n "${LLM_PRIMARY_API_KEY:-}" ] || { echo "❌ 没取到 MiniMax 凭据"; exit 2; }

# GitHub 用本机的 gh 凭据
if [ -f "$HOME/.new-gh-token" ]; then
  export GH_TOKEN="$(cat "$HOME/.new-gh-token")"
fi
export PROJECT_NUMBER="${PROJECT_NUMBER:-1}"
export GH_OWNER="${GH_OWNER:-laiyinyizao007}"

exec python3 -u generate_weekly_plan.py "$@"
