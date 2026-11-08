#!/usr/bin/env python3
"""用 mock 验证 sync_portfolio 的字段保护与幂等逻辑（不需要令牌）。

运行：python3 tests/test_sync_portfolio.py
"""
import sys, json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import sync_portfolio as sp

CALLS = []
STATE = {"items": {}, "next_id": 1}

FIELD_IDS = {"模块": "F_MOD", "评分": "F_SCORE", "最近推送": "F_PUSH", "仓库": "F_REPO",
             "栈": "F_STACK", "状态": "F_STAT", "进度": "F_PROG", "DDL": "F_DDL", "负责人": "F_OWNER"}
OPT_IDS = {("模块", "工作"): "O_WORK", ("模块", "个人"): "O_PERS",
           ("状态", "进行中"): "O_ACTIVE", ("状态", "休眠"): "O_IDLE", ("状态", "归档候选"): "O_ARCH"}

def fake_gql(query, variables=None):
    variables = variables or {}
    CALLS.append((query.strip().split("\n")[0][:40], variables))

    if "createProjectV2(" in query:
        return {"createProjectV2": {"projectV2": {"id": "P1", "number": 99, "title": "项目组合"}}}
    if "OWNER" in query or "repositoryOwner(login: $login) {" in query and "projectV2" not in query:
        return {"repositoryOwner": {"__typename": "User", "id": "U1", "login": "laiyinyizao007"}}
    if "projectV2(number: $num)" in query and "items(" in query:
        return {"repositoryOwner": {"projectV2": {
            "id": "P1", "title": "项目组合",
            "fields": {"nodes": [
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["模块"], "name": "模块",
                 "options": [{"id": "O_WORK", "name": "工作"}, {"id": "O_PERS", "name": "个人"}]},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["评分"], "name": "评分"},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["最近推送"], "name": "最近推送"},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["仓库"], "name": "仓库"},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["栈"], "name": "栈"},
                {"__typename": "ProjectV2SingleSelectField", "id": FIELD_IDS["状态"], "name": "状态",
                 "options": [{"id": "O_ACTIVE", "name": "进行中"}, {"id": "O_IDLE", "name": "休眠"}]},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["进度"], "name": "进度"},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["DDL"], "name": "DDL"},
                {"__typename": "ProjectV2Field", "id": FIELD_IDS["负责人"], "name": "负责人"},
            ]},
            "items": {"nodes": [
                {"id": iid, "content": {"id": iid, "title": t, "body": b}}
                for iid, (t, b) in STATE["items"].items()
            ]},
        }}}
    if "addProjectV2DraftIssue" in query:
        iid = f"ITEM{STATE['next_id']}"
        STATE["next_id"] += 1
        STATE["items"][iid] = (variables["title"], variables.get("body") or "")
        return {"addProjectV2DraftIssue": {"projectV2Item": {"id": iid}}}
    if "updateProjectV2ItemFieldValue" in query:
        fid = variables["fid"]
        name = {v: k for k, v in FIELD_IDS.items()}[fid]
        STATE.setdefault("written", []).append((variables["iid"], name, variables["val"]))
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["iid"]}}}
    if "deleteProjectV2Item" in query:
        STATE["items"].pop(variables["iid"], None)
        return {"deleteProjectV2Item": {"deletedItemId": variables["iid"]}}
    if "createProjectV2Field" in query:
        return {"createProjectV2Field": {"projectV2Field": {"id": "NEWFIELD", "name": variables["name"]}}}
    if "createProjectV2View" in query:
        return {"createProjectV2View": {"projectV2View": {"id": "V1", "name": variables["name"]}}}
    raise AssertionError("未预期的 query: " + query[:80])

sp.gql = fake_gql
sp.fetch_repos = lambda: [
    {"name": "green-compass-net", "description": "", "primaryLanguage": "TypeScript",
     "stargazerCount": 1, "pushedAt": "2026-11-06T00:00:00Z", "isArchived": False,
     "isFork": False, "url": "https://github.com/laiyinyizao007/green-compass-net"},
    {"name": "lovable-life-hub", "description": "", "primaryLanguage": "TypeScript",
     "stargazerCount": 2, "pushedAt": "2026-11-06T00:00:00Z", "isArchived": False,
     "isFork": False, "url": "https://github.com/laiyinyizao007/lovable-life-hub"},
]
sp.load_scores = lambda: {"green-compass-net": 20, "lovable-life-hub": 30}

print("══════ 场景 1：首次同步（Project 为空）══════")
STATE["items"] = {}; STATE["written"] = []; STATE["next_id"] = 1
sp.sync(99, "laiyinyizao007", dry_run=False, prune=False)
created = len(STATE["items"])
w1 = list(STATE["written"])
fields_written = {f for _, f, _ in w1}
print(f"\n  条目数={created}（期望 9）")
print(f"  写过的字段={sorted(fields_written)}")
assert created == 9, f"条目数不对: {created}"
assert "进度" not in fields_written, "不应写进度"
assert "DDL" not in fields_written, "不应写 DDL"
assert {"模块", "评分", "最近推送", "仓库", "栈", "状态", "负责人"} <= fields_written
# 状态/负责人 默认值各 9 次
stat_cnt = sum(1 for _, f, _ in w1 if f == "状态")
own_cnt = sum(1 for _, f, _ in w1 if f == "负责人")
assert stat_cnt == 9 and own_cnt == 9, f"默认值次数不对 status={stat_cnt} owner={own_cnt}"
print("  ✓ 首次同步：9 条全建；机器字段+人工默认值写入；进度/DDL 未写")

print("\n══════ 场景 2：再次同步（人工字段必须不被覆盖）══════")
STATE["written"] = []
sp.sync(99, "laiyinyizao007", dry_run=False, prune=False)
w2 = list(STATE["written"])
fields2 = {f for _, f, _ in w2}
print(f"  条目数={len(STATE['items'])}（应仍为 9，不得重复创建）")
print(f"  写过的字段={sorted(fields2)}")
assert len(STATE["items"]) == 9, f"重复创建了：{len(STATE['items'])}"
assert "进度" not in fields2, "进度不应被覆盖"
assert "DDL" not in fields2, "DDL 不应被覆盖"
assert "状态" not in fields2, "状态不应被覆盖"
assert "负责人" not in fields2, "负责人不应被覆盖"
assert {"模块", "评分", "最近推送", "仓库", "栈"} <= fields2, "机器字段应继续写"
print("  ✓ 二次同步：幂等；人工字段零覆盖；机器字段照常刷新")

print("\n══════ 场景 3：--prune 只删自己创建的陈旧条目 ══════")
STATE["items"]["FOREIGN"] = ("我手动加的条目", "跟 GitHub 无关")
STATE["items"]["ITEM1"] = ("🏥 Fidelity Craftsmen", "旧条目\n\n仓库：https://github.com/x/y")
STATE["written"] = []
sp.sync(99, "laiyinyizao007", dry_run=False, prune=True)
titles = [t for t, _ in STATE["items"].values()]
print(f"  剩余条目={len(titles)}，含 FOREIGN={'我手动加的条目' in titles}")
assert "我手动加的条目" in titles, "不该删用户自己加的条目"
print("  ✓ prune：保留手工条目")

print("\n══════ 场景 4：dry-run 不写任何东西 ══════")
before = len(STATE["items"]); STATE["written"] = []
sp.sync(99, "laiyinyizao007", dry_run=True, prune=False)
assert len(STATE["items"]) == before and not STATE["written"], "dry-run 写了东西！"
print("  ✓ dry-run：零写入")

print("\n✅ 全部断言通过")
