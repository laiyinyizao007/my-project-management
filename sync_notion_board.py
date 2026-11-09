#!/usr/bin/env python3
"""把 GitHub 看板镜像成 Notion 看板，并支持在 Notion 里改完自动回写。

为什么有这个东西：
    DSH 工作台里的看板交互确实一般（它是通用面板，不是为项目管理的密集操作设计的）。
    Notion 的数据库视图、筛选、多视图、拖拽排序都更顺手。
    但 GitHub 那边才是唯一真相 —— Issue、sub-issue、Projects 字段都在那儿，
    任务系统（自动周报、进度同步）也都以 GitHub 为准。
    所以：**Notion 当看板用，GitHub 当账本存**，两边自动联动。

联动规则：
    · GitHub → Notion：项目组 / 任务的结构与字段全量同步
    · Notion → GitHub：你在 Notion 里改了「进度 / 推进状态 / 负责人 / DDL」，
      下一次同步会写回 GitHub 的对应字段

怎么判断「你改过」：
    每行有一个「上次同步」时间戳。同步时比较 Notion 的 last_edited_time：
      last_edited_time > 上次同步  → 你改过 → 写回 GitHub
      否则                          → 没改过 → 以 GitHub 为准，覆盖 Notion

用法:
    python3 sync_notion_board.py --setup     # 建页面 + 两个数据库（幂等，可重复跑）
    python3 sync_notion_board.py             # 同步
    python3 sync_notion_board.py --dry-run   # 只看要做什么

令牌: ~/.dsh/project-console/notion-token（600 权限）
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from project_registry import (  # noqa: E402
    build_auto_groups,
    build_groups,
    load_workspace_tags,
)
from setup_project_issues import (  # noqa: E402
    ADD_SUB,
    ADD_TO_PROJECT,
    SET_FIELD,
    gql,
    repo_issues,
    repo_node_id,
)

OWNER = "laiyinyizao007"
PROJECT_NUMBER = 1
API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TOKEN_FILE = Path(os.environ.get("NOTION_TOKEN_FILE", str(Path.home() / ".dsh/project-console/notion-token")))

PARENT_TITLE = "项目总控台"
GROUPS_DB_TITLE = "项目组"
TASKS_DB_TITLE = "任务"

# ── Notion 属性定义 ────────────────────────────────────────────
GROUP_PROPS = {
    "名称": {"title": {}},
    "GitHub Key": {"rich_text": {}},
    "分类": {"select": {"options": [
        {"name": "客户交付", "color": "orange"},
        {"name": "个人产品", "color": "green"},
        {"name": "效率工具", "color": "blue"},
    ]}},
    "推进状态": {"select": {"options": [
        {"name": "进行中", "color": "blue"},
        {"name": "休眠", "color": "gray"},
        {"name": "归档候选", "color": "yellow"},
    ]}},
    "进度": {"number": {"format": "percent"}},
    "负责人": {"rich_text": {}},
    "DDL": {"date": {}},
    "主仓": {"rich_text": {}},
    "组内仓": {"rich_text": {}},
    "评分": {"number": {}},
    "最近推送": {"rich_text": {}},
    "任务数": {"number": {}},
    "进展记录": {"rich_text": {}},
    "链接": {"url": {}},
    "上次同步": {"date": {}},
}

TASK_PROPS = {
    "标题": {"title": {}},
    "GitHub Key": {"rich_text": {}},
    "所属项目组": {"relation": {}},      # 运行时补上 relation id
    "状态": {"select": {"options": [
        {"name": "开放", "color": "blue"},
        {"name": "完成", "color": "green"},
    ]}},
    "负责人": {"rich_text": {}},
    "DDL": {"date": {}},
    "仓库": {"rich_text": {}},
    "链接": {"url": {}},
    "上次同步": {"date": {}},
}


# ── Notion REST 客户端 ─────────────────────────────────────────
class Notion:
    """薄客户端：限速 3 次/秒（Notion 的官方限流），带重试。"""

    def __init__(self, token):
        self.token = token
        self.last = 0.0
        self.calls = 0

    def _throttle(self):
        gap = 0.34                       # ~3 req/s
        wait = gap - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()

    def request(self, method, path, payload=None):
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        for attempt in range(4):
            self._throttle()
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self.calls += 1
                    return json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:300]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Notion {method} {path} → HTTP {e.code}: {body}")
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Notion {method} {path} 网络失败: {e}")
        raise RuntimeError(f"Notion {method} {path} 重试耗尽")

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload):
        return self.request("POST", path, payload)

    def patch(self, path, payload):
        return self.request("PATCH", path, payload)

    def search(self, title):
        """按标题找页面（用来找到父页面）"""
        payload = {"query": title, "filter": {"value": "page", "property": "object"}}
        return self.post("/search", payload).get("results", [])

    def query_db(self, db_id):
        """拉数据库全部行"""
        out = []
        cursor = None
        while True:
            payload = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = self.post(f"/databases/{db_id}/query", payload)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return out


def read_token():
    if not TOKEN_FILE.is_file():
        return ""
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


# ── 属性构造 ───────────────────────────────────────────────────
def title_prop(text):
    return {"title": [{"type": "text", "text": {"content": str(text or "")[:1900]}}]}


def rich(text):
    return {"rich_text": [{"type": "text", "text": {"content": str(text or "")[:1900]}}]}


def select(name):
    return {"select": {"name": name} if name else None}


def number(value):
    return {"number": float(value) if value is not None else None}


def date(value):
    return {"date": {"start": str(value)[:10]} if value else None}


def url(value):
    return {"url": value or None}


def key_of(page, name="GitHub Key"):
    for p in (page.get("properties") or {}).get(name, {}).get("rich_text", []) or []:
        return p.get("plain_text") or p.get("text", {}).get("content", "")
    return ""


def title_of(page):
    for p in (page.get("properties") or {}).get("名称", {}).get("title", []) or []:
        return p.get("plain_text") or p.get("text", {}).get("content", "")
    for p in (page.get("properties") or {}).get("标题", {}).get("title", []) or []:
        return p.get("plain_text") or p.get("text", {}).get("content", "")
    return ""


def prop_date(page, name):
    return ((page.get("properties") or {}).get(name, {}).get("date") or {}).get("start")


def prop_number(page, name):
    return (page.get("properties") or {}).get(name, {}).get("number")


def prop_select(page, name):
    return ((page.get("properties") or {}).get(name, {}).get("select") or {}).get("name")


def prop_text(page, name):
    for p in (page.get("properties") or {}).get(name, {}).get("rich_text", []) or []:
        return p.get("plain_text") or p.get("text", {}).get("content", "")
    return ""


# ── GitHub 看板读取 ────────────────────────────────────────────
BOARD_QUERY = """
query($login:String!,$num:Int!,$after:String){
  repositoryOwner(login:$login){
    ... on User{ projectV2(number:$num){ id items(first:100, after:$after){
      nodes{
        id
        content{ __typename ... on Issue{id title number} }
        fieldValues(first:20){nodes{
          ... on ProjectV2ItemFieldSingleSelectValue{name field{... on ProjectV2FieldCommon{name}}}
          ... on ProjectV2ItemFieldTextValue{text field{... on ProjectV2FieldCommon{name}}}
          ... on ProjectV2ItemFieldNumberValue{number field{... on ProjectV2FieldCommon{name}}}
          ... on ProjectV2ItemFieldDateValue{date field{... on ProjectV2FieldCommon{name}}}
        }}
      }
      pageInfo{hasNextPage endCursor}
    } } }
  }
}
"""


def read_board():
    """看板上的项目组条目：{issue_id: {title, number, values}}"""
    items = {}
    after = None
    while True:
        data = gql(BOARD_QUERY, {"login": OWNER, "num": PROJECT_NUMBER, "after": after})
        proj = data["repositoryOwner"]["projectV2"]
        for node in proj["items"]["nodes"]:
            content = node.get("content") or {}
            if content.get("__typename") != "Issue" or not content.get("id"):
                continue
            values = {}
            for fv in node.get("fieldValues", {}).get("nodes", []):
                nm = (fv.get("field") or {}).get("name")
                if not nm:
                    continue
                values[nm] = fv.get("name") or fv.get("text") or fv.get("number") or fv.get("date") or ""
            items[content["id"]] = {
                "title": content.get("title", ""),
                "number": content.get("number"),
                "issueId": content["id"],
                "values": values,
            }
        if not proj["items"]["pageInfo"]["hasNextPage"]:
            break
        after = proj["items"]["pageInfo"]["endCursor"]
    return items


GROUP_ISSUES_QUERY = """
query($owner:String!,$repo:String!){
  repository(owner:$owner,name:$repo){ issues(first:100, states:[OPEN,CLOSED]){
    nodes{ id number title body labels(first:10){nodes{name}} }
  } }
}
"""


def find_group_issue(repo, key):
    data = gql(GROUP_ISSUES_QUERY, {"owner": OWNER, "repo": repo})
    for n in data["repository"]["issues"]["nodes"]:
        labels = [l["name"] for l in n["labels"]["nodes"]]
        if "type: group" in labels and f"group:{key}" in labels:
            return n
    return None


def collect_groups():
    """全部项目组：GitHub 上的字段 + 任务列表"""
    repos = fetch_repos()
    cap = fetch_issue_capability()
    for r in repos:
        r["hasIssuesEnabled"] = cap.get(r["name"], True)
    explicit = build_groups()
    auto = build_auto_groups(repos, groups=explicit, tags_by_repo=load_workspace_tags())
    groups = explicit + auto
    board = read_board()

    out = []
    for g in groups:
        iss = find_group_issue(g["primary"], g["key"])
        if not iss:
            continue
        item = board.get(iss["id"], {})
        values = item.get("values", {})
        # 组内仓：Issue 正文里写着，比本地注册表更贴近 GitHub 上的实际
        repos_in_group = []
        for line in (iss.get("body") or "").splitlines():
            if line.startswith("- 组内仓："):
                repos_in_group = [x.strip() for x in line.split("：", 1)[1].split(",") if x.strip()]
        tasks = []
        for rname in repos_in_group:
            for t in repo_issues(rname):
                tasks.append({"repo": rname, **t})
        out.append({
            "key": f"group:{g['key']}",
            "title": iss["title"],
            "issueId": iss["id"],
            "repo": g["primary"],
            "issueNumber": iss["number"],
            "repos": repos_in_group or g["repos"],
            "category": values.get("分类") or g["category"],
            "status": values.get("推进状态") or "进行中",
            "progress": values.get("进度"),
            "owner": values.get("负责人") or "",
            "ddl": values.get("DDL") or "",
            "score": values.get("评分"),
            "lastPush": values.get("最近推送") or "",
            "stack": values.get("栈") or "",
            "url": f"https://github.com/{OWNER}/{g['primary']}/issues/{iss['number']}",
            "latestUpdate": latest_progress(g["primary"], iss["number"]),
            "tasks": tasks,
        })
    return out


# ── Notion 结构搭建 ────────────────────────────────────────────
def ensure_parent(notion):
    for page in notion.search(PARENT_TITLE):
        if page.get("object") == "page" and title_of(page) == PARENT_TITLE:
            return page["id"]
    parent = {"type": "page_id", "page_id": None}
    # 顶层父页面：需要一个能被集成访问的页面。优先找「我的项目」工作区的页面。
    created = notion.post("/pages", {
        "parent": {"type": "workspace", "workspace": True},
        "properties": {"title": title_prop(PARENT_TITLE)},
        "children": [{
            "object": "block", "type": "heading_1",
            "heading_1": {"rich_text": [{"type": "text", "text": {"content": PARENT_TITLE}}]}},
        ],
    })
    return created["id"]


def find_database(notion, title):
    for page in notion.search(title):
        if page.get("object") == "database" and title_of(page) == title:
            return page
    return None


def ensure_databases(notion, parent_id):
    """建/找两个数据库，返回 {标题: database_id}"""
    out = {}
    for title, props in ((GROUPS_DB_TITLE, GROUP_PROPS), (TASKS_DB_TITLE, TASK_PROPS)):
        found = find_database(notion, title)
        if found:
            out[title] = found["id"]
            continue
        payload_props = json.loads(json.dumps(props))
        if title == TASKS_DB_TITLE:
            payload_props["所属项目组"] = {"relation": {"database_id": out.get(GROUPS_DB_TITLE)}}
        created = notion.post("/databases", {
            "parent": {"type": "page_id", "page_id": parent_id},
            "title": title_prop(title),
            "properties": payload_props,
        })
        out[title] = created["id"]
    return out


# ── 同步 ──────────────────────────────────────────────────────
def sync_groups(notion, db_id, groups, dry_run=False):
    existing = {key_of(p): p for p in notion.query_db(db_id)}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    created = updated = 0
    relation_targets = {}
    for g in groups:
        props = {
            "名称": title_prop(g["title"]),
            "GitHub Key": rich(g["key"]),
            "分类": select(g["category"]),
            "推进状态": select(g["status"]),
            "进度": number(g["progress"]),
            "负责人": rich(g["owner"]),
            "DDL": date(g["ddl"]),
            "主仓": rich(g["repo"]),
            "组内仓": rich(", ".join(g["repos"])),
            "评分": number(g["score"]),
            "最近推送": rich(g["lastPush"]),
            "任务数": number(len(g["tasks"])),
            "进展记录": rich(g.get("latestUpdate", "")),
            "链接": url(g["url"]),
            "上次同步": date(now),
        }
        page = existing.get(g["key"])
        if page:
            notion.patch(f"/pages/{page['id']}", {"properties": props})
            relation_targets[g["key"]] = page["id"]
            updated += 1
        else:
            if dry_run:
                continue
            new = notion.post("/pages", {
                "parent": {"type": "database_id", "database_id": db_id},
                "properties": props,
            })
            relation_targets[g["key"]] = new["id"]
            created += 1
    return created, updated, relation_targets


def sync_tasks(notion, db_id, groups, relation_targets, dry_run=False):
    existing = {key_of(p): p for p in notion.query_db(db_id)}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    created = updated = 0
    for g in groups:
        for t in g["tasks"]:
            key = f"task:{t['repo']}#{t['number']}"
            props = {
                "标题": title_prop(t["title"]),
                "GitHub Key": rich(key),
                "状态": select("完成" if t.get("state") == "closed" else "开放"),
                "负责人": rich(t.get("assignee") or ""),
                "DDL": date(t.get("due") or ""),
                "仓库": rich(t["repo"]),
                "链接": url(f"https://github.com/{OWNER}/{t['repo']}/issues/{t['number']}"),
                "上次同步": date(now),
            }
            target = relation_targets.get(g["key"])
            if target:
                props["所属项目组"] = {"relation": [{"id": target}]}
            page = existing.get(key)
            if page:
                notion.patch(f"/pages/{page['id']}", {"properties": props})
                updated += 1
            else:
                if dry_run:
                    continue
                notion.post("/pages", {
                    "parent": {"type": "database_id", "database_id": db_id},
                    "properties": props,
                })
                created += 1
    return created, updated


# ── Notion → GitHub 回写 ──────────────────────────────────────
# 你在 Notion 里改了「进度 / 推进状态 / 负责人 / DDL」，
# 下一次同步会写回 GitHub 的同名字段。
#
# 怎么知道是你改的：每行有「上次同步」时间戳。
#   Notion 的 last_edited_time > 上次同步 → 你动过 → 写回 GitHub
#   否则 → 你没动 → 以 GitHub 为准，覆盖 Notion
WRITE_BACK_FIELDS = ("进度", "推进状态", "负责人", "DDL")

PROGRESS_MARK = "<!-- dsh-progress:"


def latest_progress(repo, number):
    """组条目下最新的进展记录（进展存成 issue 评论，标记在正文末尾）"""
    url = f"https://api.github.com/repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100"
    out = subprocess.run(
        ["curl", "-s", "-H", f"Authorization: Bearer {os.environ.get('GH_TOKEN', '')}",
         "-H", "Accept: application/vnd.github+json", url],
        capture_output=True, text=True).stdout
    try:
        comments = json.loads(out)
    except ValueError:
        return ""
    if not isinstance(comments, list):
        return ""
    for c in reversed(comments):
        body = c.get("body") or ""
        if PROGRESS_MARK in body:
            return body.split(PROGRESS_MARK)[0].strip()
    return ""


def write_back(notion, db_id, groups, dry_run=False):
    """把 Notion 上被改过的字段写回 GitHub"""
    from setup_project_issues import fetch_board_items, fetch_fields

    pid = gql('query{user(login:"%s"){projectV2(number:%d){id}}}' % (OWNER, PROJECT_NUMBER))[
        "user"]["projectV2"]["id"]
    fields = fetch_fields(pid)
    items = fetch_board_items(pid)
    pages = {key_of(p): p for p in notion.query_db(db_id)}

    pushed = []
    for g in groups:
        page = pages.get(g["key"])
        if not page:
            continue
        synced_at = prop_date(page, "上次同步")
        edited_at = page.get("last_edited_time")
        if not synced_at or not edited_at:
            continue
        if edited_at <= synced_at:
            continue                      # 你没动过 → 以 GitHub 为准
        item = items.get(g["issueId"])
        if not item:
            continue
        item_id, current = item
        changes = {}
        for fname in WRITE_BACK_FIELDS:
            want = {
                "进度": prop_number(page, "进度"),
                "推进状态": prop_select(page, "推进状态"),
                "负责人": prop_text(page, "负责人"),
                "DDL": prop_date(page, "DDL"),
            }[fname]
            if want in (None, ""):
                continue
            if str(current.get(fname) or "") != str(want):
                changes[fname] = want
        if not changes:
            continue
        if dry_run:
            pushed.append((g["title"], changes))
            continue
        for fname, want in changes.items():
            spec = fields.get(fname)
            if not spec:
                continue
            val = {"text": str(want)}
            if fname == "进度":
                val = {"number": float(want)}
            elif fname == "DDL":
                val = {"date": str(want)[:10]}
            elif spec.get("options"):
                oid = spec["options"].get(str(want))
                if not oid:
                    continue
                val = {"singleSelectOptionId": oid}
            gql(SET_FIELD, {"pid": pid, "iid": item_id, "fid": spec["id"], "val": val})
        pushed.append((g["title"], changes))
    return pushed


def main():
    ap = argparse.ArgumentParser(description="GitHub 看板 → Notion 看板 镜像同步")
    ap.add_argument("--setup", action="store_true", help="建页面和数据库（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只看要做什么，不写 Notion")
    args = ap.parse_args()

    token = read_token()
    if not token:
        print(f"❌ 没有 Notion 令牌：{TOKEN_FILE}", file=sys.stderr)
        print("   放一个 ntn_ 开头的内部集成令牌进去（600 权限）", file=sys.stderr)
        return 2

    notion = Notion(token)
    groups = collect_groups()
    total_tasks = sum(len(g["tasks"]) for g in groups)
    print(f"📦 GitHub 看板：{len(groups)} 个项目组 / {total_tasks} 个任务")

    parent_id = ensure_parent(notion)
    dbs = ensure_databases(notion, parent_id)
    print(f"📓 Notion：项目组库 {dbs[GROUPS_DB_TITLE]} ｜ 任务库 {dbs[TASKS_DB_TITLE]}")

    if args.setup:
        print("✅ Notion 结构已就绪（父页面 + 两个数据库）")
        return 0

    # 先回写：你在 Notion 里的改动要趁 GitHub 的值覆盖 Notion 之前写回去
    pushed = write_back(notion, dbs[GROUPS_DB_TITLE], groups, args.dry_run)
    for title, changes in pushed:
        print(f"   ↩ 回写 {title}: {json.dumps(changes, ensure_ascii=False)}")
    if pushed:
        print(f"✅ 从 Notion 回写 {len(pushed)} 个项目组的字段")

    c1, u1, rel = sync_groups(notion, dbs[GROUPS_DB_TITLE], groups, args.dry_run)
    c2, u2 = sync_tasks(notion, dbs[TASKS_DB_TITLE], groups, rel, args.dry_run)
    print(f"✅ 项目组：新建 {c1} ｜ 更新 {u1}")
    print(f"✅ 任务：新建 {c2} ｜ 更新 {u2}")
    print(f"   （Notion API 调用 {notion.calls} 次）")
    return 0


def fetch_repos():
    """复用 setup_project_issues 的仓库列表"""
    import setup_project_issues as s
    return s.fetch_repos()


def fetch_issue_capability():
    import setup_project_issues as s
    return s.fetch_issue_capability()


if __name__ == "__main__":
    sys.exit(main())
