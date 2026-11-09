#!/usr/bin/env python3
"""建立「项目组 → 任务」的原生层级（GitHub Parent issue / Sub-issues）。

结构：
    分类（字段，看板里做第一层分组）
      └── 项目组（顶层 Issue，住在该组的**主仓**）
            └── 任务（组内各仓的 issue，跨仓 sub-issue）

做两件事，都幂等（可重复跑）：
  1. 在每个组的**主仓**建一个顶层 Issue，标签 `type: group` + `group:<key>`，
     并加进 Project #1 任务看板
  2. 把组内各仓的 issue 用 `addSubIssue` 挂到该项目组 Issue 下

GitHub 的 Projects v2 因此自动出现两个内置字段：
  Parent issue          —— 每个任务归属哪个项目组
  Sub-issues progress   —— 每个项目组的任务完成度（GitHub 自己算）

用法：
  python3 setup_project_issues.py --dry-run     # 只看要做什么
  python3 setup_project_issues.py               # 真执行
  python3 setup_project_issues.py --group dsh   # 只处理某组
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from project_registry import (  # noqa: E402
    build_auto_groups,
    build_groups,
    covered_repos,
    load_workspace_tags,
    skipped_repos,
)

OWNER = "laiyinyizao007"


def gql(query, variables=None):
    payload = json.dumps({"query": query, "variables": variables or {}})
    env = dict(os.environ)
    r = subprocess.run(["gh", "api", "graphql", "--input", "-"],
                       input=payload, capture_output=True, text=True, env=env, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"gh api graphql 失败: {(r.stderr or r.stdout).strip()[:300]}")
    d = json.loads(r.stdout)
    if d.get("errors"):
        raise RuntimeError("GraphQL error: " + "; ".join(e.get("message", "?") for e in d["errors"])[:400])
    return d["data"]


def rest(method, path, payload=None):
    cmd = ["curl", "-s", "-w", "\n%{http_code}", "-X", method,
           "-H", f"Authorization: Bearer {os.environ['GH_TOKEN']}",
           "-H", "Accept: application/vnd.github+json", "-H", "Content-Type: application/json",
           f"https://api.github.com{path}"]
    if payload is not None:
        cmd += ["--data-binary", json.dumps(payload)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    body, _, code = out.rpartition("\n")
    try:
        return (json.loads(body) if body.strip() else {}), code
    except json.JSONDecodeError:
        return {}, code


REPOS_ISSUE_CAPABILITY_QUERY = """
query($login:String!,$after:String){
  user(login:$login){
    repositories(first:100, after:$after, ownerAffiliations:OWNER,
                 orderBy:{field:PUSHED_AT, direction:DESC}){
      nodes{ name hasIssuesEnabled isArchived }
      pageInfo{ hasNextPage endCursor }
    }
  }
}
"""


def fetch_issue_capability():
    """{仓名: 是否开了 Issues} —— 关了 Issue 的仓建不了组条目（GitHub 硬限制）。"""
    out = {}
    after = None
    while True:
        data = gql(REPOS_ISSUE_CAPABILITY_QUERY, {"login": OWNER, "after": after})
        repos = data["user"]["repositories"]
        for n in repos["nodes"]:
            out[n["name"]] = bool(n["hasIssuesEnabled"])
        if not repos["pageInfo"]["hasNextPage"]:
            break
        after = repos["pageInfo"]["endCursor"]
    return out


def fetch_repos():
    raw = json.loads(subprocess.run(
        ["gh", "repo", "list", "--limit", "200", "--json",
         "name,description,primaryLanguage,stargazerCount,pushedAt,isArchived,isFork,isPrivate,url"],
        capture_output=True, text=True).stdout)
    repos = [{
        "name": x["name"], "description": x.get("description") or "",
        "primaryLanguage": (x.get("primaryLanguage") or {}).get("name", ""),
        "stargazerCount": x.get("stargazerCount", 0), "pushedAt": x.get("pushedAt", ""),
        "isArchived": x.get("isArchived", False), "isFork": x.get("isFork", False),
        "isPrivate": x.get("isPrivate", False), "url": x.get("url", ""),
    } for x in raw]
    repos.sort(key=lambda x: x["pushedAt"] or "", reverse=True)
    return repos


def repo_node_id(name):
    q = """
    query($owner:String!, $name:String!) {
      repository(owner:$owner, name:$name) { id nameWithOwner }
    }
    """
    return gql(q, {"owner": OWNER, "name": name})['repository']


def ensure_label(repo, name, color, desc):
    r, _ = rest('GET', f"/repos/{OWNER}/{repo}/labels/{name.replace(' ', '%20').replace(':', '%3A')}")
    if r.get('node_id'):
        return r['node_id']
    r, _ = rest('POST', f"/repos/{OWNER}/{repo}/labels",
                {"name": name, "color": color, "description": desc})
    if r.get('node_id'):
        return r['node_id']
    raise RuntimeError(f"建标签 {name} 失败: {r.get('message')}")


def find_group_issue(repo, key):
    """该仓里已存在的 type: group + group:<key> 的 issue"""
    q = """
    query($owner:String!, $repo:String!) {
      repository(owner:$owner, name:$repo) {
        issues(first:100, states:[OPEN,CLOSED]) {
          nodes { id number title labels(first:10) { nodes { name } } }
        }
      }
    }
    """
    nodes = gql(q, {"owner": OWNER, "repo": repo})['repository']['issues']['nodes']
    for n in nodes:
        labels = [l['name'] for l in n['labels']['nodes']]
        if 'type: group' in labels and f'group:{key}' in labels:
            return n
    return None


def repo_issues(repo):
    """仓里的**任务** issue（排除项目组条目本身 —— 它们是顶层，不能互为子任务）"""
    out = []
    for state in ("open", "closed"):
        r, _ = rest('GET', f"/repos/{OWNER}/{repo}/issues?state={state}&per_page=100")
        if not isinstance(r, list):
            continue
        for i in r:
            if 'pull_request' in i:
                continue
            labels = [l['name'] for l in i.get('labels', [])]
            if 'type: group' in labels or 'type: project' in labels:
                continue
            out.append({"number": i["number"], "title": i["title"], "state": i["state"]})
    return out


def issue_node_id(repo, number):
    q = """
    query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) { issue(number:$number) { id number title state } }
    }
    """
    return gql(q, {"owner": OWNER, "repo": repo, "number": number})['repository']['issue']


def current_children(repo, number):
    r, _ = rest('GET', f"/repos/{OWNER}/{repo}/issues/{number}/sub_issues")
    if not isinstance(r, list):
        return set()
    return {i['number'] for i in r}


ADD_TO_PROJECT = """
mutation($p:ID!, $c:ID!) {
  addProjectV2ItemById(input:{projectId:$p, contentId:$c}) { item { id } }
}
"""
ADD_SUB = """
mutation($parent:ID!, $child:ID!) {
  addSubIssue(input:{issueId:$parent, subIssueId:$child, replaceParent:true}) {
    issue { id } subIssue { id }
  }
}
"""
CREATE_ISSUE = """
mutation($repo:ID!, $title:String!, $body:String, $labels:[ID!]) {
  createIssue(input:{repositoryId:$repo, title:$title, body:$body, labelIds:$labels}) {
    issue { id number title }
  }
}
"""
SET_FIELD = """
mutation($pid:ID!, $iid:ID!, $fid:ID!, $val:ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(input:{projectId:$pid,itemId:$iid,fieldId:$fid,value:$val}) {
    projectV2Item { id }
  }
}
"""
# 人工字段的默认值 —— 只在**建条目**时给一次。
# 同步脚本（sync_portfolio.py）此后永不写这些字段，免得抹掉你填的值。
HUMAN_DEFAULTS = {"推进状态": "进行中", "负责人": "我"}


def board_item(pid, issue_id):
    """看板里某个 issue 的 item（含它当前的字段值）—— 全量分页查。

    条目会超过 100 个（组 + 任务），只取 first:100 会漏掉后面的，
    漏掉的条目就找不到，也就没法设默认值。
    """
    items = fetch_board_items(pid)
    return items.get(issue_id, (None, {}))


BOARD_ITEMS_QUERY = """
query($pid:ID!,$after:String){
  projectV2(id:$pid){
    items(first:100, after:$after){
      nodes{
        id
        content{... on Issue{id}}
        fieldValues(first:20){nodes{
          ... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}
          ... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}}
        }}
      }
      pageInfo{hasNextPage endCursor}
    }
  }
}
"""

PROJECT_FIELDS_QUERY = """
query($pid:ID!){
  projectV2(id:$pid){
    fields(first:50){nodes{
      ... on ProjectV2Field{id name}
      ... on ProjectV2SingleSelectField{id name options{id name}}
    }}
  }
}
"""


def fetch_board_items(pid):
    """看板全部条目：{issue_id: (item_id, {字段名: 值})}"""
    out = {}
    after = None
    while True:
        data = gql(BOARD_ITEMS_QUERY, {"pid": pid, "after": after})
        items = data["projectV2"]["items"]
        for node in items["nodes"]:
            content = node.get("content") or {}
            if not content.get("id"):
                continue
            vals = {}
            for fv in (node.get("fieldValues") or {}).get("nodes", []):
                nm = (fv.get("field") or {}).get("name")
                if nm:
                    vals[nm] = fv.get("name") or fv.get("text")
            out[content["id"]] = (node["id"], vals)
        if not items["pageInfo"]["hasNextPage"]:
            break
        after = items["pageInfo"]["endCursor"]
    return out


def fetch_fields(pid):
    """看板字段：{字段名: {id, options}}"""
    data = gql(PROJECT_FIELDS_QUERY, {"pid": pid})
    fmap = {}
    for f in data["projectV2"]["fields"]["nodes"]:
        if f.get("name"):
            fmap[f["name"]] = {"id": f["id"], "options": {o["name"]: o["id"] for o in (f.get("options") or [])}}
    return fmap


def set_human_defaults(pid, item_id, current, fmap=None):
    """只在字段**还是空**的时候给默认值 —— 重跑不能覆盖你标过的「休眠」。"""
    if fmap is None:
        fmap = fetch_fields(pid)
    for fname, default in HUMAN_DEFAULTS.items():
        if current.get(fname):
            continue          # 已有值 → 不碰
        f = fmap.get(fname)
        if not f:
            continue
        val = {"text": default}
        if f.get('options'):
            oid = f['options'].get(default)
            if not oid:
                continue
            val = {"singleSelectOptionId": oid}
        gql(SET_FIELD, {"pid": pid, "iid": item_id, "fid": f['id'], "val": val})
        print(f"    · 默认 {fname} = {default}")


def main():
    ap = argparse.ArgumentParser(description="建立 项目组→任务 的原生 sub-issue 层级")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", help="只处理某个组 key")
    ap.add_argument("--auto", action="store_true",
                    help="把「还没有归属」的活跃仓也自动立成单仓组")
    ap.add_argument("--index", help="工作区索引路径（默认 ../projects-index.json）")
    ap.add_argument("--project-number", type=int, default=1)
    args = ap.parse_args()

    repos = fetch_repos()
    known = {r["name"] for r in repos}
    # 有仓把 Issues 关了 —— 那些仓建不了组条目，先标记出来
    capability = fetch_issue_capability()
    for r in repos:
        r["hasIssuesEnabled"] = capability.get(r["name"], True)
    groups = build_groups()
    if args.group:
        groups = [g for g in groups if g["key"] == args.group]
    if args.auto:
        # 「还没有归属」的活跃仓 → 各建一个单仓组。
        # 归档仓、关了 Issues 的仓不建：GitHub 那边根本建不出条目。
        tags_by_repo = load_workspace_tags(args.index)
        auto = build_auto_groups(repos, groups=build_groups(), tags_by_repo=tags_by_repo)
        if args.group:
            auto = [g for g in auto if g["key"] == args.group]
        groups = groups + auto
        print(f"  ℹ️ 自动立项 {len(auto)} 个单仓组（依据：工作区索引的标签 + 归属规则）")
        skipped = skipped_repos(repos, groups=build_groups())
        if skipped:
            by_reason = {}
            for name, why in skipped:
                by_reason.setdefault(why, []).append(name)
            for why, names in by_reason.items():
                print(f"  ⚠️ {why}（{len(names)} 个）建不了组，只能留在工作区台账：")
                print("     " + ", ".join(sorted(names)))
    proj = gql('query{user(login:"laiyinyizao007"){projectV2(number:%d){id}}}' % args.project_number)['user']['projectV2']
    pid = proj['id']

    print(f"📦 项目组 {len(groups)} 个；Project #{args.project_number} = {pid}\n")
    for g in groups:
        alive = [r for r in g["repos"] if r in known]
        missing = [r for r in g["repos"] if r not in known]
        note = ""
        if g.get("auto"):
            note = f"  ← 自动立项，依据 {g.get('basis', '默认')}"
            if g.get("needs_review"):
                note = f"  ← ⚠️ {note.strip()}"
        print(f"  {g['category']:6s} {g['title']:26s} 主仓={g['primary']:26s} "
              f"组内仓 {len(alive)}/{len(g['repos'])}" + (f" ⚠ 缺 {missing}" if missing else "") + note)

    if args.dry_run:
        print("\n🧪 --dry-run：未做任何写入")
        return 0

    print("\n──── 执行 ────")
    linked = skipped = created = failed = 0
    group_issues = []          # (组, issue) —— 第二阶段统一设默认值
    for g in groups:
        primary = g["primary"]
        if primary not in known:
            print(f"  ⚠ 主仓 {primary} 不存在，跳过 {g['title']}")
            continue
        rid = repo_node_id(primary)['id']
        type_label = ensure_label(primary, "type: group", "8B5CF6",
                                  "项目组条目（顶层），其子任务是组内各仓的具体任务")
        key_label = ensure_label(primary, f"group:{g['key']}", "C5DEF5",
                                 f"归属项目组：{g['title']}")

        body = f"{g['desc']}\n\n- 分类：{g['category']}\n- 主仓：{primary}\n"
        body += f"- 组内仓：{', '.join(g['repos'])}\n- 技术栈：{' · '.join(g['tags'][:4])}\n"
        if g.get("auto"):
            # 自动立项的组，把归属依据写进去：看板上一眼能看出这个分类是怎么判出来的
            body += f"- 归属依据：{g.get('basis', '默认')}\n"
            body += "- 来源：自动立项（GitHub 上还没有 issue，先把坑位立起来）\n"
        body += "\n<!-- 本 Issue 是该项目组在看板上的顶层条目；下面的子任务是组内各仓的具体工作项。 -->"

        labels = [type_label, key_label]
        if g.get("needs_review"):
            # 规则没命中任何标签 → 挂「待归类」，等人来认领归属
            labels.append(ensure_label(primary, "待归类", "F59E0B",
                                       "归属靠规则没判出来，等人工确认分类"))

        try:
            issue = find_group_issue(primary, g["key"])
            if issue:
                print(f"  ⏭️  复用 {primary}#{issue['number']} {g['title']}")
            else:
                issue = gql(CREATE_ISSUE, {"repo": rid, "title": g["title"], "body": body,
                                           "labels": labels})['createIssue']['issue']
                created += 1
                print(f"  ✚ 新建 {primary}#{issue['number']} {g['title']}")
        except RuntimeError as e:
            # 一个组失败不能带崩整轮 —— 跳过，继续下一个
            failed += 1
            print(f"  ⚠️ 建组失败 {g['title']}（{primary}）：{str(e)[:110]}")
            continue

        # 加进看板
        try:
            gql(ADD_TO_PROJECT, {"p": pid, "c": issue['id']})['addProjectV2ItemById']['item']
            print(f"  ✚ 已加入看板 {g['title']}")
        except RuntimeError as e:
            print(f"  · 已在看板里 {g['title']}")
        group_issues.append((g, issue))

        have = current_children(primary, issue["number"])
        for rname in g["repos"]:
            if rname not in known:
                continue
            for t in repo_issues(rname):
                if t["number"] in have:
                    skipped += 1
                    continue
                try:
                    child = issue_node_id(rname, t["number"])
                    gql(ADD_SUB, {"parent": issue['id'], "child": child['id']})
                    linked += 1
                    print(f"  ↳ {g['title']:24s} ← {rname}#{t['number']} {t['title'][:42]}")
                except RuntimeError as e:
                    print(f"  ⚠ 挂载失败 {rname}#{t['number']}: {str(e)[:110]}")

    # 第二阶段：人工字段默认值。
    # 单独一轮是因为看板条目会超过 100 个，边建边查要么漏条目、
    # 要么每个组都重拉一遍全量 —— 拉一次全表（分页）就够了。
    print("\n──── 设人工字段默认值（只补空字段，不覆盖你填过的）────")
    try:
        items = fetch_board_items(pid)
        fmap = fetch_fields(pid)
        for g, issue in group_issues:
            item_id, cur = items.get(issue["id"], (None, {}))
            if item_id:
                set_human_defaults(pid, item_id, cur, fmap)
    except RuntimeError as e:
        print(f"  ⚠ 设默认值失败: {str(e)[:110]}")

    print(f"\n✅ 新建项目组 {created} 个；新挂子任务 {linked} 个；已存在跳过 {skipped} 个"
          + (f"；失败 {failed} 个" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
