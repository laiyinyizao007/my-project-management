#!/usr/bin/env python3
"""项目组登记表 —— 单一来源。

三层结构：

    大组（模块字段）      项目组（顶层 Issue，住在自己的主仓）    任务
    ─────────────        ──────────────────────────────       ────
    客户交付              🌿 Green Compass → green-compass-net    1 个 issue
    个人产品              🏥 Fidelity Craftsmen → arksusdemo      6 个
    效率工具              🖥️ DSH 组 → dsh-desktop               13 个
                          （含插件仓 dsh-desktop-responsive 等）

为什么是「项目组 = 一个仓或一族仓」：
  任务是**写在某个仓里**的 issue，它天然归属于那个仓。
  但**一族仓**（DSH 本体 + 它的插件）做的是同一件事，拆成多个组会割裂进度。
  跨仓 sub-issue 已实测可用（父在 dsh-desktop，子在 dsh-desktop-responsive），
  所以一个项目组的任务可以来自多个仓。

为什么「大组」用字段而不是 Issue：
  GitHub 的 sub-issue 官方只渲染一层。两层嵌套虽然 API 层面能建，
  但看板按「Parent issue」分组时，项目组（父=大组）和任务（父=项目组）
  会混在同一层，分组直接乱掉。用字段做第一层分组，视图里
  「先按大组分组 → 组内再按 Parent issue 分组」正好还原三层结构。

显式映射（不靠模糊关键词匹配）：
  find_latest_in_series("arksu") 会匹配到 codex-cc-arksus（推送时间最新），
  把项目指到错的仓 —— 所以这里全部写死。
"""

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from update_profile import (  # noqa: E402
    PERSONAL_SERIES_KEYS,
    PROJECT_SERIES,
    WORK_SERIES_KEYS,
)

# ── 大组（= 看板里的「模块」字段取值）──────────────────────────
GROUP_WORK = "客户交付"
GROUP_PERSONAL = "个人产品"
GROUP_TOOL = "效率工具"

# ── 项目组定义 ────────────────────────────────────────────────
# key       : 组标识（同时用作标签 group:<key>）
# title     : 顶层 Issue 标题
# module    : 所属大组
# primary   : 顶层 Issue 住在哪个仓（组的主仓）
# repos     : 这个组包含哪些仓的任务（primary 必须在其中）
# desc      : 一句话说明
# tags      : 技术栈
GROUPS = [
    # ── 客户交付 ──
    {"key": "green-compass", "title": "🌿 Green Compass", "module": GROUP_WORK,
     "primary": "green-compass-net", "repos": ["green-compass-net"],
     "desc": "碳足迹测算与管理平台", "tags": ["TypeScript", "React", "Sustainability"]},
    {"key": "arksu-demo", "title": "🏥 Fidelity Craftsmen", "module": GROUP_WORK,
     "primary": "arksusdemo", "repos": ["arksusdemo", "arksusdemo1", "arksusdemo2", "arksusdemo3"],
     "desc": "AI 职业健康管理 SaaS（GBZ 188-2025 合规）", "tags": ["TypeScript", "AI", "Health"]},
    {"key": "atomic-craft-ui", "title": "🎨 Atomic Craft UI", "module": GROUP_WORK,
     "primary": "atomic-craft-ui-5bcebc69", "repos": ["atomic-craft-ui-5bcebc69"],
     "desc": "Atomic UI 组件库 / 前端原子设计系统", "tags": ["TypeScript", "React", "Design System"]},
    {"key": "arksu-platform", "title": "🚀 Arksu", "module": GROUP_WORK,
     "primary": "arksus-yunzeanquan", "repos": ["arksus-yunzeanquan"],
     "desc": "ArkSUS 智能安全管理平台（1000+ 功能需求文档库）", "tags": ["TypeScript", "Safety"]},

    # ── 个人产品 ──
    {"key": "lifeos", "title": "🧠 LifeOS", "module": GROUP_PERSONAL,
     "primary": "lovable-life-hub", "repos": ["lovable-life-hub"],
     "desc": "事件驱动的个人管理系统（React+Vite+Supabase Edge）", "tags": ["TypeScript", "React", "Supabase", "LLM"]},
    {"key": "digital-twin", "title": "🌐 Digital Twin", "module": GROUP_PERSONAL,
     "primary": "my-digital-twin-8cc5688b", "repos": ["my-digital-twin-8cc5688b"],
     "desc": "交互式个人作品集（D3.js 力导向技能图）", "tags": ["TypeScript", "React", "D3.js"]},
    {"key": "github-rag-agent", "title": "🤖 GitHub RAG Agent", "module": GROUP_PERSONAL,
     "primary": "mygithubprojectagent", "repos": ["mygithubprojectagent"],
     "desc": "私有仓 RAG 问答 Agent（自动脱敏）", "tags": ["Python", "RAG", "LLM"]},
    {"key": "obsidian-brain", "title": "📓 Obsidian Second Brain", "module": GROUP_PERSONAL,
     "primary": "obs-averivendell", "repos": ["obs-averivendell"],
     "desc": "Obsidian 知识库（PARA + Git + 移动端）", "tags": ["JavaScript", "Obsidian", "Claude Code", "MCP"]},

    # ── 效率工具：DSH 家族（本体 + 插件）─────────────────────
    {"key": "dsh", "title": "🖥️ DSH 组", "module": GROUP_TOOL,
     "primary": "dsh-desktop",
     "repos": ["dsh-desktop", "dsh-desktop-responsive", "dsh-messenger-gateway", "dsh-audio-keepalive"],
     "desc": "DeepSeek Harness 桌面端 + 插件群（响应式布局 / Telegram 网关 / 音频保活）",
     "tags": ["JavaScript", "Electron", "Plugin"]},

    # ── 效率工具：个人自动化工具链 ────────────────────────────
    {"key": "task-system", "title": "🗂️ 任务系统组", "module": GROUP_TOOL,
     "primary": "my-project-management",
     "repos": ["my-project-management", "github-weekly-progress", "issuefighter", "tools"],
     "desc": "个人任务系统 + 周报追踪 + Issue 协作 + 小工具集",
     "tags": ["Python", "GitHub", "Automation"]},
]

# 精选项目 → 项目组的映射（用于校验登记表和 profile.md 没有脱节）
SERIES_TO_GROUP = {
    "green-compass": "green-compass",
    "fidelity-craftsmen": "arksu-demo",
    "atomic-craft-ui": "atomic-craft-ui",
    "arksu": "arksu-platform",
    "lovable-life-hub": "lifeos",
    "my-digital-twin": "digital-twin",
    "mygithubprojectagent": "github-rag-agent",
    "obs-averivendell": "obsidian-brain",
}

# 仓库已被删除、且没有替代的精选项目
DEAD_SERIES = {"pact-nexus-light"}


def build_groups():
    """项目组列表（附上仓库元数据，repos 里的仓可能不存在则过滤掉）。"""
    out = []
    for g in GROUPS:
        d = dict(g)
        d["repos"] = list(g["repos"])
        out.append(d)
    return out


def repo_meta(repos, name):
    """从仓库列表里取某个仓的元数据"""
    return next((r for r in repos if r["name"] == name), None)


if __name__ == "__main__":
    import json
    print(json.dumps(build_groups(), ensure_ascii=False, indent=2))
    print(f"\n精选项目 {len(WORK_SERIES_KEYS) + len(PERSONAL_SERIES_KEYS)} 个 → "
          f"项目组 {len(GROUPS)} 个；已删除仓库的精选项目: {sorted(DEAD_SERIES)}")
