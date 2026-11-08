#!/usr/bin/env python3
"""验证 sync_portfolio 的字段策略（mock，不需要令牌）。

必须守住的不变量：
  1. 机器字段（大组/评分/最近推送/仓库/栈）每次同步都刷新
  2. 人工字段（推进状态/进度/DDL/负责人）**一次都不写** —— 连写同样的值都算写
  3. 内置 Status 由「推进状态」派生，且读的是该项**现值**而不是默认值
  4. 任务行的大组由项目组派生，且只在不一致时才写

运行：python3 tests/test_sync_portfolio.py
"""
import sys, json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import sync_portfolio as sp

CALLS = []
ITEMS = {}        # item_id -> {issue_id, number, title, values}

FIELD_IDS = {
    "大组": "F_GRP", "评分": "F_SCORE", "最近推送": "F_PUSH", "仓库": "F_REPO",
    "栈": "F_STACK", "推进状态": "F_STAT", "进度": "F_PROG", "DDL": "F_DDL",
    "负责人": "F_OWN", "Status": "F_BUILTIN",
}
OPTS = {
    "大组": {"客户交付": "O_W", "个人产品": "O_P", "效率工具": "O_T"},
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
                    for k, v in meta["values"].items() if k in ("推进状态", "Status", "大组")
                ]},
            })
        return {"repositoryOwner": {"projectV2": {
            "id": "P1", "title": "任务看板",
            "fields": {"nodes": [
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["大组"], "name": "大组",
                 "options": [{"id": i, "name": n} for n, i in OPTS["大组"].items()]},
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["推进状态"], "name": "推进状态",
                 "options": [{"id": i, "name": n} for n, i in OPTS["推进状态"].items()]},
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["Status"], "name": "Status",
                 "options": [{"id": i, "name": n} for n, i in OPTS["Status"].items()]},
            ] + [{"__typename": "ProjectV2Field", "id": FIELD_IDS[n], "name": n}
                 for n in ("评分", "最近推送", "仓库", "栈", "进度", "DDL", "负责人")]},
            "items": {"nodes": nodes},
        }}}
    if "repository(owner: $owner, name: $repo)" in query:
        nodes = []
        for key in ("alpha", "beta"):
            nodes.append({"id": f"I_{key}", "number": 100, "title": f"组 {key}",
                          "labels": {"nodes": [{"name": "type: group"}, {"name": f"group:{key}"}]}})
        repo = variables["repo"]
        for n in (201, 202):
            nodes.append({"id": f"T_{repo}_{n}", "number": n, "title": f"任务 {n}",
                          "labels": {"nodes": [{"name": "bug"}]}})
        return {"repository": {"issues": {"nodes": nodes}}}
    if "updateProjectV2ItemFieldValue" in query:
        fid = variables["fid"]; name = NAME_OF[fid]
        for iid, meta in ITEMS.items():
            if variables["iid"] == iid:
                v = variables["val"]
                val = v.get("singleSelectOptionId")
                if val:
                    for opts in OPTS.values():
                        val = {i: n for n, i in opts.items()}.get(val, val)
                meta["values"][name] = val or v.get("number") or v.get("text") or v.get("date")
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["iid"]}}}
    if "addProjectV2ItemById" in query:
        return {"addProjectV2ItemById": {"item": {"id": "ITEM_NEW"}}}
    raise AssertionError("未预期的 query: " + query[:80])


sp.gql = fake_gql
sp.gh_cli = lambda *a: json.dumps([
    {"name": "alpha", "description": "", "primaryLanguage": {"name": "TypeScript"},
     "stargazerCount": 1, "pushedAt": "2026-11-08T00:00:00Z", "isArchived": False,
     "isFork": False, "isPrivate": False, "url": "https://github.com/x/alpha"},
    {"name": "beta", "description": "", "primaryLanguage": {"name": "Python"},
     "stargazerCount": 2, "pushedAt": "2026-11-07T00:00:00Z", "isArchived": False,
     "isFork": False, "isPrivate": False, "url": "https://github.com/x/beta"},
])
sp.load_scores = lambda: {"alpha": 20, "beta": 45}
sp.build_groups = lambda: [
    {"key": "alpha", "title": "组 alpha", "module": "客户交付", "primary": "alpha",
     "repos": ["alpha"], "desc": "", "tags": ["TypeScript"]},
    {"key": "beta", "title": "组 beta", "module": "个人产品", "primary": "beta",
     "repos": ["beta"], "desc": "", "tags": ["Python"]},
]


def setup():
    ITEMS.clear()
    for key in ("alpha", "beta"):
        ITEMS[f"ITEM_{key}"] = {"issue_id": f"I_{key}", "number": 100,
                                 "title": f"组 {key}", "values": {}}
    for key in ("alpha", "beta"):
        for n in (201, 202):
            ITEMS[f"ITEM_T_{key}_{n}"] = {"issue_id": f"T_{key}_{n}", "number": n,
                                           "title": f"任务 {n}", "values": {}}


print("══════ 场景 1：首次同步 ══════")
setup()
CALLS.clear()
sp.sync(1, "laiyinyizao007", dry_run=False)
w1 = [c for c in CALLS if "updateProjectV2ItemFieldValue" in c[0]]
names = [NAME_OF[c[1]["fid"]] for c in w1]
print(f"  写了 {len(w1)} 个字段值")
assert "评分" in names and "大组" in names and "仓库" in names, names
assert "推进状态" not in names, "同步脚本永不写人工字段"
assert "进度" not in names and "DDL" not in names and "负责人" not in names
print("  ✓ 只写机器字段；人工字段一次都没碰")

# 场景 1 里的任务行大组写入（第二次同步时已一致，不该再写）
task_writes_first = [c for c in CALLS
                     if c[1].get("fid") == FIELD_IDS["大组"] and c[1]["iid"].startswith("ITEM_T_")]
print(f"  任务行大组写入 {len(task_writes_first)} 次")
assert task_writes_first, "任务行应被标注大组"

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
for f in ("推进状态", "进度", "DDL", "负责人"):
    assert f not in names2, f"{f} 不应被同步覆盖"
assert {"大组", "评分", "最近推送", "仓库", "栈"} <= set(names2), "机器字段应继续刷新"
print("  ✓ 人工字段零写入；机器字段照常刷新")

print("\n══════ 场景 3：派生 Status 读现值 ══════")
w3 = [c for c in CALLS if c[1].get("fid") == FIELD_IDS["Status"]]
vals = [c[1]["val"].get("singleSelectOptionId") for c in w3]
print(f"  派生 Status 写入 {len(w3)} 次，取值: {vals}")
assert OPTS["Status"]["Todo"] in vals, "休眠(现值) 应派生为 Todo，而不是默认的 In Progress"
assert OPTS["Status"]["In Progress"] in vals
print("  ✓ 派生 Status 按各项现值映射")

print("\n══════ 场景 4：任务行的大组由项目组派生（一致时不重复写）═══════")
CALLS.clear()
sp.sync(1, "laiyinyizao007", dry_run=False)
again = [c for c in CALLS if c[1].get("fid") == FIELD_IDS["大组"] and c[1]["iid"].startswith("ITEM_T_")]
print(f"  第二次同步：任务行大组再写 {len(again)} 次（应只写不一致的）")
assert len(again) == 0, "大组已一致时不该重复写"
print("  ✓ 任务行大组只在不一致时写")

print("\n══════ 场景 5：dry-run 零写入 ══════")
CALLS.clear()
sp.sync(1, "laiyinyizao007", dry_run=True)
assert not [c for c in CALLS if "updateProjectV2ItemFieldValue" in c[0]], "dry-run 写了！"
print("  ✓ dry-run：零写入")

print("\n✅ 全部断言通过")
