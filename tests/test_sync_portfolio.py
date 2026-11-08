#!/usr/bin/env python3
"""验证 sync_portfolio 的字段策略（mock，不需要令牌）。

三个必须守住的不变量：
  1. 机器字段（模块/评分/最近推送/仓库/栈）每次同步都刷新
  2. 人工字段（推进状态/进度/DDL/负责人）**永不**被同步覆盖
  3. 内置 Status 由「推进状态」派生，且读的是该项**现值**而不是默认值

运行：python3 tests/test_sync_portfolio.py
"""
import sys, json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import sync_portfolio as sp

CALLS = []
ITEMS = {}        # item_id -> {issue_node_id, number, title, values}
NEXT = [1]

FIELD_IDS = {
    "模块": "F_MOD", "评分": "F_SCORE", "最近推送": "F_PUSH", "仓库": "F_REPO",
    "栈": "F_STACK", "推进状态": "F_STAT", "进度": "F_PROG", "DDL": "F_DDL",
    "负责人": "F_OWN", "Status": "F_BUILTIN",
}
OPTS = {
    "模块": {"工作": "O_W", "个人": "O_P", "工具": "O_T"},
    "推进状态": {"进行中": "O_ACTIVE", "休眠": "O_IDLE", "归档候选": "O_ARCH"},
    "Status": {"Todo": "O_TODO", "In Progress": "O_WIP", "Done": "O_DONE"},
}
NAME_OF = {v: k for k, v in FIELD_IDS.items()}


def fake_gql(query, variables=None):
    variables = variables or {}
    CALLS.append((query, variables))

    if "projectV2(number: $num)" in query and "items(" in query:
        nodes = []
        for iid, meta in ITEMS.items():
            nodes.append({
                "id": iid,
                "content": {"__typename": "Issue", "id": meta["issue_id"],
                            "number": meta["number"], "title": meta["title"]},
                "fieldValues": {"nodes": [
                    {"name": v, "field": {"name": k}}
                    for k, v in meta["values"].items() if k in ("推进状态", "Status")
                ]},
            })
        return {"repositoryOwner": {"projectV2": {
            "id": "P1", "title": "任务看板",
            "fields": {"nodes": [
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["模块"], "name": "模块",
                 "options": [{"id": i, "name": n} for n, i in OPTS["模块"].items()]},
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["推进状态"], "name": "推进状态",
                 "options": [{"id": i, "name": n} for n, i in OPTS["推进状态"].items()]},
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["Status"], "name": "Status",
                 "options": [{"id": i, "name": n} for n, i in OPTS["Status"].items()]},
            ] + [{"__typename": "ProjectV2Field", "id": FIELD_IDS[n], "name": n}
                 for n in ("评分", "最近推送", "仓库", "栈", "进度", "DDL", "负责人")]},
            "items": {"nodes": nodes},
        }}}
    if "repository(owner: $owner, name: $repo)" in query:
        return {"repository": {"issues": {"nodes": [
            {"id": "I_" + k, "number": 100 + i, "title": f"项目 {k}",
             "labels": {"nodes": [{"name": "type: project"}, {"name": f"proj:{k}"}]}}
            for i, k in enumerate(["alpha", "beta"])
        ]}}}
    if "updateProjectV2ItemFieldValue" in query:
        fid = variables["fid"]; name = NAME_OF[fid]
        for iid, meta in ITEMS.items():
            if variables["iid"] == iid:
                v = variables["val"]
                val = v.get("singleSelectOptionId")
                if val:   # id → 还原成名称（与真实 API 的 fieldValues 一致）
                    for opts in OPTS.values():
                        val = {i: n for n, i in opts.items()}.get(val, val)
                meta["values"][name] = val or v.get("number") or v.get("text") or v.get("date")
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["iid"]}}}
    if "addProjectV2ItemById" in query:
        return {"addProjectV2ItemById": {"projectV2Item": {"id": "ITEM_NEW"}}}
    raise AssertionError("未预期的 query: " + query[:80])


sp.gql = fake_gql
sp.gh_cli = lambda *a: json.dumps([
    {"name": "alpha", "description": "", "primaryLanguage": {"name": "TypeScript"}, "stargazerCount": 1,
     "pushedAt": "2026-11-08T00:00:00Z", "isArchived": False, "isFork": False,
     "isPrivate": False, "url": "https://github.com/x/alpha"},
    {"name": "beta", "description": "", "primaryLanguage": {"name": "Python"}, "stargazerCount": 2,
     "pushedAt": "2026-11-07T00:00:00Z", "isArchived": False, "isFork": False,
     "isPrivate": False, "url": "https://github.com/x/beta"},
])
sp.load_scores = lambda: {"alpha": 20, "beta": 45}

# 注册表与项目 issue 都 mock 掉，只留两个项目条目，便于精确断言
sp.build_registry = lambda repos=None: [
    {"key": "alpha", "title": "项目 alpha", "module": "工作", "desc": "", "tags": [],
     "series": "alpha", "stack": "TypeScript", "repo_url": "https://github.com/x/alpha",
     "pushed": "2026-11-08", "language": "TypeScript"},
    {"key": "beta", "title": "项目 beta", "module": "个人", "desc": "", "tags": [],
     "series": "beta", "stack": "Python", "repo_url": "https://github.com/x/beta",
     "pushed": "2026-11-07", "language": "Python"},
]
sp.project_issues = lambda owner: {
    "alpha": {"id": "I_alpha", "number": 101, "title": "项目 alpha"},
    "beta":  {"id": "I_beta",  "number": 102, "title": "项目 beta"},
}


def setup():
    ITEMS.clear(); NEXT[0] = 1
    for k in ("alpha", "beta"):
        iid = f"ITEM{NEXT[0]}"; NEXT[0] += 1
        ITEMS[iid] = {"issue_id": f"I_{k}", "number": 100, "title": f"项目 {k}", "values": {}}


print("══════ 场景 1：首次同步 ══════")
setup()
CALLS.clear()
sp.sync(1, "laiyinyizao007", dry_run=False)
w1 = [c for c in CALLS if "updateProjectV2ItemFieldValue" in c[0]]
names = [NAME_OF[c[1]["fid"]] for c in w1]
print(f"  写了 {len(w1)} 个字段值")
assert "评分" in names and "模块" in names and "仓库" in names, names
assert "推进状态" not in names, "同步脚本永不写人工字段"
assert "进度" not in names and "DDL" not in names, "同步脚本永不写进度/DDL"
assert "负责人" not in names, "同步脚本永不写负责人"
print("  ✓ 只写机器字段；人工字段一次都不碰")

print("\n══════ 场景 2：二次同步 —— 人工字段不得被覆盖 ══════")
for meta in ITEMS.values():
    if meta["issue_id"] == "I_alpha":
        meta["values"]["推进状态"] = "休眠"
        meta["values"]["进度"] = 42
        meta["values"]["DDL"] = "2026-12-01"
CALLS.clear()
sp.sync(1, "laiyinyizao007", dry_run=False)
w2 = [c for c in CALLS if "updateProjectV2ItemFieldValue" in c[0]]
names2 = [NAME_OF[c[1]["fid"]] for c in w2]
print(f"  写了字段: {sorted(set(names2))}")
assert "推进状态" not in names2, "推进状态不应被同步覆盖"
assert "负责人" not in names2, "负责人不应被同步覆盖"
assert "进度" not in names2, "进度不应被同步覆盖"
assert "DDL" not in names2, "DDL 不应被同步覆盖"
assert {"模块", "评分", "最近推送", "仓库", "栈"} <= set(names2), "机器字段应继续刷新"
print("  ✓ 人工字段零写入；机器字段照常刷新")

print("\n══════ 场景 3：派生 Status 读现值，不是默认值 ══════")
w3 = [c for c in CALLS if c[1].get("fid") == FIELD_IDS["Status"]]
vals = [c[1]["val"].get("singleSelectOptionId") for c in w3]
print(f"  派生 Status 写入 {len(w3)} 次，取值: {vals}")
assert OPTS["Status"]["Todo"] in vals, "休眠(现值) 应派生为 Todo，而不是默认的 In Progress"
assert OPTS["Status"]["In Progress"] in vals, "beta(默认进行中) 应派生为 In Progress"
print("  ✓ 派生 Status 按各项现值映射")

print("\n══════ 场景 4：dry-run 零写入 ══════")
CALLS.clear()
sp.sync(1, "laiyinyizao007", dry_run=True)
assert not [c for c in CALLS if "updateProjectV2ItemFieldValue" in c[0]], "dry-run 写了！"
print("  ✓ dry-run：零写入")

print("\n✅ 全部断言通过")
