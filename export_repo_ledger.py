#!/usr/bin/env python3
"""导出「全部 GitHub 仓」的台账 JSON，给 DSH 工作台插件的「台账」视图用。

为什么要有这个文件：
    看板上只放得下「能建 Issue 的仓」——归档仓和关了 Issues 的仓建不了条目。
    但「所有 GitHub 仓都有归属」是硬要求，所以这些仓要有个地方能看见。
    这里把全部仓连同归属、标签、状态一起导成一份 JSON，插件只读不判断。

用法：
    python3 export_repo_ledger.py            # 需要 GH_TOKEN（读 GitHub 元数据）
    python3 export_repo_ledger.py --offline  # 只用本地索引，不打 GitHub
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_registry import (  # noqa: E402
    CATEGORY_TOOL,
    DEFAULT_CATEGORY,
    build_auto_groups,
    build_groups,
    categorize,
    covered_repos,
    load_workspace_tags,
)

OUT = Path(__file__).resolve().parent / "repo-ledger.json"
TAGS_OUT = Path(__file__).resolve().parent / "repo-tags.json"
NOTES_OUT = Path(__file__).resolve().parent / "repo-notes.json"


def gh_repos():
    """GitHub 上的仓 + 归档/issues 状态（分页 GraphQL 一次拉完）。"""
    q = """
    query($login:String!,$after:String){
      user(login:$login){
        repositories(first:100, after:$after, ownerAffiliations:OWNER,
                     orderBy:{field:PUSHED_AT, direction:DESC}){
          nodes{ name hasIssuesEnabled isArchived isFork isPrivate pushedAt
                 description primaryLanguage{name} url }
          pageInfo{ hasNextPage endCursor }
        }
      }
    }
    """
    out = []
    after = None
    while True:
        payload = json.dumps({"query": q, "variables": {"login": "laiyinyizao007", "after": after}})
        r = subprocess.run(["gh", "api", "graphql", "--input", "-"], input=payload,
                           capture_output=True, text=True,
                           env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", "")}, timeout=180)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout)[:300])
        d = json.loads(r.stdout)
        if d.get("errors"):
            raise RuntimeError("; ".join(e.get("message", "?") for e in d["errors"])[:300])
        repos = d["data"]["user"]["repositories"]
        out.extend(repos["nodes"])
        if not repos["pageInfo"]["hasNextPage"]:
            break
        after = repos["pageInfo"]["endCursor"]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="不打 GitHub，只用本地索引")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    tags_by_repo = load_workspace_tags()
    explicit = build_groups()

    # 本地索引：给每个仓补上人工判定（origin/notes/最后提交）
    index_path = Path(__file__).resolve().parent.parent / "projects-index.json"
    local = {}
    if index_path.is_file():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        for key, entry in (data.get("projects") or {}).items():
            name = (entry.get("_auto") or {}).get("path") or key.split("/")[-1]
            local[name] = {
                "localPath": key,
                "origin": entry.get("origin"),
                "notes": entry.get("notes", ""),
                "head": (entry.get("_auto") or {}).get("head"),
                "lastCommit": (entry.get("_auto") or {}).get("lastCommit"),
                "sizeMb": (entry.get("_auto") or {}).get("sizeMb"),
            }
    else:
        # CI 上没有工作区索引 —— 用仓库里的备注快照兜底，别让人写的说明丢掉
        if NOTES_OUT.is_file():
            for name, note in json.loads(NOTES_OUT.read_text(encoding="utf-8")).items():
                local[name] = {"localPath": None, "origin": None, "notes": note,
                               "head": None, "lastCommit": None, "sizeMb": None}

    if args.offline:
        repos = []
        for name in sorted(set(tags_by_repo) | set(local)):
            repos.append({"name": name, "hasIssuesEnabled": True, "isArchived": False,
                          "isFork": False, "isPrivate": False, "pushedAt": "",
                          "description": "", "primaryLanguage": None,
                          "url": f"https://github.com/laiyinyizao007/{name}"})
    else:
        repos = gh_repos()

    # 看板里有哪些仓 = 显式组 + 自动立项的单仓组
    auto = build_auto_groups(repos, groups=explicit, tags_by_repo=tags_by_repo)
    groups = covered_repos(explicit + auto)

    rows = []
    for r in repos:
        name = r["name"]
        tags = sorted(tags_by_repo.get(name) or ())
        category, basis = categorize(tags)
        loc = local.get(name) or {}
        archived = bool(r.get("isArchived"))
        issues_off = not r.get("hasIssuesEnabled", True)
        rows.append({
            "name": name,
            "category": category,
            "basis": basis,
            "tags": tags,
            "archived": archived,
            "issuesDisabled": issues_off,
            "inBoard": name in groups and not archived and not issues_off,
            "groupKey": name if name in groups else None,
            "url": r.get("url") or f"https://github.com/laiyinyizao007/{name}",
            "language": (r.get("primaryLanguage") or {}).get("name", ""),
            "pushedAt": (r.get("pushedAt") or "")[:10],
            "origin": loc.get("origin"),
            "notes": loc.get("notes", ""),
            "localPath": loc.get("localPath"),
            "head": loc.get("head"),
            "lastCommit": loc.get("lastCommit"),
            "sizeMb": loc.get("sizeMb"),
            "isFork": bool(r.get("isFork")),
        })

    rows.sort(key=lambda x: (x["archived"], x["category"], x["name"].lower()))
    payload = {
        "generatedAt": subprocess.run(["date", "-Is"], capture_output=True, text=True).stdout.strip(),
        "owner": "laiyinyizao007",
        "defaultCategory": DEFAULT_CATEGORY,
        "counts": {
            "total": len(rows),
            "inBoard": sum(1 for x in rows if x["inBoard"]),
            "archived": sum(1 for x in rows if x["archived"]),
            "issuesDisabled": sum(1 for x in rows if x["issuesDisabled"]),
        },
        "repos": rows,
    }
    out = Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 顺带把标签和备注快照刷新掉 —— CI 上没工作区，靠这两份随仓库走
    TAGS_OUT.write_text(
        json.dumps({n: sorted(t) for n, t in sorted(tags_by_repo.items()) if t},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    NOTES_OUT.write_text(
        json.dumps({n: v.get("notes", "") for n, v in sorted(local.items()) if v.get("notes")},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"✓ 已导出 {len(rows)} 个仓 → {out}")
    print(f"   标签快照 → {TAGS_OUT}；备注快照 → {NOTES_OUT}")
    print("   分类分布:", json.dumps(
        {c: sum(1 for x in rows if x["category"] == c)
         for c in sorted({x["category"] for x in rows})}, ensure_ascii=False))
    print(f"   看板内 {payload['counts']['inBoard']} ｜ 归档 {payload['counts']['archived']} "
          f"｜ Issues 关闭 {payload['counts']['issuesDisabled']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
