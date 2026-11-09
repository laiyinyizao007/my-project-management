#!/usr/bin/env python3
"""把项目层字段同步到 GitHub Project #1「任务看板」的项目组条目上。

**只有一个板子了。** Project #1 里并存两类行：
  - 10 个**项目组条目**（顶层 Issue，住在各自的主仓，标签 `type: group` + `group:<key>`）
    → 项目层字段：分类 / 推进状态 / 评分 / 最近推送 / 仓库 / 栈 / 进度 / DDL / 负责人
  - 各仓的**任务** issue（挂在项目组下作 sub-issue，跨仓）
    → 任务层字段：Priority / Category / Size / Status / Sprint
GitHub 内置的 **Parent issue** 和 **Sub-issues progress** 自动表达层级与完成度。

项目组定义来自 project_registry.py（唯一来源），评分来自 tracked_config.json，
仓库事实来自 GitHub API —— 全部在本仓内，不依赖本机工作区。

写入策略（改之前先读）：
  机器字段  分类 / 评分 / 最近推送 / 仓库 / 栈   每次同步**覆盖** —— 它们是代码算出的事实
  人工字段  推进状态 / 进度 / DDL / 负责人          **同步脚本一次都不写** —— 连写同样的值都算写：
             一旦某次读不到现值，写默认值就等于抹掉你填的「休眠」。
             默认值只在建条目时给，由 setup_project_issues.py 负责。
  派生      GitHub 内置 Status 由「推进状态」映射（看板默认分组轴靠它）

用法：
  python3 sync_portfolio.py --dry-run     # 只看要做什么
  python3 sync_portfolio.py               # 真同步
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

from project_registry import (  # noqa: E402
    build_auto_groups,
    build_groups,
    load_workspace_tags,
)

TRACKED_CONFIG = BASE_DIR / "tracked_config.json"
DEFAULT_OWNER = "laiyinyizao007"

# ── 字段定义 ────────────────────────────────────────────────────
FIELDS = {
    "分类":     {"kind": "single", "machine": True},
    "评分":     {"kind": "number", "machine": True},
    "最近推送": {"kind": "date",   "machine": True},
    "仓库":     {"kind": "text",   "machine": True},
    "栈":       {"kind": "text",   "machine": True},
    "推进状态": {"kind": "single", "machine": False},
    "进度":     {"kind": "number", "machine": False},
    "DDL":      {"kind": "date",   "machine": False},
    "负责人":   {"kind": "text",   "machine": False},
}

HUMAN_DEFAULTS = {
    "推进状态": "进行中",
    "负责人": "我",
}

# 派生写入 GitHub 内置的 Status（看板默认分组轴）
DERIVED_STATUS = {
    "进行中":   "In Progress",
    "休眠":     "Todo",
    "归档候选": "Done",
}


# ── GraphQL ─────────────────────────────────────────────────────
def gql(query, variables=None):
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
  fields(first: 50) {
    nodes {
      __typename
      ... on ProjectV2Field { id name }
      ... on ProjectV2SingleSelectField { id name options { id name } }
      ... on ProjectV2MultiSelectField { id name }
      ... on ProjectV2IterationField { id name }
    }
  }
}
"""

# 看板条目单独分页拉：组 + 任务加起来会超过 100 个，
# 单次 items(first:100) 会把后面的整批漏掉（分组字段就写不上了）。
BOARD_ITEMS_QUERY = """
query($pid: ID!, $after: String) {
  node(id: $pid) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        nodes {
          id
          content {
            __typename
            ... on Issue { id title number }
            ... on DraftIssue { id title }
          }
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def fetch_all_items(pid):
    """看板全部条目（分页拉全）"""
    nodes = []
    after = None
    while True:
        data = gql(BOARD_ITEMS_QUERY, {"pid": pid, "after": after})
        items = data["node"]["items"]
        nodes.extend(items["nodes"])
        if not items["pageInfo"]["hasNextPage"]:
            break
        after = items["pageInfo"]["endCursor"]
    return nodes

REPO_ISSUES_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, states: [OPEN, CLOSED]) {
      nodes { id number title labels(first: 10) { nodes { name } } }
    }
  }
}
"""

ADD_TO_PROJECT = """
mutation($p: ID!, $c: ID!) {
  addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } }
}
"""

SET_FIELD = """
mutation($pid: ID!, $iid: ID!, $fid: ID!, $val: ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $pid, itemId: $iid, fieldId: $fid, value: $val}
  ) { projectV2Item { id } }
}
"""


# ── 数据采集 ────────────────────────────────────────────────────
def fetch_repos():
    raw = json.loads(gh_cli(
        "repo", "list", "--limit", "200", "--json",
        "name,description,primaryLanguage,stargazerCount,pushedAt,isArchived,isFork,isPrivate,url",
    ))
    repos = []
    for x in raw:
        repos.append({
            "name": x.get("name", ""),
            "description": x.get("description") or "",
            "primaryLanguage": (x.get("primaryLanguage") or {}).get("name", ""),
            "stargazerCount": x.get("stargazerCount", 0),
            "pushedAt": x.get("pushedAt", ""),
            "isArchived": x.get("isArchived", False),
            "isFork": x.get("isFork", False),
            "isPrivate": x.get("isPrivate", False),
            "url": x.get("url", ""),
        })
    repos.sort(key=lambda x: x["pushedAt"] or "", reverse=True)
    return repos


def load_scores():
    if not TRACKED_CONFIG.exists():
        return {}
    cfg = json.loads(TRACKED_CONFIG.read_text(encoding="utf-8"))
    out = {}
    for name, info in cfg.get("tracked_repos", {}).items():
        hist = info.get("score_history") or []
        if hist:
            out[name] = hist[-1].get("score", 0)
    return out


def repo_task_issues(owner, repo):
    """仓里的任务 issue（排除项目组条目）"""
    nodes = gql(REPO_ISSUES_QUERY, {"owner": owner, "repo": repo})["repository"]["issues"]["nodes"]
    out = []
    for n in nodes:
        labels = [l["name"] for l in n["labels"]["nodes"]]
        if "type: group" in labels or "type: project" in labels:
            continue
        out.append({"id": n["id"], "number": n["number"], "title": n["title"]})
    return out


def group_issue(owner, repo, key):
    """某个仓里带 group:<key> 标签的项目组条目"""
    nodes = gql(REPO_ISSUES_QUERY, {"owner": owner, "repo": repo})["repository"]["issues"]["nodes"]
    for n in nodes:
        labels = [l["name"] for l in n["labels"]["nodes"]]
        if "type: group" in labels and f"group:{key}" in labels:
            return n
    return None


# ── 同步 ────────────────────────────────────────────────────────
def field_value(kind, value):
    if value in (None, ""):
        return None
    if kind == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None
    if kind == "date":
        return {"date": str(value)[:10]}
    return {"text": str(value)}


def sync(project_number, owner, dry_run=False, auto=False, index=None):
    repos = fetch_repos()
    meta = {r["name"]: r for r in repos}
    groups = build_groups()
    if auto:
        # 自动立项的单仓组也要同步机器字段：分类 / 评分 / 最近推送 / 仓库 / 栈
        tags_by_repo = load_workspace_tags(index)
        groups = groups + build_auto_groups(repos, groups=build_groups(), tags_by_repo=tags_by_repo)
    scores = load_scores()

    print(f"📦 项目组 {len(groups)} 个 · Project #{project_number}")
    for g in groups:
        pr = meta.get(g["primary"])
        pushed = (pr["pushedAt"] or "")[:10] if pr else "—"
        score = scores.get(g["primary"], "—")
        print(f"   {g['category']:6s} {g['title']:24s} 主仓={g['primary']:26s} "
              f"组内仓 {len(g['repos'])}  评分={score} 最近推送={pushed}")

    if dry_run:
        print("\n🧪 --dry-run：到此为止，不触碰 Project。")
        return 0

    data = gql(PROJECT_QUERY, {"login": owner, "num": project_number})
    project = (data.get("repositoryOwner") or {}).get("projectV2")
    if not project:
        raise RuntimeError(f"找不到 Project #{project_number}。令牌需含 project scope。")
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
        raise RuntimeError(f"Project 缺少字段: {', '.join(missing)}")

    item_of_issue = {}
    item_values = {}
    for node in fetch_all_items(pid):
        content = node.get("content") or {}
        if content.get("__typename") == "Issue" and content.get("id"):
            item_of_issue[content["id"]] = node["id"]
        vals = {}
        for fv in (node.get("fieldValues") or {}).get("nodes", []):
            fname = (fv.get("field") or {}).get("name")
            if fname:
                vals[fname] = fv.get("name")
        item_values[node["id"]] = vals

    synced = added = task_tagged = 0
    for g in groups:
        iss = group_issue(owner, g["primary"], g["key"])
        if not iss:
            print(f"   ⚠ 找不到项目组条目（{g['primary']} group:{g['key']}），跳过 —— 先跑 setup_project_issues.py")
            continue
        item_id = item_of_issue.get(iss["id"])
        if not item_id:
            try:
                item_id = gql(ADD_TO_PROJECT, {"p": pid, "c": iss["id"]})['addProjectV2ItemById']['item']['id']
                added += 1
                print(f"   ✚ 加入看板 {g['title']}")
            except RuntimeError:
                print(f"   ⚠ 加入看板失败 {g['title']}")
                continue

        pr = meta.get(g["primary"], {})
        # 只写机器字段；人工字段一次都不碰
        values = {
            "分类": g["category"],
            "评分": scores.get(g["primary"]),
            "最近推送": (pr.get("pushedAt") or "")[:10],
            "仓库": pr.get("url") or f"https://github.com/{owner}/{g['primary']}",
            "栈": " · ".join(g["tags"][:4]),
        }
        for fname, value in values.items():
            spec = FIELDS[fname]
            val = field_value(spec["kind"], value)
            if val is None:
                continue
            if spec["kind"] == "single":
                opt_id = fields[fname]["options"].get(value)
                if not opt_id:
                    print(f"   ⚠ 字段 {fname} 没有选项「{value}」，跳过")
                    continue
                val = {"singleSelectOptionId": opt_id}
            gql(SET_FIELD, {"pid": pid, "iid": item_id, "fid": fields[fname]["id"], "val": val})

        # 派生写内置 Status（读「推进状态」现值，只读）
        builtin = fields.get("Status")
        if builtin and builtin.get("options"):
            cur = item_values.get(item_id, {})
            src = cur.get("推进状态") or HUMAN_DEFAULTS["推进状态"]
            opt = DERIVED_STATUS.get(src)
            opt_id = builtin["options"].get(opt) if opt else None
            if opt_id:
                gql(SET_FIELD, {"pid": pid, "iid": item_id, "fid": builtin["id"],
                                "val": {"singleSelectOptionId": opt_id}})
        synced += 1
        print(f"   ✓ {g['title']}（{g['primary']}）")

        # 把「分类」派生写到组内的任务行上 ——
        # 这样看板里就能「先按分类分组 → 组内再按 Parent issue 分组」，
        # 直接得到 分类 → 项目组 → 任务 三层。
        # 任务行的分类是从项目组派生的机器字段，允许覆盖。
        for rname in g["repos"]:
            for t in repo_task_issues(owner, rname):
                titem = item_of_issue.get(t["id"])
                if not titem:
                    continue
                opt_id = fields["分类"]["options"].get(g["category"])
                if not opt_id:
                    continue
                cur = item_values.get(titem, {})
                if cur.get("分类") == g["category"]:
                    continue          # 已是这个分类，不用重写
                gql(SET_FIELD, {"pid": pid, "iid": titem, "fid": fields["分类"]["id"],
                                "val": {"singleSelectOptionId": opt_id}})
                task_tagged += 1
                print(f"     ↳ {rname}#{t['number']} 分类 = {g['category']}")

    print(f"\n✅ 完成：同步 {synced} 个项目组，新加入看板 {added} 个，任务行标注分类 {task_tagged} 个")
    print("   人工字段（推进状态/进度/DDL/负责人）未被覆盖。")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="项目层字段 → Project #1 的项目组条目")
    ap.add_argument("--project-number", type=int, default=1)
    ap.add_argument("--owner", default=os.environ.get("PORTFOLIO_OWNER", DEFAULT_OWNER))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--auto", action="store_true",
                    help="把自动立项的单仓组也一起同步机器字段")
    ap.add_argument("--index", help="工作区索引路径")
    args = ap.parse_args()
    return sync(args.project_number, args.owner, args.dry_run, auto=args.auto, index=args.index)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
