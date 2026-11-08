#!/usr/bin/env python3
"""项目组合 ↔ GitHub Project「项目组合」双向工具。

两个模式：

  建板子（一次性）
    python3 sync_portfolio.py --create-project
      → 创建 Project「项目组合」+ 9 个字段 + Table/看板 两个视图
      → 把 Project 编号写进仓库变量 PORTFOLIO_PROJECT_NUMBER

  灌数据（每周 / 手动）
    python3 sync_portfolio.py --project-number 3
      → 把 PROJECT_SERIES 的 9 个系列同步成 9 条 draft item，并写机器字段

数据源全部在本仓内，不依赖本机工作区 —— 所以 GitHub Actions 里跑得起来：

  update_profile.py    → PROJECT_SERIES / WORK|PERSONAL_SERIES_KEYS（项目定义的**唯一来源**，
                         不在这里重复维护一遍，避免和 profile.md 的项目展示漂移）
  tracked_config.json  → 质量评分（0-60）
  GitHub API           → 最近推送、stars、语言、描述

写入策略（重要，改之前先读）：

  机器字段  模块 / 评分 / 最近推送 / 仓库 / 栈   每次同步**覆盖** —— 它们由代码算出，是事实
  人工字段  进度 / DDL / 状态 / 负责人             只在**首次创建**时写默认值，之后**永不覆盖**
             否则每周同步会把你手填的进度和 DDL 抹掉 —— 这是这类同步最容易犯的错。

前置：令牌需含 project scope（读+写 user projects）。
  本机：gh auth 的令牌，或 GH_TOKEN 环境变量
  Actions：secrets.PROJECT_TOKEN
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# 项目定义直接复用 update_profile.py —— 单一来源
from update_profile import (  # noqa: E402
    PERSONAL_SERIES_KEYS,
    PROJECT_SERIES,
    WORK_SERIES_KEYS,
    find_latest_in_series,
)

TRACKED_CONFIG = BASE_DIR / "tracked_config.json"
DEFAULT_OWNER = "laiyinyizao007"
DEFAULT_TITLE = "项目组合"
DEFAULT_VARIABLE = "PORTFOLIO_PROJECT_NUMBER"

# ── 字段定义 ────────────────────────────────────────────────────
# kind: single / number / date / text
# machine=True → 每次同步覆盖；machine=False → 仅创建时写默认值
FIELDS = {
    "模块":     {"kind": "single", "machine": True,
                 "options": [("工作", "BLUE", "交付给客户/公司的项目"),
                             ("个人", "GREEN", "自研/个人项目")]},
    "评分":     {"kind": "number", "machine": True},
    "最近推送": {"kind": "date",   "machine": True},
    "仓库":     {"kind": "text",   "machine": True},
    "栈":       {"kind": "text",   "machine": True},
    "推进状态": {"kind": "single", "machine": False,
                 "options": [("进行中", "GREEN", "当前在推进"),
                             ("休眠", "GRAY", "暂时没动但没放弃"),
                             ("归档候选", "ORANGE", "长期未动，考虑归档")]},
    "进度":     {"kind": "number", "machine": False},
    "DDL":      {"kind": "date",   "machine": False},
    "负责人":   {"kind": "text",   "machine": False},
}

# 首次创建时人工字段的默认值
HUMAN_DEFAULTS = {
    "推进状态": "进行中",
    "负责人": "我",
}

# 派生写入 GitHub **内置**的 Status 字段（Todo / In Progress / Done）。
# 为什么要有：看板视图的默认分组轴是内置 Status，不写它的话 9 条全挤在
# "Todo" 一列里，看着像坏了。自定义「推进状态」有更贴切的三档
# （进行中/休眠/归档候选），但分组轴只能在网页拖，所以两边都写。
DERIVED_STATUS = {
    "进行中":   "In Progress",
    "休眠":     "Todo",
    "归档候选": "Done",
}


# ── GraphQL ─────────────────────────────────────────────────────
def gql(query, variables=None):
    """调 GitHub GraphQL。令牌走 GH_TOKEN 环境变量（不经过命令行）。"""
    payload = json.dumps({"query": query, "variables": variables or {}})
    env = dict(os.environ)
    r = subprocess.run(
        ["gh", "api", "graphql", "--input", "-"],
        input=payload, capture_output=True, text=True, env=env, timeout=180,
    )
    if r.returncode != 0:
        raise RuntimeError(f"gh api graphql 失败: {(r.stderr or r.stdout).strip()[:300]}")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"GraphQL 返回非 JSON: {r.stdout[:200]}") from e
    if data.get("errors"):
        msg = "; ".join(e.get("message", "?") for e in data["errors"])
        raise RuntimeError(f"GraphQL error: {msg}")
    return data["data"]


def gh_cli(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失败: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


OWNER_QUERY = """
query($login: String!) {
  repositoryOwner(login: $login) {
    __typename
    ... on User { id login }
    ... on Organization { id login }
  }
}
"""

CREATE_PROJECT = """
mutation($ownerId: ID!, $title: String!) {
  createProjectV2(input: { ownerId: $ownerId, title: $title }) {
    projectV2 { id number title }
  }
}
"""

PROJECT_QUERY = """
query($login: String!, $num: Int!) {
  repositoryOwner(login: $login) {
    ... on User { projectV2(number: $num) { ...proj } }
    ... on Organization { projectV2(number: $num) { ...proj } }
  }
}
fragment proj on ProjectV2 {
  id
  title
  fields(first: 40) {
    nodes {
      __typename
      ... on ProjectV2Field { id name }
      ... on ProjectV2SingleSelectField { id name options { id name } }
      ... on ProjectV2MultiSelectField { id name }
      ... on ProjectV2IterationField { id name }
    }
  }
  items(first: 100) {
    nodes {
      id
      content { ... on DraftIssue { id title body } }
      fieldValues(first: 20) {
        nodes {
          ... on ProjectV2ItemFieldSingleSelectValue {
            name
            field { ... on ProjectV2FieldCommon { name } }
          }
        }
      }
    }
  }
}
"""

LIST_PROJECTS = """
query($login: String!) {
  repositoryOwner(login: $login) {
    ... on User { projectsV2(first: 50) { nodes { number title } } }
    ... on Organization { projectsV2(first: 50) { nodes { number title } } }
  }
}
"""

CREATE_FIELD = """
mutation($pid: ID!, $name: String!, $dt: ProjectV2CustomFieldType!) {
  createProjectV2Field(input: { projectId: $pid dataType: $dt name: $name }) {
    projectV2Field { ... on ProjectV2Field { id name } }
  }
}
"""

CREATE_SELECT_FIELD = """
mutation($pid: ID!, $name: String!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {
  createProjectV2Field(input: { projectId: $pid dataType: SINGLE_SELECT name: $name singleSelectOptions: $opts }) {
    projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } }
  }
}
"""

CREATE_VIEW = """
mutation($pid: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {
  createProjectV2View(input: { projectId: $pid name: $name layout: $layout }) {
    projectV2View { id name }
  }
}
"""

ADD_DRAFT = """
mutation($pid: ID!, $title: String!, $body: String) {
  addProjectV2DraftIssue(input: { projectId: $pid, title: $title, body: $body }) {
    projectItem { id }
  }
}
"""

SET_FIELD = """
mutation($pid: ID!, $iid: ID!, $fid: ID!, $val: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $pid, itemId: $iid, fieldId: $fid, value: $val}
  ) { projectV2Item { id } }
}
"""

DELETE_ITEM = """
mutation($pid: ID!, $iid: ID!) {
  deleteProjectV2Item(input: {projectId: $pid, itemId: $iid}) { deletedItemId }
}
"""


# ── 数据采集 ────────────────────────────────────────────────────
def fetch_repos():
    """拉全部仓库元数据，按推送时间降序（find_latest_in_series 依赖这个顺序）。"""
    raw = json.loads(gh_cli(
        "repo", "list", "--limit", "200", "--json",
        "name,description,primaryLanguage,stargazerCount,pushedAt,isArchived,isFork,isPrivate,url",
    ))
    repos = []
    for x in raw:
        lang = (x.get("primaryLanguage") or {}).get("name", "")
        repos.append({
            "name": x.get("name", ""),
            "description": x.get("description") or "",
            "primaryLanguage": lang,
            "stargazerCount": x.get("stargazerCount", 0),
            "pushedAt": x.get("pushedAt", ""),
            "isArchived": x.get("isArchived", False),
            "isFork": x.get("isFork", False),
            "url": x.get("url", ""),
        })
    repos.sort(key=lambda x: x["pushedAt"] or "", reverse=True)
    return repos


def load_scores():
    """tracked_config.json → {仓库名: 最近一次质量评分}"""
    if not TRACKED_CONFIG.exists():
        return {}
    cfg = json.loads(TRACKED_CONFIG.read_text(encoding="utf-8"))
    out = {}
    for name, info in cfg.get("tracked_repos", {}).items():
        hist = info.get("score_history") or []
        if hist:
            out[name] = hist[-1].get("score", 0)
    return out


def build_items():
    """按 PROJECT_SERIES 组装待同步条目。"""
    repos = fetch_repos()
    scores = load_scores()
    items = []
    for keyword in WORK_SERIES_KEYS + PERSONAL_SERIES_KEYS:
        meta = PROJECT_SERIES.get(keyword)
        if not meta:
            continue
        module = "工作" if keyword in WORK_SERIES_KEYS else "个人"
        repo = find_latest_in_series(repos, keyword)
        repo_name = repo["name"] if repo else ""
        tags = list(meta["tags"])
        if repo and repo.get("primaryLanguage") and repo["primaryLanguage"] not in tags:
            tags = [repo["primaryLanguage"]] + tags
        items.append({
            "keyword": keyword,
            "title": f"{meta['icon']} {meta['name']}",
            "module": module,
            "score": scores.get(repo_name),
            "pushed": (repo["pushedAt"] or "")[:10] if repo else "",
            "repo": repo_name,
            "repo_url": repo["url"] if repo else "",
            "stack": " · ".join(tags[:4]),
            "summary": meta.get("desc", ""),
        })
    return items


# ── 模式一：建 Project ─────────────────────────────────────────
def create_project(owner, title, variable_name):
    print(f"🧭 创建 Project「{title}」（owner={owner}）")

    owner_data = gql(OWNER_QUERY, {"login": owner})
    owner_node = owner_data.get("repositoryOwner") or {}
    if not owner_node.get("id"):
        raise RuntimeError(f"找不到 owner {owner}")
    owner_id = owner_node["id"]
    print(f"   owner node id: {owner_id}")

    # 幂等：同名 Project 已存在就接着补字段，不重复建
    listed = gql(LIST_PROJECTS, {"login": owner})
    nodes = (listed.get("repositoryOwner") or {}).get("projectsV2", {}).get("nodes", [])
    hit = next((p for p in nodes if p.get("title") == title), None)
    if hit:
        number = hit["number"]
        print(f"   ⏭️  同名 Project 已存在（#{number}），接着补字段，不重复创建")
        pid = gql(PROJECT_QUERY, {"login": owner, "num": number})["repositoryOwner"]["projectV2"]["id"]
    else:
        r = gql(CREATE_PROJECT, {"ownerId": owner_id, "title": title})
        project = r["createProjectV2"]["projectV2"]
        pid, number = project["id"], project["number"]
        print(f"   ✅ Project 已创建：#{number}  ({pid})")

    # 字段
    data = gql(PROJECT_QUERY, {"login": owner, "num": number})
    existing = {f["name"] for f in data["repositoryOwner"]["projectV2"]["fields"]["nodes"] if f.get("name")}
    for fname, spec in FIELDS.items():
        if fname in existing:
            print(f"   ⏭️  字段 {fname} 已存在，跳过")
            continue
        if spec["kind"] == "single":
            opts = [{"name": n, "color": c, "description": d} for n, c, d in spec["options"]]
            gql(CREATE_SELECT_FIELD, {"pid": pid, "name": fname, "opts": opts})
        else:
            dt = {"number": "NUMBER", "date": "DATE", "text": "TEXT"}[spec["kind"]]
            gql(CREATE_FIELD, {"pid": pid, "name": fname, "dt": dt})
        print(f"   ✅ 字段 {fname} ({spec['kind']}) 已创建")

    # 视图
    data = gql(PROJECT_QUERY, {"login": owner, "num": number})
    view_names = set()
    for node in data["repositoryOwner"]["projectV2"]["fields"]["nodes"]:
        pass
    # 视图列表在 projectV2.views（部分 schema 版本才有），取不到就当作不存在，直接尝试创建
    views_raw = data["repositoryOwner"]["projectV2"].get("views")
    if isinstance(views_raw, dict):
        view_names = {v["name"] for v in views_raw.get("nodes", []) if v.get("name")}
    for vname, layout in (("Table", "TABLE_LAYOUT"), ("看板", "BOARD_LAYOUT")):
        if vname in view_names:
            print(f"   ⏭️  视图 {vname} 已存在，跳过")
            continue
        try:
            gql(CREATE_VIEW, {"pid": pid, "name": vname, "layout": layout})
            print(f"   ✅ 视图 {vname} 已创建")
        except RuntimeError as e:
            print(f"   ⚠️  视图 {vname} 创建失败（可能已存在）：{str(e)[:120]}")

    # 仓库变量
    try:
        repo_full = gh_cli("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
        gh_cli("variable", "set", variable_name, "--body", str(number), "--repo", repo_full)
        print(f"   ✅ 仓库变量 {variable_name}={number} 已设置（{repo_full}）")
    except RuntimeError as e:
        print(f"   ⚠️  设置仓库变量失败（不影响 Project 本身）：{e}")

    print(f"\n🎉 Project「{title}」#{number}")
    print(f"   网页：https://github.com/users/{owner}/projects/{number}")
    print(f"   灌数据：python3 sync_portfolio.py --project-number {number}")
    print(f"   看板分组需在网页把分组拖到「模块」列（GraphQL 不支持分组）")
    return number


# ── 模式二：同步 ───────────────────────────────────────────────
def field_value(kind, value, spec):
    """把 Python 值转成 ProjectV2FieldValue。返回 None 表示该字段不该写。"""
    if value in (None, ""):
        return None
    if kind == "single":
        if value not in [o[0] for o in spec.get("options", [])]:
            return None
        return {"singleSelectOptionId": None}   # 占位，稍后换成 option id
    if kind == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None
    if kind == "date":
        return {"date": str(value)[:10]}
    return {"text": str(value)}


def sync(project_number, owner, dry_run=False, prune=False):
    items = build_items()
    print(f"📦 项目组合：{len(items)} 个系列")
    for it in items:
        score = it["score"] if it["score"] is not None else "—"
        print(f"   {it['module']:4s} {it['title']:22s} 仓库={it['repo'] or '—':30s} "
              f"评分={score} 最近推送={it['pushed'] or '—'}")
        if not it["repo"]:
            print(f"        ⚠️ 系列「{it['keyword']}」在 GitHub 上找不到对应仓库 —— "
                  f"PROJECT_SERIES 里的关键词可能已失效（仓库被删/改名）")

    if dry_run:
        print("\n🧪 --dry-run：到此为止，不触碰 Project。")
        return 0

    data = gql(PROJECT_QUERY, {"login": owner, "num": project_number})
    project = (data.get("repositoryOwner") or {}).get("projectV2")
    if not project:
        raise RuntimeError(
            f"找不到 Project #{project_number}（owner={owner}）。"
            "令牌需含 project scope；或先跑 --create-project 建。"
        )
    pid = project["id"]
    print(f"\n✅ Project #{project_number} → {pid}")

    fields = {}
    for f in project["fields"]["nodes"]:
        if f.get("name"):
            fields[f["name"]] = {
                "id": f["id"],
                "options": {o["name"]: o["id"] for o in (f.get("options") or [])},
            }
    missing = [n for n in FIELDS if n not in fields]
    if missing:
        raise RuntimeError(f"Project 缺少字段: {', '.join(missing)}。先跑 --create-project 建字段。")

    existing = {}
    for node in project["items"]["nodes"]:
        content = node.get("content") or {}
        if not content.get("title"):
            continue
        # 读出该 item 当前的单选字段值（派生内置 Status 时要用「推进状态」的现值，
        # 不能用默认值——否则你标了「休眠」，内置 Status 会被写回 In Progress）
        cur = {}
        for fv in (node.get("fieldValues") or {}).get("nodes", []):
            fname = (fv.get("field") or {}).get("name")
            if fname:
                cur[fname] = fv.get("name")
        existing[content["title"]] = {"id": node["id"], "body": content.get("body") or "", "fields": cur}

    expected_titles = {it["title"] for it in items}
    created = synced = 0

    for it in items:
        is_new = it["title"] not in existing
        if is_new:
            body = it["summary"]
            if it["repo_url"]:
                body += f"\n\n仓库：{it['repo_url']}"
            r = gql(ADD_DRAFT, {"pid": pid, "title": it["title"], "body": body})
            item_id = r["addProjectV2DraftIssue"]["projectItem"]["id"]
            created += 1
            print(f"   ✚ 新建 {it['title']}")
        else:
            item_id = existing[it["title"]]["id"]

        values = {
            # 机器字段：每次覆盖
            "模块": it["module"],
            "评分": it["score"],
            "最近推送": it["pushed"],
            "仓库": it["repo_url"] or it["repo"],
            "栈": it["stack"],
        }
        # 人工字段：仅新建时给默认值，之后永不覆盖
        if is_new:
            for fname, default in HUMAN_DEFAULTS.items():
                values.setdefault(fname, default)

        for fname, value in values.items():
            spec = FIELDS[fname]
            val = field_value(spec["kind"], value, spec)
            if val is None:
                continue
            if spec["kind"] == "single":
                opt_id = fields[fname]["options"].get(value)
                if not opt_id:
                    print(f"   ⚠ 字段 {fname} 没有选项「{value}」，跳过")
                    continue
                val = {"singleSelectOptionId": opt_id}
            gql(SET_FIELD, {"pid": pid, "iid": item_id, "fid": fields[fname]["id"], "val": val})

        # 派生写内置 Status（看板默认分组轴），值由「推进状态」映射而来。
        # 新建项用刚写入的默认值；已有项读**现值**，保证不与人工填的状态打架。
        builtin = fields.get("Status")
        if builtin and builtin.get("options"):
            if is_new:
                src = values.get("推进状态") or HUMAN_DEFAULTS["推进状态"]
            else:
                src = existing[it["title"]]["fields"].get("推进状态") or HUMAN_DEFAULTS["推进状态"]
            opt = DERIVED_STATUS.get(src)
            opt_id = builtin["options"].get(opt) if opt else None
            if opt_id:
                gql(SET_FIELD, {"pid": pid, "iid": item_id, "fid": builtin["id"],
                                "val": {"singleSelectOptionId": opt_id}})
            else:
                print(f"   ⚠ 内置 Status 没有选项「{opt}」，跳过")

        synced += 1
        print(f"   ✓ 同步 {it['title']}")

    if prune:
        stale = [t for t, v in existing.items()
                 if t not in expected_titles and "github.com/" in v["body"]]
        for title in stale:
            gql(DELETE_ITEM, {"pid": pid, "iid": existing[title]["id"]})
            print(f"   🗑 删除陈旧条目 {title}")

    print(f"\n✅ 完成：新建 {created} 个，同步 {synced} 个，项目共 {len(items)} 个")
    print("   人工字段（进度/DDL/状态/负责人）已保留，未被覆盖。")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="项目组合 ↔ GitHub Project 工具")
    ap.add_argument("--create-project", action="store_true", help="创建 Project + 字段 + 视图")
    ap.add_argument("--project-number", type=int, help="Project 编号（同步时必填）")
    ap.add_argument("--owner", default=os.environ.get("PORTFOLIO_OWNER", DEFAULT_OWNER))
    ap.add_argument("--title", default=DEFAULT_TITLE)
    ap.add_argument("--variable-name", default=DEFAULT_VARIABLE)
    ap.add_argument("--dry-run", action="store_true", help="只打印将要做什么，不写任何东西")
    ap.add_argument("--prune", action="store_true", help="删除本脚本创建的陈旧条目")
    args = ap.parse_args()

    if args.create_project:
        create_project(args.owner, args.title, args.variable_name)
        return 0
    if not args.project_number:
        ap.error("同步需要 --project-number，或用 --create-project 先建 Project")
    return sync(args.project_number, args.owner, args.dry_run, args.prune)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
