#!/usr/bin/env python3
"""把项目层字段同步到 GitHub Project #1「任务看板」的项目条目上。

**只有一个板子了。** Project #1 里并存两类行：
  - 13 个**项目条目**（顶层 Issue，标签 `type: project` + `proj:<仓 slug>`）
    → 用项目层字段：模块 / 推进状态 / 评分 / 最近推送 / 仓库 / 栈 / 进度 / DDL / 负责人
  - 65 个**任务**（各仓的 issue，挂在项目条目下作 sub-issue）
    → 用任务层字段：Priority / Category / Size / Status / Sprint
GitHub 内置的 **Parent issue** 和 **Sub-issues progress** 自动表达层级与完成度。

项目定义来自 project_registry.py（= update_profile.py 的 PROJECT_SERIES + 工具仓列表），
评分来自 tracked_config.json，仓库事实来自 GitHub API —— 全部在本仓内，不依赖本机工作区。

写入策略（改之前先读）：
  机器字段  模块 / 评分 / 最近推送 / 仓库 / 栈   每次同步**覆盖** —— 它们是代码算出的事实
  人工字段  推进状态 / 进度 / DDL / 负责人          **同步脚本永不写** —— 连写同样的值都算写：
             一旦某次读不到现值，写默认值就等于抹掉你填的「休眠」。
             默认值由 setup_project_issues.py 在建条目时给。
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

from project_registry import build_registry  # noqa: E402

TRACKED_CONFIG = BASE_DIR / "tracked_config.json"
HOME_REPO = "my-project-management"
DEFAULT_OWNER = "laiyinyizao007"

# ── 字段定义 ────────────────────────────────────────────────────
# machine=True → 每次同步覆盖；machine=False → 仅创建时写默认值
FIELDS = {
    "模块":     {"kind": "single", "machine": True},
    "评分":     {"kind": "number", "machine": True},
    "最近推送": {"kind": "date",   "machine": True},
    "仓库":     {"kind": "text",   "machine": True},
    "栈":       {"kind": "text",   "machine": True},
    "推进状态": {"kind": "single", "machine": False},
    "进度":     {"kind": "number", "machine": False},
    "DDL":      {"kind": "date",   "machine": False},
    "负责人":   {"kind": "text",   "machine": False},
}

# 首次创建时人工字段的默认值
HUMAN_DEFAULTS = {
    "推进状态": "进行中",
    "负责人": "我",
}

# 派生写入 GitHub **内置**的 Status 字段（看板默认分组轴）。
# 自定义「推进状态」的三档更贴切，但分组轴只能在网页拖，所以两边都写。
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
  items(first: 100) {
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
  }
}
"""

PROJECT_ISSUES_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, states: [OPEN, CLOSED]) {
      nodes {
        id
        number
        title
        labels(first: 10) { nodes { name } }
      }
    }
  }
}
"""

ADD_TO_PROJECT = """
mutation($p: ID!, $c: ID!) {
  addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { projectV2Item { id } }
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


def project_issues(owner):
    """my-project-management 里带 type: project 标签的 issue → {proj_key: issue}"""
    nodes = gql(PROJECT_ISSUES_QUERY, {"owner": owner, "repo": HOME_REPO})["repository"]["issues"]["nodes"]
    out = {}
    for n in nodes:
        labels = [l["name"] for l in n["labels"]["nodes"]]
        if "type: project" not in labels:
            continue
        key = next((l[5:] for l in labels if l.startswith("proj:")), None)
        if key:
            out[key] = n
    return out


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


def sync(project_number, owner, dry_run=False):
    repos = fetch_repos()
    registry = build_registry(repos)
    scores = load_scores()

    print(f"📦 项目条目 {len(registry)} 个 · Project #{project_number}")
    for p in registry:
        score = scores.get(p["key"])
        print(f"   {p['module']:4s} {p['title']:30s} 仓库={p['key']:32s} "
              f"评分={score if score is not None else '—'} 最近推送={p['pushed'] or '—'}")

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

    # 看板条目：content.id 就是 issue 的 node id → 用它对上项目 issue
    item_of_issue = {}
    for node in project["items"]["nodes"]:
        content = node.get("content") or {}
        if content.get("__typename") == "Issue" and content.get("id"):
            item_of_issue[content["id"]] = node["id"]
    # item 上已有的单选字段值（派生内置 Status 时要读「推进状态」现值）
    item_values = {}
    for node in project["items"]["nodes"]:
        vals = {}
        for fv in (node.get("fieldValues") or {}).get("nodes", []):
            fname = (fv.get("field") or {}).get("name")
            if fname:
                vals[fname] = fv.get("name")
        item_values[node["id"]] = vals

    issues = project_issues(owner)
    synced = added = 0

    for p in registry:
        iss = issues.get(p["key"])
        if not iss:
            print(f"   ⚠ 找不到项目条目 issue（proj:{p['key']}），跳过 —— 先跑 setup_project_issues.py")
            continue
        item_id = item_of_issue.get(iss["id"])
        if not item_id:
            gql(ADD_TO_PROJECT, {"p": pid, "c": iss["id"]})
            added += 1
            item_id = None  # 下一轮才有 item；字段先跳过
            print(f"   ✚ 加入看板 {p['title']}")
        if not item_id:
            continue

        # 只写机器字段。人工字段（推进状态/进度/DDL/负责人）**绝不写** ——
        # 哪怕写的值和现值一样也算写：一旦某次读不到现值（字段没拉到），
        # 写默认值就等于抹掉你填的「休眠」。默认值只在建条目时给，
        # 由 setup_project_issues.py 负责。
        values = {
            "模块": p["module"],
            "评分": scores.get(p["key"]),
            "最近推送": p["pushed"],
            "仓库": p["repo_url"] or p["key"],
            "栈": p["stack"],
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

        # 派生写内置 Status（看板默认分组轴）。源是该项「推进状态」的**现值**（只读）
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
        print(f"   ✓ {p['title']}")

    print(f"\n✅ 完成：同步 {synced} 个项目条目，新加入看板 {added} 个")
    print("   人工字段（推进状态/进度/DDL/负责人）已保留，未被覆盖。")
    print(f"   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="项目层字段 → Project #1 的项目条目")
    ap.add_argument("--project-number", type=int, default=1, help="Project 编号（默认 1 = 任务看板）")
    ap.add_argument("--owner", default=os.environ.get("PORTFOLIO_OWNER", DEFAULT_OWNER))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    return sync(args.project_number, args.owner, args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
