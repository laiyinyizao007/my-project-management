#!/usr/bin/env python3
"""项目登记表 —— 「项目 = 仓」的单一来源。

为什么是「项目 = 仓」：
  任务是**写在某个仓里**的 issue，它天然归属于那个仓所实现的项目。
  跨仓 sub-issue 已实测可用（父在 my-project-management，子在 lovable-life-hub），
  所以项目条目统一放在 my-project-management，任务挂在各自仓里。

两处来源合成：
  1. PROJECT_SERIES（update_profile.py）—— 9 个**精选项目**，用于个人主页展示，
     带 icon / 描述 / 技术栈 / 模块(工作|个人)
  2. TOOL_REPOS —— 你实际在维护、但不在精选列表里的**工具/系统仓**，
     它们同样有活跃 issue，同样是"你手上的项目"

模块三档：
  工作 = 交付给客户/公司的项目
  个人 = 自研/个人产品
  工具 = 自己用的工具链、系统、本仓库（DSH 桌面端、任务系统本身等）
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from update_profile import (  # noqa: E402
    PERSONAL_SERIES_KEYS,
    PROJECT_SERIES,
    WORK_SERIES_KEYS,
    find_latest_in_series,
)

# 精选项目 → 所属模块（工作 / 个人）—— 与 profile.md 的分组一致
SERIES_MODULE = {k: ("工作" if k in WORK_SERIES_KEYS else "个人")
                 for k in list(WORK_SERIES_KEYS) + list(PERSONAL_SERIES_KEYS)}

# 工具/系统仓：不在精选列表里，但你确实在维护它们
TOOL_REPOS = [
    {
        "key": "dsh-desktop",
        "name": "DSH Desktop",
        "icon": "🖥️",
        "module": "工具",
        "desc": "DeepSeek Harness 桌面版（DSH Desktop）",
        "tags": ["JavaScript", "Electron", "Desktop"],
    },
    {
        "key": "dsh-desktop-responsive",
        "name": "DSH Desktop Responsive",
        "icon": "📱",
        "module": "工具",
        "desc": "DSH Desktop 与 Web UI 的手机宽度布局",
        "tags": ["JavaScript", "Responsive"],
    },
    {
        "key": "issuefighter",
        "name": "IssueFighter",
        "icon": "⚔️",
        "module": "工具",
        "desc": "通过 GitHub Issue 与 Claude AI 协作的自动化平台",
        "tags": ["Python", "GitHub", "Agent"],
    },
    {
        "key": "github-weekly-progress",
        "name": "GitHub Weekly Progress",
        "icon": "📊",
        "module": "工具",
        "desc": "GitHub 仓库活跃度追踪 + 周报生成 + 个人主页自动维护",
        "tags": ["Python", "GitHub", "Report"],
    },
    {
        "key": "my-project-management",
        "name": "Project Management",
        "icon": "🗂️",
        "module": "工具",
        "desc": "个人任务管理系统（GitHub Issues + Projects）—— 任务系统本身",
        "tags": ["Python", "GitHub", "Automation"],
    },
]

# 显式的「系列 → 仓库」映射。
# 为什么不用 find_latest_in_series 的模糊匹配：它按关键词 in 名字 + 推送时间取最新，
# 于是 "arksu" 会匹配到 codex-cc-arksus（推送时间最新），把项目指到错的仓。
# 这里改成确定性的显式映射。
SERIES_REPO_OVERRIDE = {
    # Fidelity Craftsmen 的仓已改名 —— 它的 README（"AI赋能的职业健康管理解决方案，
    # 严格遵循 GBZ 188—2025"）与项目描述完全一致，就是现在的 arksusdemo
    "fidelity-craftsmen": "arksusdemo",
    # Arksu = ArkSUS 平台本体（1000+ 功能需求的文档库），不是它的某个 demo 副本
    "arksu": "arksus-yunzeanquan",
}

# 同项目的副本仓 → 归到同一个项目条目下
REPO_ALIAS = {
    "arksusdemo1": "arksusdemo",
    "arksusdemo2": "arksusdemo",
    "arksusdemo3": "arksusdemo",
}

# 真正查无此仓的系列（仓库被删除，没有 issue 可挂）
DEAD_SERIES = {"pact-nexus-light"}


def _resolve_series_repo(keyword, repos):
    """系列 → 代表仓（显式映射优先，其次才用模糊匹配）"""
    if keyword in SERIES_REPO_OVERRIDE:
        name = SERIES_REPO_OVERRIDE[keyword]
        hit = next((r for r in repos if r["name"] == name), None)
        return hit
    return find_latest_in_series(repos, keyword)


def build_registry(repos=None):
    """返回项目条目列表。

    repos: 仓库元数据列表（按推送时间降序）。为 None 时只返回不需要仓库信息的部分。
    每条：
      key      仓库 slug（同时用作标签 proj:<key>）
      title    Issue 标题（含 icon）
      module   工作 / 个人 / 工具
      desc     一句话说明
      tags     技术栈
      series   对应的 PROJECT_SERIES 键（工具仓为 None）
      stack    拼接后的技术栈字符串
    """
    out = []
    for keyword in list(WORK_SERIES_KEYS) + list(PERSONAL_SERIES_KEYS):
        meta = PROJECT_SERIES.get(keyword)
        if not meta or keyword in DEAD_SERIES:
            continue
        repo = _resolve_series_repo(keyword, repos or [])
        if not repo:
            continue
        tags = list(meta["tags"])
        if repo.get("primaryLanguage") and repo["primaryLanguage"] not in tags:
            tags = [repo["primaryLanguage"]] + tags
        out.append({
            "key": repo["name"],
            "title": f"{meta['icon']} {meta['name']}",
            "module": SERIES_MODULE[keyword],
            "desc": meta.get("desc", ""),
            "tags": tags,
            "series": keyword,
            "stack": " · ".join(tags[:4]),
            "repo_url": repo.get("url", ""),
            "pushed": (repo.get("pushedAt") or "")[:10],
            "language": repo.get("primaryLanguage", ""),
        })

    for t in TOOL_REPOS:
        repo = next((r for r in (repos or []) if r["name"] == t["key"]), None)
        tags = list(t["tags"])
        if repo and repo.get("primaryLanguage") and repo["primaryLanguage"] not in tags:
            tags = [repo["primaryLanguage"]] + tags
        out.append({
            "key": t["key"],
            "title": f"{t['icon']} {t['name']}",
            "module": t["module"],
            "desc": t["desc"],
            "tags": tags,
            "series": None,
            "stack": " · ".join(tags[:4]),
            "repo_url": repo.get("url", "") if repo else f"https://github.com/laiyinyizao007/{t['key']}",
            "pushed": (repo.get("pushedAt") or "")[:10] if repo else "",
            "language": repo.get("primaryLanguage", "") if repo else "",
        })
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(build_registry([]), ensure_ascii=False, indent=2))
