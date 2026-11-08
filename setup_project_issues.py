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

from project_registry import build_groups  # noqa: E402

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
    """看板里某个 issue 的 item（含它当前的字段值）"""
    q = """
    query($login:String!,$num:Int!){
      repositoryOwner(login:$login){
        ... on User{
          projectV2(number:$num){
            items(first:100){
              nodes{
                id
                content{... on Issue{id}}
                fieldValues(first:20){nodes{
                  ... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}
                  ... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}}
                }}
              }
            }
          }
        }
      }
    }
    """
    data = gql(q, {"login": OWNER, "num": 1})['repositoryOwner']['projectV2']
    for node in data['items']['nodes']:
        c = node.get('content') or {}
        if c.get('id') == issue_id:
            vals = {}
            for fv in (node.get('fieldValues') or {}).get('nodes', []):
                nm = (fv.get('field') or {}).get('name')
                if nm:
                    vals[nm] = fv.get('name') or fv.get('text')
            return node['id'], vals
    return None, {}


def set_human_defaults(pid, item_id, current):
    """只在字段**还是空**的时候给默认值 —— 重跑不能覆盖你标过的「休眠」。"""
    fq = """
    query($login:String!,$num:Int!){
      repositoryOwner(login:$login){
        ... on User{
          projectV2(number:$num){
            id
            fields(first:50){nodes{
              ... on ProjectV2Field{id name}
              ... on ProjectV2SingleSelectField{id name options{id name}}
            }}
          }
        }
      }
    }
    """
    fdata = gql(fq, {"login": OWNER, "num": 1})['repositoryOwner']['projectV2']
    fmap = {f['name']: f for f in fdata['fields']['nodes'] if f.get('name')}
    for fname, default in HUMAN_DEFAULTS.items():
        if current.get(fname):
            continue          # 已有值 → 不碰
        f = fmap.get(fname)
        if not f:
            continue
        val = {"text": default}
        if f.get('options'):
            oid = next((o['id'] for o in f['options'] if o['name'] == default), None)
            if not oid:
                continue
            val = {"singleSelectOptionId": oid}
        gql(SET_FIELD, {"pid": fdata['id'], "iid": item_id, "fid": f['id'], "val": val})
        print(f"    · 默认 {fname} = {default}")


def main():
    ap = argparse.ArgumentParser(description="建立 项目组→任务 的原生 sub-issue 层级")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--group", help="只处理某个组 key")
    ap.add_argument("--project-number", type=int, default=1)
    args = ap.parse_args()

    repos = fetch_repos()
    known = {r["name"] for r in repos}
    groups = build_groups()
    if args.group:
        groups = [g for g in groups if g["key"] == args.group]
    proj = gql('query{user(login:"laiyinyizao007"){projectV2(number:%d){id}}}' % args.project_number)['user']['projectV2']
    pid = proj['id']

    print(f"📦 项目组 {len(groups)} 个；Project #{args.project_number} = {pid}\n")
    for g in groups:
        alive = [r for r in g["repos"] if r in known]
        missing = [r for r in g["repos"] if r not in known]
        print(f"  {g['category']:6s} {g['title']:26s} 主仓={g['primary']:26s} "
              f"组内仓 {len(alive)}/{len(g['repos'])}" + (f" ⚠ 缺 {missing}" if missing else ""))

    if args.dry_run:
        print("\n🧪 --dry-run：未做任何写入")
        return 0

    print("\n──── 执行 ────")
    linked = skipped = created = 0
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
        body += "\n<!-- 本 Issue 是该项目组在看板上的顶层条目；下面的子任务是组内各仓的具体工作项。 -->"

        issue = find_group_issue(primary, g["key"])
        if issue:
            print(f"  ⏭️  复用 {primary}#{issue['number']} {g['title']}")
        else:
            issue = gql(CREATE_ISSUE, {"repo": rid, "title": g["title"], "body": body,
                                       "labels": [type_label, key_label]})['createIssue']['issue']
            created += 1
            print(f"  ✚ 新建 {primary}#{issue['number']} {g['title']}")

        # 加进看板
        try:
            gql(ADD_TO_PROJECT, {"p": pid, "c": issue['id']})['addProjectV2ItemById']['item']
            print(f"  ✚ 已加入看板 {g['title']}")
        except RuntimeError as e:
            print(f"  · 已在看板里 {g['title']}")
        # 人工字段默认值：**只在字段还是空的时候**给 —— 重跑不能覆盖你标过的「休眠」
        try:
            item_id, cur = board_item(pid, issue['id'])
            if item_id:
                set_human_defaults(pid, item_id, cur)
        except RuntimeError as e:
            print(f"    ⚠ 设默认值失败: {str(e)[:110]}")

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
    print(f"\n✅ 新建项目组 {created} 个；新挂子任务 {linked} 个；已存在跳过 {skipped} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
