#!/bin/bash
# 本机定时器跑的同步：先刷台账快照（工作台插件的「台账」页签读它），再同步 Notion 看板。
set -u
cd /home/averypi/Projects/my-project-management
# 台账快照 6 小时刷一次就够（它只是展示用的派生数据）
STAMP=~/.dsh/project-console/ledger-stamp
if [ ! -f "$STAMP" ] || [ -n "$(find "$STAMP" -mmin +360 2>/dev/null)" ]; then
  echo "── 刷台账快照 ──"
  python3 export_repo_ledger.py || echo "⚠️ 台账快照刷新失败（不影响同步）"
  touch "$STAMP" 2>/dev/null || true
fi
exec python3 sync_notion_board.py --auto-mode
