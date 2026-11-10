#!/bin/bash
# 每周把「还没建组的活跃仓」建出来，并刷新看板上的机器字段。
#
# 这是 GitHub 侧的唯一入口：
#   setup_project_issues.py --auto  → 新仓的顶层组 Issue + 挂 sub-issue
#   sync_portfolio.py --auto        → 看板上的 分类/评分/最近推送/仓库/栈
#   export_repo_ledger.py          → 台账快照（工作台插件的「台账」页签读它）
set -u
cd /home/averypi/Projects/my-project-management
export GH_TOKEN="$(cat "$HOME/.new-gh-token")"
echo "── 建组（--auto）──"
python3 -u setup_project_issues.py --auto || echo "⚠️ 建组失败（不影响其它）"
echo "── 看板机器字段（--auto）──"
python3 -u sync_portfolio.py --auto || echo "⚠️ 字段同步失败"
echo "── 台账快照 ──"
python3 -u export_repo_ledger.py || echo "⚠️ 台账快照失败"
