#!/usr/bin/env python3
"""
每周计划 Issue 自动生成

流程：
1. 计算本周 ISO 周号和日期范围
2. 幂等检查：已存在本周计划 Issue 则退出
3. 读取上周进展（profile.md 的 WEEKLY_PROGRESS 区块）
4. 查询本周 Sprint 中的 open Issue（GraphQL，降级为 repo 全量 open）
5. 调用 Claude Haiku 生成本周优先级建议
6. 构建 Issue 正文并创建 Issue

触发方式：
  - 由 auto-create-sprint.yml 的 sprint-created dispatch 驱动（主路径）
  - schedule/workflow_dispatch 直接触发（兜底）

环境变量：
  GH_TOKEN           PROJECT_TOKEN PAT（repo + project scope）
  ANTHROPIC_API_KEY  Claude API 密钥（可选，无则跳过 AI 部分）
  PROJECT_NUMBER     Project v2 编号
  SPRINT_TITLE       由 dispatch payload 传入的 Sprint 名（可选）
  ROLLED_OVER        续期 Issue 数量（可选）
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
PROFILE_PATH = BASE_DIR / "profile.md"
GITHUB_USER = (
    os.environ.get("GH_OWNER")
    or os.environ.get("GITHUB_REPOSITORY_OWNER")
    or "laiyinyizao007"
)


# ── 1. 计算周信息 ──────────────────────────────────────────────────────────

def get_week_info():
    now = datetime.now(timezone.utc)
    # ISO 8601 周数（处理跨年边界）
    d = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    d_thursday = d + timedelta(days=(3 - d.weekday()))  # 移到本周周四
    year_start = datetime(d_thursday.year, 1, 1, tzinfo=timezone.utc)
    week_num = (d_thursday - year_start).days // 7 + 1
    iso_year = d_thursday.year
    week_str = str(week_num).zfill(2)

    # 本周周一和周日
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)

    return {
        "title": f"[Weekly] {iso_year}-W{week_str}",
        "sprint": f"Sprint {iso_year}-W{week_str}",
        "week_id": f"{iso_year}-W{week_str}",
        "monday": monday.strftime("%Y-%m-%d"),
        "sunday": sunday.strftime("%Y-%m-%d"),
    }


# ── 2. 幂等检查 ────────────────────────────────────────────────────────────

def get_existing_issue_number(title):
    """返回已存在的同名周计划 Issue 编号，不存在返回 None"""
    r = subprocess.run(
        ["gh", "issue", "list", "--label", "type: weekly-plan",
         "--state", "all", "--json", "title,number", "--limit", "10"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        issues = json.loads(r.stdout)
        for i in issues:
            if i["title"] == title:
                return i["number"]
        return None
    except Exception:
        return None


# ── 3. 读上周进展 ──────────────────────────────────────────────────────────

def read_weekly_progress():
    if not PROFILE_PATH.exists():
        return None
    text = PROFILE_PATH.read_text(encoding="utf-8")
    m = re.search(
        r"<!-- WEEKLY_PROGRESS_START -->(.*?)<!-- WEEKLY_PROGRESS_END -->",
        text, re.DOTALL,
    )
    if not m:
        return None
    content = m.group(1).strip()
    # 跳过空内容或只有链接的占位内容
    if not content or content.count("\n") < 2:
        return None
    return content


# ── 4. 查询本周 Sprint 的 open Issues ─────────────────────────────────────

def run_gh(args):
    r = subprocess.run(["gh"] + args, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def get_sprint_issues(sprint_title):
    """GraphQL 查询 Project v2 当前 Sprint 的 open items。

    Returns:
        (issues, project_id, sprint_field_id, iteration_id)
        issues 为列表或 None（查询失败/无结果），其余三项在查询失败时为 None。
    """
    _NONE = (None, None, None, None)
    project_num = os.environ.get("PROJECT_NUMBER", "1")
    try:
        project_num_int = int(project_num)
    except ValueError:
        return _NONE

    # 先获取 Project ID 和 Sprint field ID
    query = """
query($login: String!, $num: Int!) {
  repositoryOwner(login: $login) {
    ... on User {
      projectV2(number: $num) {
        id
        fields(first: 30) {
          nodes {
            __typename
            ... on ProjectV2IterationField {
              id name
              configuration { iterations { id title startDate } }
            }
          }
        }
      }
    }
    ... on Organization {
      projectV2(number: $num) {
        id
        fields(first: 30) {
          nodes {
            __typename
            ... on ProjectV2IterationField {
              id name
              configuration { iterations { id title startDate } }
            }
          }
        }
      }
    }
  }
}"""
    out = run_gh(["api", "graphql", "-f", f"query={query}",
                  "-f", f"login={GITHUB_USER}", "-F", f"num={project_num_int}"])
    if not out:
        return _NONE

    try:
        data = json.loads(out)
        project = data["data"]["repositoryOwner"]["projectV2"]
        project_id = project["id"]
        sprint_field = next(
            (f for f in project["fields"]["nodes"]
             if f.get("__typename") == "ProjectV2IterationField" and f.get("name") == "Sprint"),
            None,
        )
        if not sprint_field:
            return (None, project_id, None, None)

        sprint_field_id = sprint_field["id"]
        current_iter = next(
            (i for i in sprint_field["configuration"]["iterations"]
             if i["title"] == sprint_title),
            None,
        )
        if not current_iter:
            # 取最新的 active iteration 作为兜底
            iters = sprint_field["configuration"]["iterations"]
            current_iter = iters[-1] if iters else None
        if not current_iter:
            return (None, project_id, sprint_field_id, None)
        iteration_id = current_iter["id"]
    except (KeyError, TypeError, StopIteration):
        return _NONE

    # 查 Sprint 中的 open issues
    items_query = """
query($pid: ID!, $cursor: String) {
  node(id: $pid) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          content {
            ... on Issue {
              number title state url labels(first: 5) { nodes { name } }
            }
          }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldIterationValue { iterationId }
            }
          }
        }
      }
    }
  }
}"""

    issues = []
    cursor = None
    while True:
        args = ["api", "graphql", "-f", f"query={items_query}", "-f", f"pid={project_id}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        out = run_gh(args)
        if not out:
            break
        try:
            page_data = json.loads(out)
            page = page_data["data"]["node"]["items"]
            for item in page["nodes"]:
                content = item.get("content", {})
                if not content or content.get("state") != "OPEN":
                    continue
                in_sprint = any(
                    fv.get("__typename") == "ProjectV2ItemFieldIterationValue"
                    and fv.get("iterationId") == iteration_id
                    for fv in item["fieldValues"]["nodes"]
                )
                if not in_sprint:
                    continue
                labels = [l["name"] for l in content.get("labels", {}).get("nodes", [])]
                if "type: weekly-plan" in labels:
                    continue
                issues.append({
                    "number": content["number"],
                    "title": content["title"],
                    "url": content["url"],
                    "labels": labels,
                })
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        except (KeyError, TypeError) as e:
            print(f"[warn] 分页解析失败: {e}", file=sys.stderr)
            break

    return (issues if issues else None), project_id, sprint_field_id, iteration_id


def add_issue_to_project(issue_number, project_id, sprint_field_id, iteration_id):
    """将 Issue 加入 Project v2 并设置 Sprint 字段。幂等：重复添加无副作用。"""
    if not project_id:
        return

    # 获取 Issue node_id
    r = subprocess.run(
        ["gh", "api", f"repos/{GITHUB_USER}/{os.environ.get('GITHUB_REPOSITORY', '').split('/')[-1] or 'my-project-management'}/issues/{issue_number}",
         "--jq", ".node_id"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        print(f"   ⚠️  获取 Issue #{issue_number} node_id 失败", file=sys.stderr)
        return
    node_id = r.stdout.strip()

    # addProjectV2ItemById → 获取 item_id
    add_mutation = """
mutation($pid: ID!, $cid: ID!) {
  addProjectV2ItemById(input: {projectId: $pid, contentId: $cid}) {
    item { id }
  }
}"""
    out = run_gh(["api", "graphql",
                  "-f", f"query={add_mutation}",
                  "-f", f"pid={project_id}",
                  "-f", f"cid={node_id}"])
    if not out:
        print(f"   ⚠️  addProjectV2ItemById 失败（Issue #{issue_number}）", file=sys.stderr)
        return
    try:
        item_id = json.loads(out)["data"]["addProjectV2ItemById"]["item"]["id"]
    except (KeyError, TypeError):
        print(f"   ⚠️  解析 item_id 失败", file=sys.stderr)
        return

    print(f"   ✅ Issue #{issue_number} 已加入 Project")

    # 设置 Sprint 字段
    if not sprint_field_id or not iteration_id:
        return
    update_mutation = """
mutation($pid: ID!, $iid: ID!, $fid: ID!, $itid: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $pid
    itemId: $iid
    fieldId: $fid
    value: { iterationId: $itid }
  }) { projectV2Item { id } }
}"""
    out2 = run_gh(["api", "graphql",
                   "-f", f"query={update_mutation}",
                   "-f", f"pid={project_id}",
                   "-f", f"iid={item_id}",
                   "-f", f"fid={sprint_field_id}",
                   "-f", f"itid={iteration_id}"])
    if out2:
        print(f"   ✅ Issue #{issue_number} Sprint 字段已设置")
    else:
        print(f"   ⚠️  Sprint 字段设置失败（Issue #{issue_number}）", file=sys.stderr)


def get_open_issues_fallback():
    """降级：获取 repo 全量 open issues（最多 20 条）"""
    out = run_gh([
        "issue", "list", "--state", "open",
        "--json", "number,title,url,labels", "--limit", "20",
    ])
    if not out:
        return []
    try:
        raw = json.loads(out)
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "url": i["url"],
                "labels": [l["name"] for l in i.get("labels", [])],
            }
            for i in raw
        ]
    except Exception:
        return []


# ── 5. Claude API 调用（带限额重试）─────────────────────────────────────────

def _claude_call(client, *, max_wait_seconds=300, **kwargs):
    """调用 Claude API，遇到限额时按 retry-after 等待重试；超过 max_wait_seconds 则抛异常。"""
    import time
    import anthropic as _anthropic
    for attempt in range(4):
        try:
            return client.messages.create(**kwargs)
        except _anthropic.RateLimitError as e:
            headers = getattr(getattr(e, "response", None), "headers", {}) or {}
            retry_after = int(headers.get("retry-after", 60))
            if retry_after > max_wait_seconds:
                print(f"❌ API 限额，retry-after={retry_after}s 超过上限 {max_wait_seconds}s，放弃", file=sys.stderr)
                raise
            print(f"⏳ API 限额（第 {attempt+1} 次），{retry_after}s 后重试...", file=sys.stderr)
            time.sleep(retry_after)
        except _anthropic.APIStatusError as e:
            if e.status_code == 529:  # overloaded
                wait = 30 * (attempt + 1)
                print(f"⏳ API 过载（第 {attempt+1} 次），{wait}s 后重试...", file=sys.stderr)
                import time as _time; _time.sleep(wait)
            else:
                raise
    raise RuntimeError("Claude API 限额，重试次数耗尽，workflow 应失败")


def _make_client():
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    kwargs = {"api_key": api_key}
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


# ── 6. Claude Haiku 生成计划 ───────────────────────────────────────────────

def generate_ai_plan(week_info, weekly_progress, sprint_issues):
    issue_list = ""
    if sprint_issues:
        for i in sprint_issues[:15]:
            labels_str = f" `{'` `'.join(i['labels'])}`" if i["labels"] else ""
            issue_list += f"- #{i['number']} {i['title']}{labels_str}\n"
    else:
        issue_list = "（无续期任务）\n"

    progress_section = weekly_progress or "（上周无进展记录）"

    prompt = f"""你是一个个人效率助手。根据以下信息，为本周（{week_info['week_id']}）生成工作计划。

## 上周进展
{progress_section}

## 本周 Sprint 续期任务
{issue_list}

请严格按以下格式输出，不要添加任何其他文字：

=== SUGGESTIONS ===
（本周重点：3 条目标，动词开头，每条不超过 40 字）
（建议执行顺序：从续期任务选 3-5 项，格式 `- [ ] #编号 任务名`）

=== GOALS ===
- 目标1
- 目标2
- 目标3

=== TASKS ===
- [ ] #编号 任务名
- [ ] #编号 任务名

要求：
- 中文输出，简洁直接
- GOALS 中每条不超过 30 字，动词开头
- TASKS 从续期任务中选优先级最高的 3-5 项
- 重点参考高优先级标签（P0/P1）和上周未完成方向
"""

    client = _make_client()
    if not client:
        return None
    resp = _claude_call(client, model="claude-haiku-4-5-20251001", max_tokens=600,
                        messages=[{"role": "user", "content": prompt}])
    raw = resp.content[0].text.strip()
    return _parse_ai_sections(raw)


def _parse_ai_sections(raw):
    """将 AI 输出按 === SECTION === 标记解析为 dict"""
    sections = {"suggestions": "", "goals": "", "tasks": ""}
    current = None
    buf = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped in ("=== SUGGESTIONS ===", "=== GOALS ===", "=== TASKS ==="):
            if current and buf:
                sections[current] = "\n".join(buf).strip()
            current = stripped.strip("= ").lower()
            buf = []
        else:
            if current:
                buf.append(line)
    if current and buf:
        sections[current] = "\n".join(buf).strip()
    # 兜底：如果解析失败，把整段放进 suggestions
    if not any(sections.values()):
        sections["suggestions"] = raw
    return sections


# ── 6. 构建 Issue 正文 ─────────────────────────────────────────────────────

def build_issue_body(week_info, ai_plan, weekly_progress, sprint_issues):
    monday, sunday = week_info["monday"], week_info["sunday"]
    lines = [f"## {monday} ~ {sunday}", ""]

    # AI 建议部分
    suggestions = (ai_plan or {}).get("suggestions", "") if ai_plan else ""
    if suggestions:
        lines += ["### 🤖 AI 本周建议", "", suggestions, "", "---", ""]
    else:
        lines += [
            "### 🤖 AI 本周建议",
            "",
            "> ℹ️  AI 建议生成失败（未配置 ANTHROPIC_API_KEY 或 API 调用失败）。",
            "",
            "---",
            "",
        ]

    # 上周进展部分
    if weekly_progress:
        lines += ["### 📊 上周进展", "", weekly_progress, "", "---", ""]

    # 续期任务部分
    if sprint_issues:
        lines += [f"### 🔄 续期任务（{len(sprint_issues)} 项）", ""]
        for i in sprint_issues:
            labels_str = f" `{'` `'.join(i['labels'])}`" if i["labels"] else ""
            lines.append(f"- [ ] #{i['number']} [{i['title']}]({i['url']}){labels_str}")
        lines += ["", "---", ""]
    elif os.environ.get("ROLLED_OVER", "0") != "0":
        rolled = os.environ.get("ROLLED_OVER", "0")
        lines += [
            f"### 🔄 续期任务（{rolled} 项）",
            "",
            "> 任务已续期到本周 Sprint，请前往 [Project 看板](https://github.com/users/"
            f"{GITHUB_USER}/projects/{os.environ.get('PROJECT_NUMBER', '1')}) 查看。",
            "",
            "---",
            "",
        ]

    # 本周目标 + 计划任务（AI 自动填写，无 AI 则留空）
    goals = (ai_plan or {}).get("goals", "") if ai_plan else ""
    tasks = (ai_plan or {}).get("tasks", "") if ai_plan else ""
    lines += [
        "### 本周目标",
        "",
        goals if goals else "（请填写本周目标）",
        "",
        "### 计划任务",
        "",
        tasks if tasks else "- [ ] ",
        "",
        "### 每日回顾",
        "",
        f"#### 周一 {monday}",
        "- 完成：",
        "- 阻塞：",
        "",
        "#### 周二",
        "- 完成：",
        "- 阻塞：",
        "",
        "#### 周三",
        "- 完成：",
        "- 阻塞：",
        "",
        "#### 周四",
        "- 完成：",
        "- 阻塞：",
        "",
        "#### 周五",
        "- 完成：",
        "- 阻塞：",
        "",
        "### 周回顾（周日填写）",
        "",
        "- 完成率：",
        "- 下周重点：",
        "",
    ]
    return "\n".join(lines)


# ── 7. 创建 Issue ──────────────────────────────────────────────────────────

def create_issue(title, body):
    """创建 Issue 并返回 Issue 编号（int）"""
    body_file = Path("/tmp/weekly_plan_body.md")
    body_file.write_text(body, encoding="utf-8")
    r = subprocess.run(
        ["gh", "issue", "create",
         "--title", title,
         "--body-file", str(body_file),
         "--label", "type: weekly-plan,status: todo"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        url = r.stdout.strip()
        print(f"✅ 已创建周计划 Issue：{title}")
        print(f"   {url}")
        try:
            return int(url.split("/")[-1])
        except (ValueError, IndexError):
            return None
    else:
        print(f"❌ Issue 创建失败：{r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)


# ── 8. 补填已有 Issue 的 AI 建议区块 ─────────────────────────────────────

def fill_ai_for_existing_issue(issue_number, week_info, weekly_progress, sprint_issues):
    """为已存在但 AI 建议缺失的 Issue 生成并更新 AI 建议、本周目标、计划任务区块"""
    ai_plan = generate_ai_plan(week_info, weekly_progress, sprint_issues)
    if not ai_plan:
        print("ℹ️  AI 计划生成失败，跳过更新", file=sys.stderr)
        return

    suggestions = ai_plan.get("suggestions", "")
    goals = ai_plan.get("goals", "")
    tasks = ai_plan.get("tasks", "")

    # 获取当前 body
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "my-project-management"
    r = subprocess.run(
        ["gh", "api", f"repos/{GITHUB_USER}/{repo}/issues/{issue_number}", "--jq", ".body"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"❌ 获取 Issue #{issue_number} 失败", file=sys.stderr)
        return
    body = r.stdout.strip()

    # 替换 AI 建议区块
    if suggestions:
        body = re.sub(
            r"### 🤖 AI 本周建议\n\n.*?(?=\n---)",
            f"### 🤖 AI 本周建议\n\n{suggestions}",
            body,
            flags=re.DOTALL,
        )
    # 替换本周目标区块
    if goals:
        body = re.sub(
            r"(### 本周目标\n\n).*?(\n\n### )",
            rf"\g<1>{goals}\g<2>",
            body,
            flags=re.DOTALL,
        )
    # 替换计划任务区块
    if tasks:
        body = re.sub(
            r"(### 计划任务\n\n).*?(\n\n### )",
            rf"\g<1>{tasks}\g<2>",
            body,
            flags=re.DOTALL,
        )
    # 替换续期任务区块（反映当前 Sprint open issues）
    if sprint_issues:
        task_lines = ""
        for i in sprint_issues:
            labels_str = f" `{'` `'.join(i['labels'])}`" if i["labels"] else ""
            task_lines += f"- [ ] #{i['number']} [{i['title']}]({i['url']}){labels_str}\n"
        rolled_block = f"### 🔄 续期任务（{len(sprint_issues)} 项）\n\n{task_lines.rstrip()}"
    else:
        rolled_block = "### 🔄 续期任务（0 项）\n\n> 本周 Sprint 无续期任务。"
    body = re.sub(
        r"### 🔄 续期任务（\d+ 项）\n\n.*?(?=\n---)",
        rolled_block,
        body,
        flags=re.DOTALL,
    )

    import json as _json, tempfile as _tmp, os as _os
    with _tmp.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        _json.dump({"body": body}, f, ensure_ascii=False)
        tmp = f.name
    r2 = subprocess.run(
        ["gh", "api", "--method", "PATCH", f"repos/{GITHUB_USER}/{repo}/issues/{issue_number}",
         "--input", tmp, "--jq", ".number,.updated_at"],
        capture_output=True, text=True,
    )
    _os.unlink(tmp)
    if r2.returncode == 0:
        print(f"✅ Issue #{issue_number} AI 建议、本周目标、计划任务已更新")
    else:
        print(f"❌ 更新失败: {r2.stderr[:200]}", file=sys.stderr)


# ── 9. 每日回顾 / 周回顾 自动更新 ─────────────────────────────────────────

WEEKDAY_ZH = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _beijing_now():
    return datetime.now(timezone.utc) + timedelta(hours=8)


def _load_tracked_repos():
    config_path = BASE_DIR / "tracked_config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("tracked_repos", {})
    except Exception:
        return {}


def get_today_commits_by_repo():
    """获取今日（北京时间）各追踪仓库的 commits"""
    bn = _beijing_now()
    today_start_utc = bn.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    since_iso = today_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    results = {}
    for repo, info in _load_tracked_repos().items():
        r = subprocess.run(
            ["gh", "api",
             f"/repos/{GITHUB_USER}/{repo}/commits?since={since_iso}&per_page=20",
             "--jq", '[.[].commit.message | split("\n")[0]]'],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            try:
                commits = [c for c in json.loads(r.stdout) if c.strip()]
                if commits:
                    results[repo] = {"info": info, "commits": commits}
            except Exception:
                pass
    return results


def generate_daily_ai_review(today_commits_by_repo):
    """用 Claude 生成当日完成摘要和阻塞"""
    client = _make_client()
    if not client:
        return None, None

    if today_commits_by_repo:
        commit_lines = ""
        for repo, data in today_commits_by_repo.items():
            name = data["info"].get("name", repo)
            commits = data["commits"][:5]
            commit_lines += f"- {name}: {'; '.join(commits)}\n"
    else:
        commit_lines = "（今日无 commit 记录）"

    prompt = f"""根据以下今日 commits，生成每日回顾。严格按格式输出，不要其他文字：

{commit_lines}

完成：[今日完成的主要工作，30字以内，无活动则写"无"]
阻塞：[遇到的阻塞，无则写"无"]"""

    resp = _claude_call(client, model="claude-haiku-4-5-20251001", max_tokens=100,
                        messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text.strip()
    completed, blocked = "", ""
    for line in text.splitlines():
        if line.startswith("完成："):
            completed = line[3:].strip()
        elif line.startswith("阻塞："):
            blocked = line[3:].strip()
    return completed or "（详见 commits）", blocked or "无"


def generate_weekly_ai_review(week_info):
    """用 Claude 生成周回顾（周六早上触发）"""
    bn = _beijing_now()
    monday_bn = bn - timedelta(days=bn.weekday())
    since_iso = (monday_bn.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")

    summary_lines = []
    for repo, info in _load_tracked_repos().items():
        r = subprocess.run(
            ["gh", "api",
             f"/repos/{GITHUB_USER}/{repo}/commits?since={since_iso}&per_page=50",
             "--jq", "length"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            try:
                count = int(r.stdout.strip())
                if count > 0:
                    summary_lines.append(f"- {info.get('name', repo)}: {count} commits")
            except Exception:
                pass

    week_summary = "\n".join(summary_lines) if summary_lines else "（本周无 commit 记录）"

    prompt = f"""根据以下本周 commits 摘要，生成周回顾。严格按格式输出，不要其他文字：

{week_summary}

完成率：[估算本周任务完成率，如 80%]
主要成果：[本周最重要成果，一句话40字以内]
下周重点：[建议下周重点方向，一句话40字以内]"""

    client = _make_client()
    if not client:
        return None
    resp = _claude_call(client, model="claude-haiku-4-5-20251001", max_tokens=150,
                        messages=[{"role": "user", "content": prompt}])
    return resp.content[0].text.strip()


def _patch_issue_body(issue_number, new_body):
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "my-project-management"
    import json as _j, tempfile as _t, os as _o
    with _t.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        _j.dump({"body": new_body}, f, ensure_ascii=False)
        tmp = f.name
    r = subprocess.run(
        ["gh", "api", "--method", "PATCH", f"repos/{GITHUB_USER}/{repo}/issues/{issue_number}",
         "--input", tmp, "--jq", ".number"],
        capture_output=True, text=True,
    )
    _o.unlink(tmp)
    return r.returncode == 0


def patch_daily_review(issue_number, day_label, completed, blocked):
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "my-project-management"
    r = subprocess.run(
        ["gh", "api", f"repos/{GITHUB_USER}/{repo}/issues/{issue_number}", "--jq", ".body"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"❌ 获取 Issue #{issue_number} 失败", file=sys.stderr)
        return
    body = r.stdout.strip()
    pattern = rf"(#### {re.escape(day_label)}(?:\s+\d{{4}}-\d{{2}}-\d{{2}})?\n)(- 完成：[^\n]*\n- 阻塞：[^\n]*)"
    new_body = re.sub(pattern, rf"\g<1>- 完成：{completed}\n- 阻塞：{blocked}", body)
    if new_body == body:
        print(f"⚠️  未找到 {day_label} 回顾区块", file=sys.stderr)
        return
    if _patch_issue_body(issue_number, new_body):
        print(f"✅ Issue #{issue_number} {day_label} 每日回顾已更新")
    else:
        print(f"❌ 更新失败", file=sys.stderr)


def patch_weekly_review(issue_number, review_text):
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] or "my-project-management"
    r = subprocess.run(
        ["gh", "api", f"repos/{GITHUB_USER}/{repo}/issues/{issue_number}", "--jq", ".body"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"❌ 获取 Issue #{issue_number} 失败", file=sys.stderr)
        return
    body = r.stdout.strip()
    new_body = re.sub(
        r"(### 周回顾（周日填写）\n\n).*",
        rf"\g<1>{review_text}",
        body,
        flags=re.DOTALL,
    )
    if _patch_issue_body(issue_number, new_body):
        print(f"✅ Issue #{issue_number} 周回顾已更新")
    else:
        print(f"❌ 更新失败", file=sys.stderr)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    fill_ai = "--fill-ai" in sys.argv
    daily_review = "--daily-review" in sys.argv
    weekly_review = "--weekly-review" in sys.argv

    week_info = get_week_info()
    print(f"📅 生成周计划：{week_info['week_id']}  ({week_info['monday']} ~ {week_info['sunday']})")

    # ── 每日回顾模式 ──────────────────────────────────────────────
    if daily_review:
        bn = _beijing_now()
        day_idx = bn.weekday()  # 0=Monday … 6=Sunday
        day_label = WEEKDAY_ZH[day_idx]
        print(f"📝 --daily-review：生成 {day_label} 每日回顾（北京时间 {bn.strftime('%Y-%m-%d')}）")
        issue_number = get_existing_issue_number(week_info["title"])
        if not issue_number:
            print("⚠️  本周计划 Issue 不存在，跳过", file=sys.stderr)
            return
        today_commits = get_today_commits_by_repo()
        total_commits = sum(len(d["commits"]) for d in today_commits.values())
        print(f"   今日 commits：{total_commits} 条（{len(today_commits)} 个仓库有活动）")
        completed, blocked = generate_daily_ai_review(today_commits)
        if completed is None:
            completed = "（无 AI 摘要）"
            blocked = "无"
        print(f"   完成：{completed}")
        print(f"   阻塞：{blocked}")
        patch_daily_review(issue_number, day_label, completed, blocked)
        return

    # ── 周回顾模式 ────────────────────────────────────────────────
    if weekly_review:
        print("📊 --weekly-review：生成周回顾")
        issue_number = get_existing_issue_number(week_info["title"])
        if not issue_number:
            print("⚠️  本周计划 Issue 不存在，跳过", file=sys.stderr)
            return
        review_text = generate_weekly_ai_review(week_info)
        if review_text is None:
            review_text = "（无 AI 摘要）"
        print(f"   周回顾内容：{review_text[:80]}...")
        patch_weekly_review(issue_number, review_text)
        return

    # 查本周 Sprint issues（同时获取 project 上下文，供后续 add_issue_to_project 使用）
    sprint_title = os.environ.get("SPRINT_TITLE") or week_info["sprint"]
    print(f"🔍 查询 Sprint：{sprint_title}")
    sprint_issues, project_id, sprint_field_id, iteration_id = get_sprint_issues(sprint_title)
    if sprint_issues is None and project_id is None:
        print("⚠️  GraphQL 查询失败，降级为 repo 全量 open issues")
        sprint_issues = get_open_issues_fallback() or []
    elif sprint_issues is None:
        sprint_issues = get_open_issues_fallback() or []
    # 周计划 Issue 本身不应出现在续期任务列表中
    sprint_issues = [i for i in sprint_issues if "type: weekly-plan" not in i.get("labels", [])]
    print(f"   找到 {len(sprint_issues)} 个续期/open Issue")

    # 幂等检查：若 Issue 已存在
    existing_number = get_existing_issue_number(week_info["title"])
    if existing_number:
        print(f"⏭️  本周计划 Issue 已存在：{week_info['title']} (#{existing_number})")
        if project_id:
            print("   尝试确保 Issue 已加入 Project board...")
            add_issue_to_project(existing_number, project_id, sprint_field_id, iteration_id)
        if fill_ai:
            print("   --fill-ai：补填 AI 建议区块...")
            weekly_progress = read_weekly_progress()
            fill_ai_for_existing_issue(existing_number, week_info, weekly_progress, sprint_issues)
        return

    # 读上周进展
    weekly_progress = read_weekly_progress()
    if weekly_progress:
        print("✅ 读取到上周进展")
    else:
        print("ℹ️  profile.md 无上周进展记录，跳过该部分")

    # AI 生成计划
    ai_plan = generate_ai_plan(week_info, weekly_progress, sprint_issues)
    if ai_plan:
        print("✅ AI 计划生成完成")
    else:
        print("ℹ️  跳过 AI 计划（无 ANTHROPIC_API_KEY 或调用失败）")

    # 构建并创建 Issue，然后加入 Project board
    body = build_issue_body(week_info, ai_plan, weekly_progress, sprint_issues)
    issue_number = create_issue(week_info["title"], body)
    if issue_number and project_id:
        print(f"📌 将 Issue #{issue_number} 加入 Project board...")
        add_issue_to_project(issue_number, project_id, sprint_field_id, iteration_id)


if __name__ == "__main__":
    main()
