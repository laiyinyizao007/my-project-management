#!/bin/bash
# 本机定时器跑的同步：
#   1) 看门狗 —— 工作台插件被误禁用的话自动恢复（它被禁用过一次，症状是路由 404）
#   2) 刷台账快照（工作台插件的「台账」页签读它）
#   3) 同步 Notion 看板（--auto-mode：按上次全量刷新的时间决定查改动还是全量）
set -u
cd /home/averypi/Projects/my-project-management
# ── 单实例租约（抄 CapGen 的做法：文件系统租约保证一个 profile 只有一个调度者）
# 没有它，两个 DSH 进程共用一个 profile 时会重复跑（回顾会重复建 issue）。
LOCK_DIR="$HOME/.dsh/project-console/locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/notion-sync.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "⏭️  已有同一任务在跑（租约被占），跳过"
  exit 0
fi

MARKET_STATE=~/.dsh/profiles/web/.dsh-market/state.json
if [ -f "$MARKET_STATE" ] && grep -q "dsh-workbench-project-console" "$MARKET_STATE" 2>/dev/null; then
  echo "── 工作台插件被禁用了，恢复它 ──"
  python3 - <<'PYEOF'
import json, io
p = '/home/averypi/.dsh/profiles/web/.dsh-market/state.json'
d = json.load(io.open(p, encoding='utf-8'))
d['disabled'] = [x for x in d.get('disabled', []) if x != 'dsh-workbench-project-console']
with io.open(p, 'w', encoding='utf-8') as f:
    f.write(json.dumps(d, ensure_ascii=False, indent=2) + '\n')
PYEOF
fi

STAMP=~/.dsh/project-console/ledger-stamp
if [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mmin +360 2>/dev/null)" ]; then
  echo "── 刷台账快照 ──"
  python3 export_repo_ledger.py || echo "⚠️ 台账快照刷新失败（不影响同步）"
  touch "$STAMP" 2>/dev/null || true
fi
exec python3 sync_notion_board.py --auto-mode
