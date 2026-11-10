#!/bin/bash
# 每周把「还没建组的活跃仓」建出来，并刷新看板上的机器字段。
#
# 这是 GitHub 侧的唯一入口：
#   setup_project_issues.py --auto  → 新仓的顶层组 Issue + 挂 sub-issue
#   sync_portfolio.py --auto        → 看板上的 分类/评分/最近推送/仓库/栈
#   export_repo_ledger.py          → 台账快照（工作台插件的「台账」页签读它）
set -u
cd /home/averypi/Projects/my-project-management
# ── 单实例租约（抄 CapGen 的做法：文件系统租约保证一个 profile 只有一个调度者）
# 没有它，两个 DSH 进程共用一个 profile 时会重复跑（回顾会重复建 issue）。
LOCK_DIR="$HOME/.dsh/project-console/locks"
mkdir -p "$LOCK_DIR"
LOCK_FILE="$LOCK_DIR/groups-setup.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "⏭️  已有同一任务在跑（租约被占），跳过"
  exit 0
fi
export GH_TOKEN="$(cat "$HOME/.new-gh-token")"
echo "── 建组（--auto）──"
python3 -u setup_project_issues.py --auto || echo "⚠️ 建组失败（不影响其它）"
echo "── 看板机器字段（--auto）──"
python3 -u sync_portfolio.py --auto || echo "⚠️ 字段同步失败"
echo "── 台账快照 ──"
python3 -u export_repo_ledger.py || echo "⚠️ 台账快照失败"
