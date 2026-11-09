#!/usr/bin/env python3
"""项目组登记表 —— 单一来源。

三层结构：

    分类（字段）          项目组（顶层 Issue，住在自己的主仓）    任务
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

为什么「分类」用字段而不是 Issue：
  GitHub 的 sub-issue 官方只渲染一层。两层嵌套虽然 API 层面能建，
  但看板按「Parent issue」分组时，项目组（父=分类）和任务（父=项目组）
  会混在同一层，分组直接乱掉。用字段做第一层分组，视图里
  「先按分类分组 → 组内再按 Parent issue 分组」正好还原三层结构。

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

# ── 分类（= 看板里的「分类」字段取值）──────────────────────────────
CATEGORY_WORK = "客户交付"
CATEGORY_PERSONAL = "个人产品"
CATEGORY_TOOL = "效率工具"

# ── 项目组定义 ────────────────────────────────────────────────
# key       : 组标识（同时用作标签 group:<key>）
# title     : 顶层 Issue 标题
# category : 所属分类
# primary   : 顶层 Issue 住在哪个仓（组的主仓）
# repos     : 这个组包含哪些仓的任务（primary 必须在其中）
# desc      : 一句话说明
# tags      : 技术栈
GROUPS = [
    # ── 客户交付 ──
    {"key": "green-compass", "title": "🌿 Green Compass", "category": CATEGORY_WORK,
     "primary": "green-compass-net", "repos": ["green-compass-net"],
     "desc": "碳足迹测算与管理平台", "tags": ["TypeScript", "React", "Sustainability"]},
    {"key": "arksu-demo", "title": "🏥 Fidelity Craftsmen", "category": CATEGORY_WORK,
     "primary": "arksusdemo", "repos": ["arksusdemo", "arksusdemo1", "arksusdemo2", "arksusdemo3"],
     "desc": "AI 职业健康管理 SaaS（GBZ 188-2025 合规）", "tags": ["TypeScript", "AI", "Health"]},
    {"key": "atomic-craft-ui", "title": "🎨 Atomic Craft UI", "category": CATEGORY_WORK,
     "primary": "atomic-craft-ui-5bcebc69",
     "repos": ["atomic-craft-ui-5bcebc69", "atomic-craft-ui"],
     "desc": "Atomic UI 组件库 / 前端原子设计系统", "tags": ["TypeScript", "React", "Design System"]},
    {"key": "arksu-platform", "title": "🚀 Arksu", "category": CATEGORY_WORK,
     "primary": "arksus-yunzeanquan", "repos": ["arksus-yunzeanquan"],
     "desc": "ArkSUS 智能安全管理平台（1000+ 功能需求文档库）", "tags": ["TypeScript", "Safety"]},

    # ── 客户交付：工作侧交付集合 ──────────────────────────
    # 这几个仓在 GitHub 上大多是 0 个 issue，但它们是真实在做的客户项目，
    # 之后会在仓里提 issue。所以先立项（组），任务随 issue 出现自然长出来。
    # 同源的仓并进同一个组，不重复立项。
    {"key": "approval-ment", "title": "✅ 审批管理系统", "category": CATEGORY_WORK,
     "primary": "20260405approval-ment-main",
     "repos": ["20260405approval-ment-main", "approval-ment"],
     "desc": "Vue3+TS monorepo 审批管理系统（pnpm workspaces + Turbo）", "tags": ["Vue", "TypeScript", "pnpm"]},
    {"key": "carbon-compass", "title": "🌍 Carbon Compass", "category": CATEGORY_WORK,
     "primary": "carbon-compass", "repos": ["carbon-compass", "remix-of-carbon-compass"],
     "desc": "能碳平台（Lovable + Cloudflare + Radix）", "tags": ["TypeScript", "React", "Cloudflare"]},
    {"key": "employee-assessment", "title": "📊 员工考核表", "category": CATEGORY_WORK,
     "primary": "employee-assessment-table", "repos": ["employee-assessment-table"],
     "desc": "员工考核表生成工具（Python + Markdown/Word）", "tags": ["Python", "文档"]},
    {"key": "prismtek-atomic-ui", "title": "🔷 PRISM OS", "category": CATEGORY_WORK,
     "primary": "prismtek-atomic-ui", "repos": ["prismtek-atomic-ui"],
     "desc": "PRISM OS 设计系统与业务前端（医疗器械销售场景）", "tags": ["TypeScript", "React", "Design System"]},
    {"key": "stagegate-flow", "title": "🚧 工程监理阶段关卡", "category": CATEGORY_WORK,
     "primary": "stagegate-flow-45", "repos": ["stagegate-flow-45"],
     "desc": "M1-M7 阶段关卡审查 SaaS", "tags": ["TypeScript", "React", "Drizzle"]},
    {"key": "system-blueprint", "title": "🗺️ 系统蓝图工作台", "category": CATEGORY_WORK,
     "primary": "system-blueprint-studio", "repos": ["system-blueprint-studio"],
     "desc": "架构设计可视化与文档化工作台", "tags": ["TypeScript", "React", "文档"]},
    {"key": "delivery-collection", "title": "🧰 工作侧交付集合", "category": CATEGORY_WORK,
     "primary": "averivendell-projects", "repos": ["averivendell-projects"],
     "desc": "n8n 连接器 + Telegram 机器人 + MCP 研究的交付集合", "tags": ["JavaScript", "n8n", "MCP"]},

    # ── 个人产品 ──
    {"key": "lifeos", "title": "🧠 LifeOS", "category": CATEGORY_PERSONAL,
     "primary": "lovable-life-hub", "repos": ["lovable-life-hub"],
     "desc": "事件驱动的个人管理系统（React+Vite+Supabase Edge）", "tags": ["TypeScript", "React", "Supabase", "LLM"]},
    {"key": "digital-twin", "title": "🌐 Digital Twin", "category": CATEGORY_PERSONAL,
     "primary": "my-digital-twin-8cc5688b", "repos": ["my-digital-twin-8cc5688b"],
     "desc": "交互式个人作品集（D3.js 力导向技能图）", "tags": ["TypeScript", "React", "D3.js"]},
    {"key": "github-rag-agent", "title": "🤖 GitHub RAG Agent", "category": CATEGORY_PERSONAL,
     "primary": "mygithubprojectagent", "repos": ["mygithubprojectagent"],
     "desc": "私有仓 RAG 问答 Agent（自动脱敏）", "tags": ["Python", "RAG", "LLM"]},
    {"key": "obsidian-brain", "title": "📓 Obsidian Second Brain", "category": CATEGORY_PERSONAL,
     "primary": "obs-averivendell", "repos": ["obs-averivendell"],
     "desc": "Obsidian 知识库（PARA + Git + 移动端）", "tags": ["JavaScript", "Obsidian", "Claude Code", "MCP"]},

    # ── 效率工具：DSH 家族（本体 + 插件）─────────────────────
    {"key": "dsh", "title": "🖥️ DSH 组", "category": CATEGORY_TOOL,
     "primary": "dsh-desktop",
     "repos": ["dsh-desktop", "dsh-desktop-responsive", "dsh-messenger-gateway", "dsh-audio-keepalive"],
     "desc": "DeepSeek Harness 桌面端 + 插件群（响应式布局 / Telegram 网关 / 音频保活）",
     "tags": ["JavaScript", "Electron", "Plugin"]},

    # ── 效率工具：个人自动化工具链 ────────────────────────────
    {"key": "task-system", "title": "🗂️ 任务系统组", "category": CATEGORY_TOOL,
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

# ── 归属规则（机器判定）──────────────────────────────────────
# 一个仓归到哪个「分类」，按这张表从上往下匹配，第一个命中的标签生效。
# 这是**猜**的结果：工作区索引里的人工判断（projects-index-seed.json）优先，
# 人工改过的标签会先命中这里更靠前的规则。
#
# 想改归属，改这张表 + 重跑同步即可，不用动代码。
TAG_CATEGORY_RULES = [
    (("client-work",), CATEGORY_WORK),
    (("family:life-system",), CATEGORY_PERSONAL),
    (("knowledge-base",), CATEGORY_PERSONAL),
    (("game",), CATEGORY_PERSONAL),
    (("hardware-iot",), CATEGORY_PERSONAL),
    (("personal-admin",), CATEGORY_TOOL),
    (("family:self-tools",), CATEGORY_TOOL),
    (("dataset",), CATEGORY_PERSONAL),
    # 其余技术仓归到「效率工具」。把标签逐个列出来，是为了让归属依据能写出
    # 具体名字（看板上能看出是哪个标签判的），而不是笼统一句「默认」。
    (("ai-agent", "automation", "tooling", "web-app", "infra-devops", "agent-config",
      "messaging-bot", "data-pipeline", "media-content", "security", "crypto-vpn",
      "learning-reference"), CATEGORY_TOOL),
]
# 一条标签都没命中时的兜底分类
DEFAULT_CATEGORY = CATEGORY_TOOL
# 兜底归类且没有任何标签依据 → 标「待归类」，等人工确认
NEEDS_REVIEW = "待归类"

# GitHub 上存在、但没克隆到工作区的仓：工作区索引里没有它们，
# 标签得在这里手写一份，不然归属只能靠「默认」猜。
EXTRA_TAGS = {
    "aws-skills": ["tooling", "learning-reference"],
    "bailian-speech-demo-local": ["tooling"],
    "bisheng": ["tooling", "learning-reference"],
    "claude": ["tooling", "agent-config"],
    "claude-epub-skill": ["tooling"],
    "dotfiles-private": ["agent-config", "family:self-tools"],
    "fork-pywxdump": ["tooling"],
    "linear-cli-skill": ["tooling", "family:self-tools"],
    "n8n-workflows": ["automation", "family:self-tools"],
    "test-api": ["tooling"],

    # ── 归档 / 未克隆的仓，按 README 判的性质 ──
    # 客户交付线的副本与测试仓
    "approval-ment": ["web-app", "client-work"],
    "arksusdemo": ["web-app", "ai-agent", "client-work"],
    "arksusdemo1": ["web-app", "client-work"],
    "arksusdemo2": ["tooling", "automation"],
    "arksusdemo3": ["tooling", "automation"],
    "dream-pixel-aligner-6ccf6b2e": ["web-app", "ai-agent", "client-work"],
    "green-compass-net-f3505092": ["web-app", "client-work"],
    "remix-of-remix-of-pixel-perfect-ui-81": ["web-app", "ai-agent", "client-work"],
    "my-digital-twin": ["web-app"],
    # 基础设施 / 运行时 / 集成
    "dsh-audio-keepalive": ["infra-devops", "tooling"],
    "averivendell-wsl-vscode-diagnostics": ["infra-devops", "tooling"],
    "averivendell-api-connection-tester": ["tooling"],
    "averivendell-github-mcp-research": ["tooling", "ai-agent"],
    "averivendell-cline-context-optimizer": ["agent-config", "tooling"],
    "averivendell-telegram-AIworkHorse": ["messaging-bot", "agent-config"],
    "averivendell-telegram-AnotherAvery_bot": ["messaging-bot", "automation"],
    "averivendell-telegram-groupChat": ["messaging-bot", "automation"],
    "averivendell-backup-manager": ["infra-devops", "tooling"],
    "averivendell-linux-learning-project": ["learning-reference"],
    "integrations": ["tooling", "automation"],
    "github-mcp-server": ["tooling", "ai-agent"],
    "development-tools-collection": ["tooling", "family:self-tools"],
    "figma-tools-collection": ["tooling", "web-app"],
    "rhinotools": ["tooling"],
    # AI 助手 / 规则
    "claude-code-best-practices": ["agent-config", "learning-reference"],
    "cline-global-rules": ["agent-config"],
    "config-test": ["agent-config"],
    "my-claude-code-setup": ["agent-config"],
    "sleepless-agent": ["ai-agent"],
    "rivenDell-DarwinAgent": ["ai-agent", "family:self-tools"],
    "rivenDell-Memory": ["ai-agent", "family:self-tools"],
    "rivenDell-Notion": ["agent-config", "family:self-tools"],
    # 产品（Lovable 生成的那些）
    "blueprint-zen-87-989e7a28": ["web-app"],
    "cityvibe-audio-guide": ["web-app"],
    "deployment-buddy": ["web-app"],
    "freshdesk-play-8ab3bf0f": ["web-app"],
    "laiyinyizao007.github.io": ["web-app"],
    "pixel-fortune-hub": ["web-app"],
    "pixel-perfect-replica": ["web-app"],
    "remix-of-safeguard-core": ["web-app"],
    "wander-tale-guide": ["web-app"],
    "chrono-wear-ai-publish": ["web-app", "ai-agent"],
    "smart-wardrobe-ai-system": ["web-app", "ai-agent"],
    # 知识 / 素材
    "academic-coursework-archive": ["knowledge-base", "learning-reference"],
    "learning-development-workspace": ["learning-reference", "knowledge-base"],
    "notion-api-research": ["learning-reference", "knowledge-base"],
    "unlocking-the-power-of-llms": ["learning-reference", "knowledge-base"],
    "neng-tan-guidebook": ["knowledge-base"],
    "safeworkpermitPRD": ["knowledge-base"],
    "RSS2CUBOX": ["automation"],
    "wewe-rss": ["data-pipeline", "automation"],
}

# 工作区索引（127 个项目的全量台账）：归属判定的标签来源
WORKSPACE_INDEX = Path(__file__).resolve().parent.parent / "projects-index.json"
# 仓库内标签快照（CI 上没有工作区索引，靠它判定归属）
REPO_TAGS_SNAPSHOT = Path(__file__).resolve().parent / "repo-tags.json"


def categorize(tags):
    """给一个仓判分类。返回 (分类, 依据)。

    依据会写进组条目的正文，这样看板上一眼能看出这个归属是怎么来的。
    """
    tags = set(tags or ())
    for rule_tags, category in TAG_CATEGORY_RULES:
        for t in rule_tags:
            if t in tags:
                return category, t
    if not tags:
        return DEFAULT_CATEGORY, NEEDS_REVIEW
    return DEFAULT_CATEGORY, "默认（无专用标签）"


def load_workspace_tags(index_path=None):
    """读每个仓的标签。

    三个来源合并成一个集合：
      1. EXTRA_TAGS      —— 没克隆到工作区的仓，手写在注册表里
      2. repo-tags.json  —— 仓库内快照（CI runner 上没有工作区，靠它）
      3. 工作区索引       —— 本机最新（人工判定都在这里面）
    """
    import json
    import os

    out = {name: set(tags) for name, tags in EXTRA_TAGS.items()}
    if REPO_TAGS_SNAPSHOT.is_file():
        try:
            snap = json.loads(REPO_TAGS_SNAPSHOT.read_text(encoding="utf-8"))
            for name, tags in snap.items():
                out.setdefault(name, set()).update(tags or ())
        except (OSError, ValueError):
            pass          # 快照坏了不该让同步挂掉 —— 下面的索引还能兜底
    path = Path(index_path or os.environ.get("PROJECTS_INDEX") or WORKSPACE_INDEX)
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        for key, entry in (data.get("projects") or {}).items():
            name = (entry.get("_auto") or {}).get("path") or key.split("/")[-1]
            out.setdefault(name, set()).update(entry.get("tags") or [])
    return out


def build_groups():
    """项目组列表（附上仓库元数据，repos 里的仓可能不存在则过滤掉）。"""
    out = []
    for g in GROUPS:
        d = dict(g)
        d["repos"] = list(g["repos"])
        out.append(d)
    return out


def covered_repos(groups=None):
    """已经被显式项目组覆盖的仓（这些仓不再自动另立一组）。"""
    out = set()
    for g in (groups if groups is not None else build_groups()):
        out.update(g["repos"])
    return out


def build_auto_groups(repos, groups=None, tags_by_repo=None):
    """给「还没有归属」的活跃仓各建一个单仓项目组。

    repos      : [{name, isArchived, hasIssuesEnabled, description, primaryLanguage, ...}]
    tags_by_repo: {仓名: {标签}}，来自工作区索引

    两种仓不建组：
      归档仓      —— GitHub 不允许在归档仓开 issue
      关了 Issues —— 同上，连建 Issue 都做不到（这两类只在工作区台账里可见）
    """
    covered = covered_repos(groups)
    tags_by_repo = tags_by_repo or {}
    out = []
    for r in repos:
        name = r["name"]
        if name in covered or r.get("isArchived"):
            continue
        if r.get("hasIssuesEnabled") is False:
            continue          # 关了 Issue 的仓建不了组条目
        category, basis = categorize(tags_by_repo.get(name))
        stack = [r.get("primaryLanguage")] if r.get("primaryLanguage") else []
        out.append({
            "key": name,
            "title": name,
            "category": category,
            "primary": name,
            "repos": [name],
            "desc": r.get("description") or "",
            "tags": stack,
            "auto": True,
            "basis": basis,
            "needs_review": basis == NEEDS_REVIEW,
        })
    return out


def skipped_repos(repos, groups=None):
    """建不了组的仓：归档的、关了 Issues 的。它们只在工作区台账里可见。"""
    covered = covered_repos(groups)
    out = []
    for r in repos:
        name = r["name"]
        if name in covered:
            continue
        if r.get("isArchived"):
            out.append((name, "已归档"))
        elif r.get("hasIssuesEnabled") is False:
            out.append((name, "Issues 已关闭"))
    return out


def repo_meta(repos, name):
    """从仓库列表里取某个仓的元数据"""
    return next((r for r in repos if r["name"] == name), None)


if __name__ == "__main__":
    import json
    print(json.dumps(build_groups(), ensure_ascii=False, indent=2))
    print(f"\n精选项目 {len(WORK_SERIES_KEYS) + len(PERSONAL_SERIES_KEYS)} 个 → "
          f"项目组 {len(GROUPS)} 个；已删除仓库的精选项目: {sorted(DEAD_SERIES)}")
