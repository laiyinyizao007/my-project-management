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
import re
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
# 数据库 id 的本地缓存 —— 每次都去 Notion 搜一遍要花掉一分多钟，
# 而库 id 是不会变的（除非手动删库重建）。
LEDGER_FILE = Path(os.environ.get(
    "REPO_LEDGER_FILE", str(Path(__file__).resolve().parent / "repo-ledger.json")))
DB_CACHE_FILE = Path(os.environ.get(
    "NOTION_DB_CACHE_FILE", str(Path.home() / ".dsh/project-console/notion-dbs.json")))

PARENT_TITLE = "项目总控台"
# 三层结构：项目 → 仓库 → issue，每一层都是关系连着的。
# 「域」不是一层，只是项目/仓库上的一个 select 字段（为谁做的）。
GROUPS_DB_TITLE = "项目"
REPOS_DB_TITLE = "仓库"
TASKS_DB_TITLE = "issue"
# 曾经按分类各建一个库（客户交付/个人产品/效率工具）当「筛好的看板」。
# 那是个绕法错误：Notion API 建不出带筛选的保存视图，于是用复制数据来变出筛选效果 ——
# 结果 95 行存成 190 行，而且你在视图库改了、主库那份不跟着变。
# 现在只留「项目组」一个库，分类看板用 Notion 原生的**链接数据库视图**做：
# 视图和数据是同一份，改一处两处都变，不存在显示不一致。
VIEW_DB_TITLES = []

# ── Notion 属性定义 ────────────────────────────────────────────
# 「分类」是行上的属性（多选），回答「这是什么性质」。
# 值从工作区索引已有的标签推导，不另造词 —— 标签是人判过的，分类只是换个说法。
TAG_TO_CATEGORY = {
    "产品": ("web-app", "ai-agent", "game"),
    "工具": ("tooling", "family:self-tools"),
    "自动化": ("automation", "personal-admin", "agent-config"),
    "基础设施": ("infra-devops", "hardware-iot", "crypto-v vpn", "security", "messaging-bot"),
    "素材": ("dataset", "data-pipeline", "media-content"),
    "知识库": ("knowledge-base", "learning-reference"),
}
TAG_TO_CATEGORY["基础设施"] = ("infra-devops", "hardware-iot", "crypto-vpn", "security", "messaging-bot")
TAG2CATEGORY = {t: c for c, ts in TAG_TO_CATEGORY.items() for t in ts}


def categories_of(tags):
    """标签集合 → 分类集合（多选：一个仓可以既是产品又是知识库）。"""
    return sorted({TAG2CATEGORY[t] for t in (tags or ()) if t in TAG2CATEGORY})


def multi_select(names):
    return {"multi_select": [{"name": n} for n in names]}


GROUP_PROPS = {
    "名称": {"title": {}},
    "GitHub Key": {"rich_text": {}},
    # 域：select 字段（为谁做的）—— 不单独建库
    "域": {"select": {"options": [
        {"name": "客户交付", "color": "orange"},
        {"name": "个人产品", "color": "green"},
        {"name": "效率工具", "color": "blue"},
    ]}},
    # 分类：多选属性（是什么性质），值从标签推导
    "分类": {"multi_select": {"options": [{"name": n} for n in TAG_TO_CATEGORY]}},
    "推进状态": {"select": {"options": [
        {"name": "进行中", "color": "blue"},
        {"name": "休眠", "color": "gray"},
        {"name": "归档候选", "color": "yellow"},
    ]}},
    "进度": {"number": {"format": "percent"}},
    "负责人": {"rich_text": {}},
    "DDL": {"date": {}},
    "主仓": {"rich_text": {}},
    "评分": {"number": {}},
    "最近推送": {"rich_text": {}},
    "进展记录": {"rich_text": {}},
    "链接": {"url": {}},
    "上次同步": {"date": {}},
}

# 「域」的取值：为谁做的。三个，跟看板的分类字段一致。
DOMAIN_OPTIONS = [
    {"name": "客户交付", "color": "orange"},
    {"name": "个人产品", "color": "green"},
    {"name": "效率工具", "color": "blue"},
]

# 仓库库 = 台账。178 个仓（活跃 + 归档），每个都有归属。
REPO_PROPS = {
    "名称": {"title": {}},
    "GitHub Key": {"rich_text": {}},
    "域": {"select": {"options": [
        {"name": "客户交付", "color": "orange"},
        {"name": "个人产品", "color": "green"},
        {"name": "效率工具", "color": "blue"},
    ]}},
    "分类": {"multi_select": {"options": [{"name": n} for n in TAG_TO_CATEGORY]}},
    "项目": {"relation": {}},
    "语言": {"rich_text": {}},
    "最后推送": {"rich_text": {}},
    "已归档": {"checkbox": {}},
    "Issues 关闭": {"checkbox": {}},
    "说明": {"rich_text": {}},
    "链接": {"url": {}},
    "上次同步": {"date": {}},
}

TASK_PROPS = {
    "标题": {"title": {}},
    "GitHub Key": {"rich_text": {}},
    # issue 精确属于它所在的那个仓 —— 这条关系是准的
    "所属仓库": {"relation": {}},
    "所属项目": {"relation": {}},
    "状态": {"select": {"options": [
        {"name": "开放", "color": "blue"},
        {"name": "完成", "color": "green"},
    ]}},
    "负责人": {"rich_text": {}},
    "DDL": {"date": {}},
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

    def search(self, title, kind="page"):
        """按标题找页面/数据库。

        filter 的 value 只能是 page 或 data_source —— 不带 filter 会把
        两种都搜出来，但带上 filter 才能精确找到数据库（否则每次都新建一套）。
        """
        payload = {"query": title}
        if kind == "page":
            payload["filter"] = {"value": "page", "property": "object"}
        elif kind == "database":
            payload["filter"] = {"value": "data_source", "property": "object"}
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
    """令牌：优先环境变量（CI 里用），其次本地文件。"""
    env = os.environ.get("NOTION_TOKEN", "").strip()
    if env:
        return env
    if not TOKEN_FILE.is_file():
        return ""
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


# ── 属性构造 ───────────────────────────────────────────────────
def title_prop(text):
    return {"title": [{"type": "text", "text": {"content": str(text or "")[:1900]}}]}


def db_title(text):
    """数据库标题：/databases 要的是数组，不是页面的属性对象。"""
    return [{"type": "text", "text": {"content": str(text or "")[:1900]}}]


def rich(text):
    return {"rich_text": [{"type": "text", "text": {"content": str(text or "")[:1900]}}]}


def select(name):
    return {"select": {"name": name} if name else None}


def number(value):
    """空串 / 非数字 → 空值。GitHub 的字段读出来可能是 ''。"""
    if value in (None, ""):
        return {"number": None}
    try:
        return {"number": float(value)}
    except (TypeError, ValueError):
        return {"number": None}


def date(value):
    """Notion 日期属性。DDL 这类只到天，截成 2026-11-09。"""
    return {"date": {"start": str(value)[:10]} if value else None}


def stamp(value):
    """Notion 日期属性，但**保留时分秒**。

    「上次同步」必须带完整时间戳：Notion 的 last_edited_time 是
    2026-11-09T07:32:00.000Z 这样的完整时间，只存到天的话
    字符串比较永远是「你改过」，每次检查都白跑一遍。
    """
    return {"date": {"start": str(value)} if value else None}


def url(value):
    return {"url": value or None}


def as_dt(value):
    """把 Notion 的时间字符串变成可比较的时间对象。

    Notion 的 last_edited_time 结尾是 Z（UTC），我们自己写的「上次同步」
    结尾是 +00:00 —— 直接比字符串的话，同一秒内会误判成「你改过」。
    """
    from datetime import datetime
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def issue_number_of(page):
    """从「链接」里取 issue 编号（.../issues/123）。"""
    link = ((page.get("properties") or {}).get("链接", {}).get("url")) or ""
    tail = link.rstrip("/").rsplit("/", 1)[-1]
    return tail if tail.isdigit() else ""


def find_issue_node(repo, number):
    """按仓和编号找 issue 的 node id（写回时要拿它对上看板条目）。"""
    q = """
    query($owner:String!,$repo:String!,$number:Int!){
      repository(owner:$owner,name:$repo){ issue(number:$number){ id number title } }
    }
    """
    try:
        data = gql(q, {"owner": OWNER, "repo": repo, "number": int(number)})
        return (data.get("repository") or {}).get("issue")
    except RuntimeError:
        return None


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def key_of(page, name="GitHub Key"):
    for p in (page.get("properties") or {}).get(name, {}).get("rich_text", []) or []:
        return p.get("plain_text") or p.get("text", {}).get("content", "")
    return ""


def title_of(page):
    """页面的标题。

    Notion 页面的默认标题属性名是 **title**（数据库里可以叫「名称」「标题」…），
    认错了就永远找不到页面 —— 这个坑踩过一次。
    """
    props = page.get("properties") or {}
    for key in ("title", "名称", "标题"):
        for p in props.get(key, {}).get("title", []) or []:
            return p.get("plain_text") or p.get("text", {}).get("content", "")
    # 数据库的标题在 title 数组里，不在 properties 里
    for p in page.get("title", []) or []:
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


def parallel_map(items, limit, worker):
    """并发跑一批任务，最多 limit 个同时在飞；结果顺序保持不变。"""
    import threading
    results = [None] * len(items)
    cursor = [0]
    lock = threading.Lock()

    errors = []

    def run():
        while True:
            with lock:
                i = cursor[0]
                cursor[0] += 1
            if i >= len(items):
                return
            try:
                results[i] = worker(items[i])
            except Exception as e:      # noqa: BLE001 —— 失败必须冒出去
                errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(min(max(1, limit), len(items)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        # 一条失败就整轮失败：宁可报错，也不能拿半截数据去覆盖 Notion
        raise errors[0]
    return [r for r in results if r is not None]


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

    # 每个组要查一次 GraphQL 找组条目 + 拉组内各仓的任务：
    # 95 个组串行要两分钟。并发上限 8。
    def one(g):
        iss = find_group_issue(g["primary"], g["key"])
        if not iss:
            return None
        # 组内仓：Issue 正文里写着，比本地注册表更贴近 GitHub 上的实际
        repos_in_group = []
        for line in (iss.get("body") or "").splitlines():
            if line.startswith("- 组内仓："):
                repos_in_group = [x.strip() for x in line.split("：", 1)[1].split(",") if x.strip()]
        tasks = []
        for rname in repos_in_group:
            for t in repo_issues(rname):
                tasks.append({"repo": rname, **t})
        return g, iss, repos_in_group, tasks

    out = []
    for g, iss, repos_in_group, tasks in parallel_map(groups, 8, one):
        item = board.get(iss["id"], {})
        values = item.get("values", {})
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
    """找父页面。

    注意：不能用 API 在 workspace 底下建页面 —— 那样页面归集成所有，
    只出现在集成的私有页里，你的侧边栏根本看不到。
    所以父页面必须由**你在自己的 Notion 里建**，再把集成加为共享成员。
    """
    for page in notion.search(PARENT_TITLE):
        if page.get("object") == "page" and title_of(page) == PARENT_TITLE:
            return page["id"]
    raise RuntimeError(
        f"没找到名为「{PARENT_TITLE}」的页面。\n"
        f"  1. 在 Notion 里新建一个空页面，标题就叫「{PARENT_TITLE}」\n"
        f"  2. 页面右上角 ··· → 连接（Connections）→ 选择你刚建的集成 → 确认\n"
        f"  3. 再跑一次本脚本"
    )


def find_database(notion, title):
    """找数据库。

    搜索结果里**混着已归档的库**（归档过的照样能被搜到），
    选错一个就 404 —— 所以先把归档的剔掉。
    """
    for db in notion.search(title, kind="database"):
        if db.get("archived") or db.get("in_trash"):
            continue
        if title_of(db) == title:
            return db
    return None


LEGACY_RENAMES = {"项目组": "项目", "任务": "issue", "分类": "域"}


def ensure_databases(notion, parent_id, use_cache=True):
    """建/找四个库（分类 → 项目 → 仓库 → issue），返回 {标题: database_id}。

    带缓存时：缓存里有的直接用，没有的才去 Notion 搜 / 建。
    """
    cached = {}
    if use_cache and DB_CACHE_FILE.is_file():
        try:
            cached = json.loads(DB_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
    out = {k: v for k, v in cached.items() if v}

    # 旧的库名先改名（改名的数据还在，重建就白丢 95+74 行了）
    for old_name, new_name in LEGACY_RENAMES.items():
        if new_name in out:
            continue
        found = find_database(notion, old_name)
        if found:
            notion.patch(f"/databases/{found['id']}", {"title": db_title(new_name)})
            out[new_name] = found["id"]
            print(f"   ↻ 库「{old_name}」改名成「{new_name}」")

    plan = [
        (GROUPS_DB_TITLE, GROUP_PROPS),
        (REPOS_DB_TITLE, REPO_PROPS),
        (TASKS_DB_TITLE, TASK_PROPS),
    ]
    missing = [(title, props) for title, props in plan if title not in out]
    for title, props in missing:
        found = find_database(notion, title)
        if found:
            out[title] = found["id"]
        else:
            # 关系属性建库时还不能填（目标库还没建出来），先剥掉，之后再连
            plain = {k: v for k, v in json.loads(json.dumps(props)).items()
                     if not (isinstance(v, dict) and "relation" in v)}
            created = notion.post("/databases", {
                "parent": {"type": "page_id", "page_id": parent_id},
                "title": db_title(title),
                "properties": plain,
            })
            out[title] = created["id"]

    # 关系属性要指向具体的库，建完之后再补
    _apply_relations(notion, out)

    try:
        DB_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        DB_CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        DB_CACHE_FILE.chmod(0o600)
    except OSError:
        pass
    return out


def _patch_props(notion, db_id, props, ignore_missing=True):
    """改库的属性。删一个已经删掉的属性是 404 —— 那是「已经没了」，不是错误。"""
    try:
        notion.patch(f"/databases/{db_id}", {"properties": props})
        return True
    except RuntimeError as e:
        if ignore_missing and "object_not_found" in str(e):
            return False
        raise


def _relation(db_id):
    """关系的两种写法：新 API 要 data_source_id，老的收 database_id。都试一遍。"""
    return {"database_id": db_id, "single_property": {}}


def db_properties(notion, db_id):
    """库的属性（带类型），用来判断某个关系该叫什么。"""
    req = urllib.request.Request(f"https://api.notion.com/v1/databases/{db_id}",
        headers={"Authorization": f"Bearer {read_token()}", "Notion-Version": "2022-06-28"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode()).get("properties") or {}


def ensure_select(notion, db_id, name, options):
    """保证 db_id 上有一个名为 name 的 select 属性。

    和关系一样不能靠改名（改名接口报 200 但没变），所以：
    已经是 select 就原样保留；是关系就删掉重建 —— 属性 id 会变，
    但同步每轮都会重写值，视图那边刷新即可。
    """
    props = db_properties(notion, db_id)
    entry = props.get(name)
    if entry and entry.get("type") == "select":
        return False
    if entry:
        _patch_props(notion, db_id, {name: None})
    _patch_props(notion, db_id, {name: {"select": {"options": options}}})
    return True


def ensure_relation(notion, db_id, name, target_db_id):
    """保证 db_id 上有一个名为 name、指向 target_db_id 的关系。

    **不能靠改名**：Notion 的改名接口报 200 但实际没变，
    于是每次同步都新建一个关系、名字被占位成「域 1」「域 2」…
    （真踩过：项目库一度挂着 5 个指向域库的关系）。
    所以用「删掉重建」——关系数据反正下一轮同步会重写。
    """
    props = db_properties(notion, db_id)
    same_target = [k for k, v in props.items()
                   if v.get("type") == "relation"
                   and (v.get("relation") or {}).get("database_id") == target_db_id]
    if same_target == [name]:
        return False
    for k in same_target:
        _patch_props(notion, db_id, {k: None})
    _patch_props(notion, db_id, {name: {"relation": {"database_id": target_db_id, "single_property": {}}}})
    return True


def _apply_relations(notion, dbs):
    """把关系属性接到对应的库上。

    步骤有讲究：
      1. 先删掉同名旧属性（项目库的「分类」原来是 select）
      2. 再按新定义建关系（同名可以复用）
      3. 顺手删掉被关系取代的文本列（项目库的「组内仓」→ 关系「仓库」的逆向）
    """
    grp = dbs.get(GROUPS_DB_TITLE)
    rep = dbs.get(REPOS_DB_TITLE)
    iss = dbs.get(TASKS_DB_TITLE)
    if not (grp and rep and iss):
        return

    # 项目库：「域」是 select，「分类」是多选
    ensure_select(notion, grp, "域", DOMAIN_OPTIONS)
    _patch_props(notion, grp, {"组内仓": None})
    _patch_props(notion, grp, {"分类": {"multi_select": {"options": [{"name": n} for n in TAG_TO_CATEGORY]}}})

    # 仓库库：「域」是 select，「项目」是关系
    ensure_select(notion, rep, "域", DOMAIN_OPTIONS)
    ensure_relation(notion, rep, "项目", grp)
    _patch_props(notion, rep, {"分类": {"multi_select": {"options": [{"name": n} for n in TAG_TO_CATEGORY]}}})

    # issue 库：「所属仓库」+「所属项目」
    ensure_relation(notion, iss, "所属仓库", rep)
    ensure_relation(notion, iss, "所属项目", grp)

    # 反向关系：rollup 是在**本库**上滚的，所以本库得有一条指向对家的关系
    #   项目.任务数 ← 项目库需要指向 issue 库的关系「任务」
    #   仓库.任务数 ← 仓库库需要指向 issue 库的关系「issue」
    ensure_relation(notion, grp, "任务", iss)
    ensure_relation(notion, rep, "issue", iss)


# ── 同步 ──────────────────────────────────────────────────────
CATEGORIES = [
    ("客户交付", "域：给别人做的项目 —— 客户合同下的交付"),
    ("个人产品", "域：自己做出来给别人用的东西"),
    ("效率工具", "域：给自己用的工具与自动化"),
]


def group_props(g, now):
    """项目组在 Notion 里的一行。"""
    return {
        "名称": title_prop(g["title"]),
        "GitHub Key": rich(g["key"]),
        "推进状态": select(g["status"]),
        "进度": number(g["progress"]),
        "负责人": rich(g["owner"]),
        "DDL": date(g["ddl"]),
        "主仓": rich(g["repo"]),
        "评分": number(g["score"]),
        "最近推送": rich(g["lastPush"]),
        "进展记录": rich(g.get("latestUpdate", "")),
        "链接": url(g["url"]),
        "上次同步": stamp(now),
    }


def sync_groups(notion, db_id, groups, dry_run=False):
    """把项目写进库；库里已经不存在的行（GitHub 上删了/改名了）自动归档。"""
    existing = {key_of(p): p for p in notion.query_db(db_id)}
    now = now_iso()
    created = updated = archived = 0
    relation_targets = {}
    wanted = {}
    for g in groups:
        wanted[g["key"]] = g
        props = group_props(g, now)
        props["域"] = select(g["category"])
        props["分类"] = multi_select(g.get("categories") or [])
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
    # 不该在这个库里的行 → 归档（挪进回收站，不真删）
    for key, page in existing.items():
        if key in wanted:
            continue
        if dry_run:
            continue
        notion.patch(f"/pages/{page['id']}", {"archived": True})
        archived += 1
    return created, updated, relation_targets, archived


def sync_repos(notion, db_id, repos, group_ids, dry_run=False):
    """仓库层 = 台账。178 个仓，每个都有分类，能挂上项目的挂上项目。"""
    existing = {title_of(p): p for p in notion.query_db(db_id)}
    now = now_iso()
    created = updated = archived = 0
    ids = {}
    wanted = set()
    for r in repos:
        name = r["name"]
        wanted.add(name)
        props = {
            "名称": title_prop(name),
            "GitHub Key": rich(name),
            "语言": rich(r.get("language") or ""),
            "最后推送": rich(r.get("lastCommit") or r.get("pushedAt") or ""),
            "已归档": {"checkbox": bool(r.get("archived"))},
            "Issues 关闭": {"checkbox": bool(r.get("issuesDisabled"))},
            "说明": rich(r.get("notes") or ""),
            "链接": url(r.get("url") or f"https://github.com/{OWNER}/{name}"),
            "上次同步": stamp(now),
        }
        props["域"] = select(r.get("category"))
        props["分类"] = multi_select(r.get("categories") or [])
        grp_id = group_ids.get(name)
        if grp_id:
            props["项目"] = {"relation": [{"id": grp_id}]}
        page = existing.get(name)
        if page:
            notion.patch(f"/pages/{page['id']}", {"properties": props})
            ids[name] = page["id"]
            updated += 1
        else:
            if dry_run:
                continue
            new = notion.post("/pages", {
                "parent": {"type": "database_id", "database_id": db_id},
                "properties": props,
            })
            ids[name] = new["id"]
            created += 1
    for name, page in existing.items():
        if name in wanted:
            continue
        if dry_run:
            continue
        notion.patch(f"/pages/{page['id']}", {"archived": True})
        archived += 1
    return created, updated, ids, archived


def sync_reverse(notion, dbs, groups, repos, group_ids, repo_ids, issue_ids, dry_run=False):
    """把反向关系也填上。

    Notion API 建出来的关系是**两条独立的属性**，不像 UI 里加关系那样自动互填 ——
    我写了「项目.域」，「域.项目」那边还是空的。
    而 rollup 是在本库上滚的（域.项目数 = 域库里的「项目」关系），
    所以反向这几条必须一起写，否则 rollup 数出来全是 0。
    """
    now = now_iso()
    by_project = {}
    for g in groups:
        ids = [issue_ids[f"task:{t['repo']}#{t['number']}"] for t in g["tasks"]
               if f"task:{t['repo']}#{t['number']}" in issue_ids]
        by_project[g["key"]] = ids
    by_repo = {}
    for g in groups:
        for t in g["tasks"]:
            key = f"task:{t['repo']}#{t['number']}"
            if key in issue_ids:
                by_repo.setdefault(t["repo"], []).append(issue_ids[key])

    # Notion 一个关系属性最多 100 项。超了就**整条不填** ——
    # 填一半会让 rollup 显示 100，静默错比空着更难发现。
    over = []

    # 项目.任务
    for g in groups:
        gid = group_ids.get(g["key"])
        ids = by_project.get(g["key"], [])
        if not gid or dry_run:
            continue
        if len(ids) > 100:
            over.append((f"项目「{g['title']}」的任务", len(ids)))
            continue
        notion.patch(f"/pages/{gid}", {"properties": {
            "任务": {"relation": [{"id": i} for i in ids]},
        }})
    # 仓库.issue
    for r in repos:
        rid = repo_ids.get(r["name"])
        ids = by_repo.get(r["name"], [])
        if not rid or dry_run:
            continue
        if len(ids) > 100:
            over.append((f"仓库「{r['name']}」的 issue", len(ids)))
            continue
        notion.patch(f"/pages/{rid}", {"properties": {
            "issue": {"relation": [{"id": i} for i in ids]},
        }})
    for what, n in over:
        print(f"   ⚠️ {what} 有 {n} 项，超过 Notion 关系上限 100 —— 整条没填，"
              f"改用两跳 rollup 数（页面里的指引）")
    return True


def sync_tasks(notion, db_id, groups, relation_targets, repo_ids, dry_run=False):
    """issue 层：精确连到它所在的那个仓，同时连到所属项目。返回 {key: page_id}。"""
    existing = {key_of(p): p for p in notion.query_db(db_id)}
    now = now_iso()
    created = updated = 0
    issue_ids = {}
    for g in groups:
        for t in g["tasks"]:
            key = f"task:{t['repo']}#{t['number']}"
            props = {
                "标题": title_prop(t["title"]),
                "GitHub Key": rich(key),
                "状态": select("完成" if t.get("state") == "closed" else "开放"),
                "负责人": rich(t.get("assignee") or ""),
                "DDL": date(t.get("due") or ""),
                "链接": url(f"https://github.com/{OWNER}/{t['repo']}/issues/{t['number']}"),
                "上次同步": stamp(now),
            }
            repo_id = repo_ids.get(t["repo"])
            if repo_id:
                props["所属仓库"] = {"relation": [{"id": repo_id}]}
            target = relation_targets.get(g["key"])
            if target:
                props["所属项目"] = {"relation": [{"id": target}]}
            page = existing.get(key)
            if page:
                notion.patch(f"/pages/{page['id']}", {"properties": props})
                issue_ids[key] = page["id"]
                updated += 1
            else:
                if dry_run:
                    continue
                new = notion.post("/pages", {
                    "parent": {"type": "database_id", "database_id": db_id},
                    "properties": props,
                })
                issue_ids[key] = new["id"]
                created += 1
    return created, updated, issue_ids


# ── Notion → GitHub 回写 ──────────────────────────────────────
# 你在 Notion 里改了「进度 / 推进状态 / 负责人 / DDL」，
# 下一次同步会写回 GitHub 的同名字段。
#
# 怎么知道是你改的：每行有「上次同步」时间戳。
#   Notion 的 last_edited_time > 上次同步 → 你动过 → 写回 GitHub
#   否则 → 你没动 → 以 GitHub 为准，覆盖 Notion
WRITE_BACK_FIELDS = ("进度", "推进状态", "负责人", "DDL")
# 进展记录（项目的 rich_text）→ GitHub 上的一条评论。
# 末尾带 dsh-progress 标记，DSH 插件那边读得回来，id 也能对上。
PROGRESS_MARK = "<!-- dsh-progress:{id} kind={kind} -->"

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


def set_issue_state(repo, number, closed):
    """把 GitHub issue 关掉或重新打开。归档仓只读，跳过。"""
    if repo not in writable_repos():
        return {"skipped": "archived"}
    url = f"https://api.github.com/repos/{OWNER}/{repo}/issues/{number}"
    return gh_rest("PATCH", url, {"state": "closed" if closed else "open"})


def write_back_issue_state(notion, db_id, dry_run=False):
    """Notion 上把 issue 的状态改成「完成」→ GitHub 上关掉它；改回「开放」→ 重新打开。"""
    pages = {key_of(p): p for p in notion.query_db(db_id)}
    touched = []
    for key, page in pages.items():
        synced_at = prop_date(page, "上次同步")
        edited_at = page.get("last_edited_time")
        edited_dt, synced_dt = as_dt(edited_at), as_dt(synced_at)
        if not edited_dt or not synced_dt or edited_dt <= synced_dt:
            continue
        touched.append((key, page))
    changed = []
    for key, page in touched:
        state = prop_select(page, "状态")
        if state not in ("开放", "完成"):
            continue
        m = re.match(r"task:(.+)#(\d+)$", key)
        if not m:
            continue
        repo, number = m.group(1), int(m.group(2))
        if dry_run:
            changed.append((key, state))
            continue
        try:
            result = set_issue_state(repo, number, state == "完成")
        except RuntimeError as e:
            print(f"   ⚠️ issue 状态写回失败 {key}: {str(e)[:120]}")
            continue
        if result.get("skipped"):
            print(f"   ⚠️ {key} 所在仓已归档，改不了状态（只读）")
            continue
        changed.append((key, state))
        # 状态处理完了，把标记时间推进，免得下次又当成新改动
        notion.patch(f"/pages/{page['id']}", {"properties": {"上次同步": stamp(now_iso())}})
    return changed


_WRITABLE = None


def writable_repos():
    """还能改的仓（归档仓是只读的：issue 状态改不了、评论也加不了）。

    一次 gh 调用拿到全部仓的归档状态，缓存给本次运行用。
    """
    global _WRITABLE
    if _WRITABLE is None:
        try:
            import setup_project_issues as s
            _WRITABLE = {r["name"] for r in s.fetch_repos() if not r.get("isArchived")}
        except Exception:
            _WRITABLE = set()
    return _WRITABLE


def gh_rest(method, url, payload=None):
    """GitHub REST 调用。出错时把响应体带出来 —— 光看 403 猜不出是哪回事。"""
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers={
            "Authorization": f"Bearer {os.environ.get('GH_TOKEN', '')}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub {method} {url} → HTTP {e.code}: {e.read().decode()[:200]}")


def post_progress_comment(repo, number, text, kind="progress"):
    """把 Notion 上写的进展记录发成 GitHub issue 的一条评论。

    末尾带 dsh-progress 标记 —— 插件那边读评论时按标记还原 id 与 kind，
    标记本身从正文剥掉，所以界面上看到的就是你写的那句话。
    """
    import hashlib
    marker_id = "notion-" + hashlib.sha1(f"{repo}#{number}:{text}".encode()).hexdigest()[:12]
    if repo not in writable_repos():
        return {"skipped": "archived"}
    body = f"{text}\n\n{PROGRESS_MARK.format(id=marker_id, kind=kind)}"
    url = f"https://api.github.com/repos/{OWNER}/{repo}/issues/{number}/comments"
    return gh_rest("POST", url, {"body": body})


def latest_progress_comment(repo, number):
    """GitHub 上这个组条目下最新的进展记录（进展存成评论）。"""
    url = f"https://api.github.com/repos/{OWNER}/{repo}/issues/{number}/comments?per_page=100"
    comments = gh_rest("GET", url)
    if not isinstance(comments, list):
        return ""
    for c in reversed(comments):
        body = c.get("body") or ""
        if "<!-- dsh-progress:" in body:
            return body.split("<!-- dsh-progress:")[0].strip()
    return ""


def write_back(notion, db_ids, groups=None, dry_run=False):
    """把 Notion 上被改过的字段写回 GitHub。

    db_ids 是**所有**组库（主库 + 分类视图库）——
    你在哪个库里改的都算数，谁先改的算谁的。

    只处理「被改过」的行：没改过的行一次 API 都不碰。
    所以这个检查很便宜，可以每 15 分钟跑一次。
    """
    from setup_project_issues import fetch_board_items, fetch_fields

    pid = gql('query{user(login:"%s"){projectV2(number:%d){id}}}' % (OWNER, PROJECT_NUMBER))[
        "user"]["projectV2"]["id"]
    fields = fetch_fields(pid)
    # 同一个项目在多个库里各有一行（主库 + 分类视图库）。
    # 去重时**要留 last_edited_time 最新的那份** ——
    # 按库顺序「先到先得」的话，主库里没动过的那份会把视图库里改过的那份挡住。
    pages = {}
    for db_id in db_ids:
        for page in notion.query_db(db_id):
            key = key_of(page)
            if not key:
                continue
            old = pages.get(key)
            if old is None or (page.get("last_edited_time") or "") > (old.get("last_edited_time") or ""):
                pages[key] = page

    # 先筛出被改过的行 —— 大多数时候 0 到 2 行
    touched = []
    for key, page in pages.items():
        synced_at = prop_date(page, "上次同步")
        edited_at = page.get("last_edited_time")
        if not synced_at or not edited_at:
            continue
        edited_dt = as_dt(edited_at)
        synced_dt = as_dt(synced_at)
        if edited_dt is None or synced_dt is None:
            continue
        if edited_dt <= synced_dt:
            continue                      # 你没动过 → 以 GitHub 为准
        touched.append((key, page))
    if not touched:
        return []

    items = fetch_board_items(pid)
    pushed = []
    for key, page in touched:
        repo = prop_text(page, "主仓")
        number = issue_number_of(page)
        if not repo or not number:
            continue
        issue = find_issue_node(repo, number)
        if not issue:
            continue
        item = items.get(issue["id"])
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
        # 进展记录：写一条评论（不是字段）
        note = prop_text(page, "进展记录").strip()
        if note:
            # 只对**被改过**的行去查 GitHub 上的最新评论 —— 一次调用，
            # 这样 --check 不用拉全量 GitHub（那要两分半）
            if note != latest_progress_comment(repo, number):
                changes["进展记录"] = note
        if not changes:
            continue
        title = title_of(page) or key
        if dry_run:
            pushed.append((title, changes))
            continue
        for fname, want in changes.items():
            if fname == "进展记录":
                try:
                    post_progress_comment(repo, number, want)
                except RuntimeError as e:
                    print(f"   ⚠️ 进展记录写回失败 {repo}#{number}: {str(e)[:120]}")
                continue
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
        pushed.append((title, changes))
        # 记一笔「这次改动处理过了」：否则下一次跑又把它当新改动，
        # 而且全量刷新会把 Notion 里的值覆盖回 GitHub（其实已经是同一个值，无害但吵）
        if not dry_run:
            notion.patch(f"/pages/{page['id']}", {"properties": {"上次同步": stamp(now_iso())}})
    return pushed


def main():
    ap = argparse.ArgumentParser(description="GitHub 看板 → Notion 看板 镜像同步")
    ap.add_argument("--setup", action="store_true", help="建页面和数据库（幂等）")
    ap.add_argument("--dry-run", action="store_true", help="只看要做什么，不写 Notion")
    ap.add_argument("--check", action="store_true",
                    help="只查 Notion 有没有改动（有就回写 GitHub），不做全量刷新 —— 便宜，可以跑得勤")
    args = ap.parse_args()

    token = read_token()
    if not token:
        print(f"❌ 没有 Notion 令牌：{TOKEN_FILE}", file=sys.stderr)
        print("   放一个 ntn_ 开头的内部集成令牌进去（600 权限）", file=sys.stderr)
        return 2

    notion = Notion(token)

    parent_id = ensure_parent(notion)
    dbs = ensure_databases(notion, parent_id)
    print(f"📓 Notion：项目 {dbs.get(GROUPS_DB_TITLE, '')[:8]} → 仓库 {dbs.get(REPOS_DB_TITLE, '')[:8]} → issue {dbs.get(TASKS_DB_TITLE, '')[:8]}")

    if args.setup:
        print("✅ Notion 结构已就绪（分类 → 项目 → 仓库 → issue 四个库）")
        return 0

    if args.check:
        # 只回写：读 Notion（几个查询）→ 比对「上次同步」→ 有改动就写回 GitHub。
        # 不拉 GitHub 全量，也不刷新 Notion —— 所以很便宜，可以每 15 分钟跑一次。
        group_db_ids = [dbs[GROUPS_DB_TITLE]]
        pushed = write_back(notion, group_db_ids, None, args.dry_run)
        for title, changes in pushed:
            print(f"   ↩ 回写 {title}: {json.dumps(changes, ensure_ascii=False)}")
        # issue 的完成状态 → 关掉 / 打开 GitHub issue
        state_changes = write_back_issue_state(notion, dbs[TASKS_DB_TITLE], args.dry_run)
        for key, state in state_changes:
            print(f"   ↩ issue {key} → {state}")
        print(f"✅ 检查完成：{len(pushed)} 处字段 + {len(state_changes)} 处状态已写回 GitHub"
              f"（Notion API {notion.calls} 次）")
        return 0

    groups = collect_groups()
    total_tasks = sum(len(g["tasks"]) for g in groups)
    print(f"📦 GitHub 看板：{len(groups)} 个项目组 / {total_tasks} 个任务")

    # 先回写：你在 Notion 里的改动要趁 GitHub 的值覆盖 Notion 之前写回去
    group_db_ids = [dbs[GROUPS_DB_TITLE]]
    pushed = write_back(notion, group_db_ids, None, args.dry_run)
    for title, changes in pushed:
        print(f"   ↩ 回写 {title}: {json.dumps(changes, ensure_ascii=False)}")
    if pushed:
        print(f"✅ 从 Notion 回写 {len(pushed)} 个项目组的字段")

    # ── 四层：分类 → 项目 → 仓库 → issue ──────────────────
    # 台账（repo-ledger.json）就是仓库层的数据源
    ledger = json.loads(LEDGER_FILE.read_text(encoding="utf-8")) if LEDGER_FILE.is_file() else {"repos": []}
    repos = ledger.get("repos", [])

    # 项目的分类 = 组内各仓的标签并集 → 分类
    repo_tags = {r["name"]: r.get("tags") or [] for r in repos}
    for g in groups:
        merged = set()
        for rname in (g.get("repos") or []):
            merged.update(repo_tags.get(rname) or [])
        g["categories"] = categories_of(merged)

    # 仓 → 项目 的对应（一个仓属于哪个项目组）
    repo_to_group = {}
    for g in groups:
        for rname in (g.get("repos") or []):
            repo_to_group.setdefault(rname, g["key"])
    # 每个仓有几个 issue
    task_count = {}
    for g in groups:
        for t in g["tasks"]:
            task_count[t["repo"]] = task_count.get(t["repo"], 0) + 1
    for r in repos:
        r["tasks"] = task_count.get(r["name"], 0)
        r["categories"] = categories_of(r.get("tags") or [])

    c1, u1, rel, a1 = sync_groups(notion, dbs[GROUPS_DB_TITLE], groups, args.dry_run)
    print(f"✅ 项目：新建 {c1} ｜ 更新 {u1}" + (f" ｜ 归档 {a1}" if a1 else ""))

    # 仓 → 项目：GitHub Key（group:<key>）→ 项目页 id
    group_ids = {}
    for g in groups:
        gid = rel.get(g["key"])
        if gid:
            group_ids[g["key"]] = gid
    repo_group = {name: group_ids.get(key) for name, key in repo_to_group.items()}
    repo_group = {k: v for k, v in repo_group.items() if v}

    rc, ru, repo_ids, ra = sync_repos(notion, dbs[REPOS_DB_TITLE], repos, repo_group, args.dry_run)
    print(f"✅ 仓库：新建 {rc} ｜ 更新 {ru}" + (f" ｜ 归档 {ra}" if ra else ""))

    c2, u2, issue_ids = sync_tasks(notion, dbs[TASKS_DB_TITLE], groups, rel, repo_ids, args.dry_run)
    print(f"✅ issue：新建 {c2} ｜ 更新 {u2}")

    # issue 的完成状态 → 关掉 GitHub issue
    state_changes = write_back_issue_state(notion, dbs[TASKS_DB_TITLE], args.dry_run)
    for key, state in state_changes:
        print(f"   ↩ issue {key} → {state}")

    # 反向关系（Notion API 建的关系不会自动互填，rollup 靠它们）
    sync_reverse(notion, dbs, groups, repos, group_ids, repo_ids, issue_ids, args.dry_run)
    print("✅ 反向关系已填（域→项目/仓库、项目→任务、仓库→issue）")

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
