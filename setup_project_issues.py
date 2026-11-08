#!/usr/bin/env python3
"""建立「项目 → 任务」的原生层级（GitHub Parent issue / Sub-issues）。

做两件事，都幂等（可重复跑）：
  1. 在 my-project-management 仓为每个项目建一个**顶层 Issue**，
     标签 `type: project` + `proj:<仓库 slug>`，并加进 Project #1 任务看板
  2. 把各仓的 issue 通过 `addSubIssue` 挂到对应项目 Issue 下（**跨仓已实测可用**）

GitHub 的 Projects v2 里因此自动出现两个内置字段：
  Parent issue          —— 每个任务归属哪个项目
  Sub-issues progress   —— 每个项目的任务完成度（GitHub 自己算，不用我同步）

用法：
  python3 setup_project_issues.py --dry-run     # 只看要做什么
  python3 setup_project_issues.py               # 真执行
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from project_registry import build_registry, REPO_ALIAS  # noqa: E402

OWNER = "laiyinyizao007"
HOME_REPO = "my-project-management"


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
    cmd = ["curl", "-s", "-X", method, "-H", f"Authorization: Bearer {os.environ['GH_TOKEN']}",
           "-H", "Accept: application/vnd.github+json", "-H", "Content-Type: application/json",
           f"https://api.github.com{path}"]
    if payload is not None:
        cmd += ["--data-binary", json.dumps(payload)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return json.loads(r.stdout)


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


def repo_node_ids():
    q = """
    query {
      user(login:"laiyinyizao007") {
        home: repository(name:"my-project-management") { id }
        projects: projectV2(number:1) { id }
      }
    }
    """
    u = gql(q)['user']
    return u['home']['id'], u['projects']['id']


def ensure_label(repo, name, color, desc):
    r = rest('GET', f"/repos/{OWNER}/{repo}/labels/{name.replace(' ', '%20').replace(':', '%3A')}")
    if r.get('node_id'):
        return r['node_id']
    r = rest('POST', f"/repos/{OWNER}/{repo}/labels",
             {"name": name, "color": color, "description": desc})
    if r.get('node_id'):
        return r['node_id']
    raise RuntimeError(f"建标签 {name} 失败: {r.get('message')}")


def existing_project_issues():
    """my-project-management 里已存在的 type: project issue → {proj_key: issue}"""
    q = """
    query {
      user(login:"laiyinyizao007") {
        repository(name:"my-project-management") {
          issues(first:100, states:[OPEN,CLOSED]) {
            nodes { id number title labels(first:10) { nodes { name } } }
          }
        }
      }
    }
    """
    nodes = gql(q)['user']['repository']['issues']['nodes']
    out = {}
    for n in nodes:
        labels = [l['name'] for l in n['labels']['nodes']]
        if 'type: project' not in labels:
            continue
        key = next((l[5:] for l in labels if l.startswith('proj:')), None)
        if key:
            out[key] = n
    return out


SET_FIELD = """
mutation($pid:ID!, $iid:ID!, $fid:ID!,$val:ProjectV2FieldValue!) {
  updateProjectV2ItemFieldValue(input:{projectId:$pid,itemId:$iid,fieldId:$fid,value:$val}) {
    projectV2Item { id }
  }
}
"""
# 人工字段的默认值 —— 只在**建条目**时给一次。
# 同步脚本（sync_portfolio.py）此后永不写这些字段，免得抹掉你填的值。
HUMAN_DEFAULTS = {"推进状态": "进行中", "负责人": "我"}


def add_to_project(project_id, issue_id):
    q = """
    mutation($p:ID!, $c:ID!) {
      addProjectV2ItemById(input:{projectId:$p, contentId:$c}) { projectV2Item { id } }
    }
    """
    try:
        item = gql(q, {"p": project_id, "c": issue_id})['addProjectV2ItemById']['projectV2Item']
        return item['id']
    except RuntimeError:
        return None   # 已在看板里


def repo_issues(repo):
    """仓里的**任务** issue（排除项目条目本身 —— 它们是顶层，不能互为子任务）"""
    out = []
    for state in ("open", "closed"):
        r = rest('GET', f"/repos/{OWNER}/{repo}/issues?state={state}&per_page=100")
        if isinstance(r, dict):
            continue
        for i in r:
            if 'pull_request' in i:
                continue
            labels = [l['name'] for l in i.get('labels', [])]
            if 'type: project' in labels:
                continue          # 项目条目是顶层，不能挂成子任务
            out.append({"number": i["number"], "title": i["title"], "state": i["state"],
                        "id": None})
    return out


def issue_node_id(repo, number):
    q = """
    query($owner:String!, $repo:String!, $number:Int!) {
      repository(owner:$owner, name:$repo) { issue(number:$number) { id number title state } }
    }
    """
    r = gql(q, {"owner": OWNER, "repo": repo, "number": number})
    return r['repository']['issue']


def current_children(parent_issue_number):
    r = rest('GET', f"/repos/{OWNER}/{HOME_REPO}/issues/{parent_issue_number}/sub_issues")
    if isinstance(r, dict):
        return set()
    return {i['number'] for i in r}


def main():
    ap = argparse.ArgumentParser(description="建立 项目→任务 的原生 sub-issue 层级")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-closed", action="store_true", help="只挂未关闭的 issue")
    args = ap.parse_args()

    repos = fetch_repos()
    registry = build_registry(repos)
    home_id, project_id = repo_node_ids()
    print(f"📦 项目条目 {len(registry)} 个；Project #1 = {project_id}")

    have = existing_project_issues()
    # 建标签 / 建 Issue 都是远端写操作 —— dry-run 阶段一律不碰
    label_cache = {}

    def get_label(name, color, desc):
        if name not in label_cache:
            label_cache[name] = ensure_label(HOME_REPO, name, color, desc) if not args.dry_run else None
        return label_cache[name]

    plan = []
    for p in registry:
        key = p["key"]
        body = f"{p['desc']}\n\n- 仓库：{p['repo_url']}\n- 技术栈：{p['stack']}\n- 模块：{p['module']}\n"
        body += "\n<!-- 本 Issue 是该项目在看板上的顶层条目；下面的子任务（sub-issue）才是具体工作项。 -->"

        issue = have.get(key)
        if issue:
            action = "复用"
        elif args.dry_run:
            issue = {"id": None, "number": "—", "title": p["title"]}
            action = "待建"
        else:
            proj_label = get_label(f"proj:{key}", "C5DEF5", f"归属项目：{p['title']}")
            type_label = get_label("type: project", "8B5CF6", "项目条目（顶层），其子任务是项目下的具体任务")
            q = """
            mutation($repo:ID!, $title:String!, $body:String, $labels:[ID!]) {
              createIssue(input:{repositoryId:$repo, title:$title, body:$body, labelIds:$labels}) {
                issue { id number title }
              }
            }
            """
            issue = gql(q, {"repo": home_id, "title": p["title"], "body": body,
                            "labels": [type_label, proj_label]})['createIssue']['issue']
            action = "新建"

        # 该项目名下所有仓的 issue（主仓 + 别名副本仓）
        repos_for_project = [key] + [r for r, target in REPO_ALIAS.items() if target == key]
        tasks = []
        for rname in repos_for_project:
            for it in repo_issues(rname):
                if rname == HOME_REPO and it["number"] == issue["number"]:
                    continue
                tasks.append((rname, it))

        plan.append({"project": p, "issue": issue, "action": action, "tasks": tasks})

    print()
    for row in plan:
        p, iss = row["project"], row["issue"]
        open_n = sum(1 for _, t in row["tasks"] if t["state"] == "open")
        print(f"  {row['action']}  #{iss['number']:<4} {p['title']:30s} "
              f"任务 {len(row['tasks'])} 个（未关闭 {open_n}）  模块={p['module']}")

    if args.dry_run:
        print("\n🧪 --dry-run：未做任何写入")
        return 0

    print("\n──── 执行 ────")
    linked = skipped = 0
    for row in plan:
        p, iss = row["project"], row["issue"]
        item_id = add_to_project(project_id, iss["id"])
        if item_id is None:
            print(f"  · {p['title']} 已在看板里")
        else:
            print(f"  ✚ 加入看板 {p['title']}")
            # 新条目：给人工字段的默认值（只此一次，之后同步脚本永不碰）
            try:
                fq = """
                query($login:String!,$num:Int!){
                  repositoryOwner(login:$login){... on User{projectV2(number:$num){
                    fields(first:50){nodes{... on ProjectV2Field{id name}
                      ... on ProjectV2SingleSelectField{id name options{id name}}}}}}}}
                }
                """
                fdata = gql(fq, {"login": OWNER, "num": 1})['repositoryOwner']['projectV2']
                fmap = {f['name']: f for f in fdata['fields']['nodes'] if f.get('name')}
                for fname, default in HUMAN_DEFAULTS.items():
                    f = fmap.get(fname)
                    if not f:
                        continue
                    val = {"text": default}
                    if f.get('options'):
                        oid = next((o['id'] for o in f['options'] if o['name'] == default), None)
                        if not oid:
                            continue
                        val = {"singleSelectOptionId": oid}
                    gql(SET_FIELD, {"pid": fdata['id'], "iid": item_id,
                                    "fid": f['id'], "val": val})
            except RuntimeError as e:
                print(f"    ⚠ 设默认值失败（不影响层级）: {str(e)[:100]}")

        have_children = current_children(iss["number"])
        for rname, t in row["tasks"]:
            if t["number"] in have_children:
                skipped += 1
                continue
            if args.skip_closed and t["state"] != "open":
                continue
            try:
                child = issue_node_id(rname, t["number"])
                q = """
                mutation($parent:ID!, $child:ID!) {
                  addSubIssue(input:{issueId:$parent, subIssueId:$child}) {
                    issue { id } subIssue { id }
                  }
                }
                """
                gql(q, {"parent": iss["id"], "child": child["id"]})
                linked += 1
                print(f"  ↳ {p['title']:26s} ← {rname}#{t['number']} {t['title'][:44]}")
            except RuntimeError as e:
                print(f"  ⚠ 挂载失败 {rname}#{t['number']}: {str(e)[:120]}")
    print(f"\n✅ 新挂 {linked} 个子任务，已存在跳过 {skipped} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
