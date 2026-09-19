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
  GH_TOKEN                PROJECT_TOKEN PAT（repo + project scope）
  ANTHROPIC_API_KEY       Claude API 密钥（可选，无则跳过 AI 部分，向后兼容）
  ANTHROPIC_BASE_URL      Claude API 自定义端点（可选，向后兼容）
  LLM_PRIMARY_API_KEY     主 LLM key（优先于 ANTHROPIC_API_KEY）
  LLM_PRIMARY_BASE_URL    主 LLM 端点（优先于 ANTHROPIC_BASE_URL）
  LLM_PRIMARY_MODEL       主 LLM 模型名（默认 claude-haiku-4-5-20251001）
  LLM_FALLBACK_API_KEY    备用 LLM key（未设 = 无 fallback）
  LLM_FALLBACK_BASE_URL   备用 LLM 端点（可选）
  LLM_FALLBACK_MODEL      备用 LLM 模型名（默认 MiniMax-M3）
  PROJECT_NUMBER          Project v2 编号
  SPRINT_TITLE            由 dispatch payload 传入的 Sprint 名（可选）
  ROLLED_OVER             续期 Issue 数量（可选）
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 本地运行：自动加载仓库根目录的 .env（若存在）；CI 中 dotenv 无副作用
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

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
    if r.returncode != 0:
        if r.stderr.strip():
            print(f"[gh error] {' '.join(args[:3])}: {r.stderr.strip()[:200]}", file=sys.stderr)
        return None
    return r.stdout.strip()


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


# ── 5. Claude API 调用（带限额重试 + fallback）────────────────────────────────

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


def _make_client_with_env(api_key, base_url):
    """从给定 (api_key, base_url) 构造 anthropic.Anthropic 客户端。任一为 None 时返回 None。"""
    if not api_key:
        return None
    import anthropic
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _make_client():
    """主 client 工厂：LLM_PRIMARY_* 优先，向后兼容 ANTHROPIC_*。"""
    api_key = (
        os.environ.get("LLM_PRIMARY_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    )
    base_url = (
        os.environ.get("LLM_PRIMARY_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
    )
    return _make_client_with_env(api_key, base_url)


def _log_primary_failure_hint(primary_exc):
    """主 LLM 失败时打印排查建议（基于错误信息文本启发式匹配）。"""
    msg = str(primary_exc)
    hints = []
    if "Relay service error" in msg or "No available" in msg:
        hints.append(
            "💡 主 LLM 中转无可用账号（{model}）。检查 ANTHROPIC_BASE_URL 中转账号余额，或"
            " 切换到 LLM_PRIMARY_* 系列环境变量指向其他 LLM provider。".format(
                model=os.environ.get("LLM_PRIMARY_MODEL") or "claude-haiku-4-5-20251001"
            )
        )
    if "401" in msg or "Unauthorized" in msg or "Authentication" in msg:
        hints.append("💡 主 LLM 鉴权失败：检查 ANTHROPIC_API_KEY / LLM_PRIMARY_API_KEY 是否过期")
    if "429" in msg or "rate" in msg.lower():
        hints.append("💡 主 LLM 限流：_claude_call 已自动重试 4 次仍失败，已降级到 fallback")
    for h in hints:
        print(h, file=sys.stderr)


def _call_with_fallback(*, messages, max_tokens, system=None):
    """包装层：主 client（_claude_call 4 次重试）失败 → fallback client（同 4 次重试）。

    主 client 缺失或未配置 fallback 时，行为 = 现状（仅主 client，不报错）。
    """
    primary = _make_client()
    if not primary:
        return None

    primary_model = os.environ.get("LLM_PRIMARY_MODEL") or "claude-haiku-4-5-20251001"
    primary_kwargs = {"model": primary_model, "max_tokens": max_tokens, "messages": messages}
    if system is not None:
        primary_kwargs["system"] = system

    primary_exc = None  # 显式初始化：PEP 3134 except 块结束时会清除 `as var` 绑定
    try:
        return _claude_call(primary, **primary_kwargs)
    except Exception as e:
        primary_exc = e
        print(f"⚠️  主 LLM 失败：{type(e).__name__}: {e}", file=sys.stderr)
        _log_primary_failure_hint(e)

    fallback_key = os.environ.get("LLM_FALLBACK_API_KEY")
    if not fallback_key:
        raise RuntimeError(
            f"主 LLM 调用失败且未配置 LLM_FALLBACK_API_KEY，无法 fallback：{primary_exc}"
        ) from primary_exc

    fallback = _make_client_with_env(
        fallback_key,
        os.environ.get("LLM_FALLBACK_BASE_URL"),
    )
    if not fallback:
        raise RuntimeError("LLM_FALLBACK_API_KEY 已设但 client 创建失败") from primary_exc

    fallback_model = os.environ.get("LLM_FALLBACK_MODEL") or "MiniMax-M3"
    fallback_kwargs = {"model": fallback_model, "max_tokens": max_tokens, "messages": messages}
    if system is not None:
        fallback_kwargs["system"] = system

    print(f"🔄 切换到 fallback LLM（model={fallback_model}）", file=sys.stderr)
    return _claude_call(fallback, **fallback_kwargs)


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

    resp = _call_with_fallback(messages=[{"role": "user", "content": prompt}], max_tokens=600)
    if resp is None:
        return None
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
        "完成：",
        "- （待填写）",
        "阻塞：无",
        "",
        "#### 周二",
        "完成：",
        "- （待填写）",
        "阻塞：无",
        "",
        "#### 周三",
        "完成：",
        "- （待填写）",
        "阻塞：无",
        "",
        "#### 周四",
        "完成：",
        "- （待填写）",
        "阻塞：无",
        "",
        "#### 周五",
        "完成：",
        "- （待填写）",
        "阻塞：无",
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
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', encoding='utf-8', delete=False) as f:
        f.write(body)
        body_file = f.name
    try:
        r = subprocess.run(
            ["gh", "issue", "create",
             "--title", title,
             "--body-file", body_file,
             "--label", "type: weekly-plan,status: todo"],
            capture_output=True, text=True,
        )
    finally:
        os.unlink(body_file)
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


def _load_tracked_config_raw():
    config_path = BASE_DIR / "tracked_config.json"
    if not config_path.exists():
        return {"version": "1.0", "settings": {}, "tracked_repos": {}, "ignored_repos": []}
    try:
        with open(config_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": "1.0", "settings": {}, "tracked_repos": {}, "ignored_repos": []}


def _get_today_push_repos(target_bn=None):
    """从 GitHub Events API 找出今日有推送但未在 tracked_config 里的仓库"""
    bn = target_bn or _beijing_now()
    today_start_utc = bn.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    since_str = today_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    r = subprocess.run(
        ["gh", "api", f"/users/{GITHUB_USER}/events?per_page=100",
         "--jq", '[.[] | select(.type=="PushEvent") | {repo: .repo.name, ts: .created_at}]'],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return {}
    try:
        events = json.loads(r.stdout)
    except Exception:
        return {}
    tracked = set(_load_tracked_repos().keys())
    extra = {}
    today_events_count = 0
    for ev in events:
        if ev.get("ts", "") < since_str:
            continue
        today_events_count += 1
        full_name = ev.get("repo", "")
        short = full_name.split("/")[-1] if "/" in full_name else full_name
        if short and short not in tracked and short not in extra:
            extra[short] = {"name": short, "icon": "📦", "type": "personal"}
    print(f"[INFO] Events API：共 {len(events)} 条推送事件，其中今日 {today_events_count} 条，新发现未追踪仓库 {len(extra)} 个")
    return extra


def _persist_new_tracked_repos(new_repos):
    """把 Events API 发现的新仓库写入 tracked_config.json（由 workflow commit 回仓库）"""
    if not new_repos:
        return
    config = _load_tracked_config_raw()
    today = _beijing_now().strftime("%Y-%m-%d")
    added = []
    for repo, info in new_repos.items():
        if repo not in config.get("tracked_repos", {}):
            config.setdefault("tracked_repos", {})[repo] = {
                **info,
                "added_date": today,
                "score_history": [],
            }
            added.append(repo)
    if added:
        with open(BASE_DIR / "tracked_config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print(f"✅ 新增 {len(added)} 个活跃仓库到 tracked_config.json: {added}")


def get_today_commits_by_repo(target_bn=None, until_iso=None):
    """获取今日（北京时间）各追踪仓库的 commits，同时发现并持久化未追踪的活跃仓库"""
    bn = target_bn or _beijing_now()
    today_start_utc = bn.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    since_iso = today_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    window_info = f"since={since_iso}" + (f" until={until_iso}" if until_iso else "")
    print(f"[INFO] 数据窗口：{window_info}（北京时间 {bn.strftime('%H:%M')}，起点为今日 00:00）")

    extra = _get_today_push_repos(target_bn=bn)
    _persist_new_tracked_repos(extra)

    all_repos = dict(_load_tracked_repos())
    all_repos.update(extra)
    print(f"[INFO] 仓库池：{len(all_repos) - len(extra)} 个追踪仓库 + {len(extra)} 个今日新发现 = 合计 {len(all_repos)} 个")

    def _fetch_commits(repo, info):
        # 取 commit message 第一段（到第一个空行）+ 涉及的文件名
        # 文件名提供给 LLM 以生成"具体到文件名"的摘要（A2 修复）
        url = f"/repos/{GITHUB_USER}/{repo}/commits?since={since_iso}&per_page=100"
        if until_iso:
            url += f"&until={until_iso}"
        r = subprocess.run(
            ["gh", "api", url,
             "--jq",
             '[.[] | {msg: (.commit.message | split("\n\n")[0]), '
             'files: ([.files[]?.filename] | unique | .[0:10])}]'],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            if r.stderr.strip():
                print(f"[WARN] {repo}: gh api 失败（rc={r.returncode}）：{r.stderr.strip()[:120]}", file=sys.stderr)
            return repo, None
        try:
            rows = json.loads(r.stdout)
            msgs = []
            files = []
            for row in rows:
                m = (row.get("msg") or "").strip()
                if m:
                    # 单条 commit message 截断到 200 字符，避免 prompt 超长
                    msgs.append(m[:200])
                files.extend(row.get("files") or [])
            # 保序去重，最多 30 个文件名
            files = list(dict.fromkeys(files))[:30]
            if msgs:
                return repo, {"info": info, "commits": msgs, "files": files}
        except Exception as exc:
            print(f"[WARN] {repo}: JSON 解析失败：{exc}", file=sys.stderr)
        return repo, None

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_commits, repo, info): repo
                   for repo, info in all_repos.items()}
        for f in as_completed(futures):
            repo, data = f.result()
            if data:
                results[repo] = data
    print(f"[INFO] 扫描完成：{len(results)} / {len(all_repos)} 个仓库今日有活动")
    # 返回 (今日活跃仓库 dict, 追踪+今日发现的仓库总数)
    return results, len(all_repos)


def _dedup_ordered(seq):
    """保序去重"""
    seen = set()
    return [x for x in seq if not (x in seen or seen.add(x))]


def generate_daily_ai_review(today_commits_by_repo, total_tracked_count=None):
    """用 Claude 生成当日各仓库完成摘要和阻塞

    Args:
        today_commits_by_repo: 今日活跃仓库的 {repo: {info, commits, files}} 映射
        total_tracked_count: 追踪+今日发现的仓库总数（用于A4 在摘要中展示分母）
    """
    client = _make_client()
    if not client:
        return None, None

    if not today_commits_by_repo:
        completed = "- （今日无 commit 活动）"
        if total_tracked_count:
            completed += f"\n- 今日活跃 0 / {total_tracked_count} 个仓库"
        return completed, "无"

    from collections import Counter

    deduped = {
        repo: _dedup_ordered(data["commits"])
        for repo, data in today_commits_by_repo.items()
    }
    n = len(deduped)

    # 批量同步检测阈值：仓库数多时用 40%，避免 n-1 导致阈值过高
    if n >= 5:
        threshold = max(3, int(n * 0.4))
    else:
        threshold = max(2, n - 1)

    msg_freq = Counter(m for msgs in deduped.values() for m in set(msgs))
    shared = {m for m, cnt in msg_freq.items() if cnt >= threshold}

    # 三分类：
    #   batch_only  —— 仅做共有工作（独有=0）→ 末尾聚合为一行
    #   mixed       —— 既有共有也有独有 → 单独成块，独有完整列出
    #   unique_only —— 只有独有（无共有）→ 单独成块，独有完整列出
    # 设计：把"共有操作"在 sections 顶部独立抽出一行（每个仓库只贡献一次计数），
    # 任何仓库的独有 commit 一条不漏（不再 unique[:8] 截断）。
    batch_only = []
    mixed = []
    unique_only = []

    for repo, msgs in deduped.items():
        repo_unique = [m for m in msgs if m not in shared]
        repo_shared = [m for m in msgs if m in shared]
        if repo_unique and repo_shared:
            mixed.append((repo, repo_unique))
        elif repo_unique:
            unique_only.append((repo, repo_unique))
        else:
            batch_only.append(repo)

    sections = ""

    # 1. 抽取共有操作（顶部一块，仓库只贡献一次计数）
    if shared:
        shared_sorted = sorted(shared, key=lambda m: (-msg_freq[m], m))
        sections += f"\n【今日批量同步】共有 {len(shared)} 项操作（每个操作至少 {threshold} 个仓库执行）：\n"
        for m in shared_sorted:
            sections += f"  - {m}（{msg_freq[m]} 仓库）\n"
        sections += "\n"

    # 2. mixed：完整列出独有工作（不截断）
    for repo, unique_msgs in mixed:
        data = today_commits_by_repo[repo]
        name = data["info"].get("name", repo)
        files = data.get("files") or []
        sections += f"\n【{name}】独有工作（{len(unique_msgs)} 条，另有批量同步）：\n"
        for m in unique_msgs:
            sections += f"  - {m}\n"
        if files:
            sections += "  涉及文件（仅供你写摘要时参考，不要直接复制）：" + "、".join(files[:10]) + "\n"

    # 3. unique_only：完整列出所有
    for repo, unique_msgs in unique_only:
        data = today_commits_by_repo[repo]
        name = data["info"].get("name", repo)
        files = data.get("files") or []
        sections += f"\n【{name}】独有工作（{len(unique_msgs)} 条）：\n"
        for m in unique_msgs:
            sections += f"  - {m}\n"
        if files:
            sections += "  涉及文件（仅供你写摘要时参考，不要直接复制）：" + "、".join(files[:10]) + "\n"

    # 4. 仅做共有工作的仓库：聚合到末尾（不再独立成行）
    if batch_only:
        names = [today_commits_by_repo[r]["info"].get("name", r) for r in batch_only]
        if len(names) <= 15:
            sections += f"\n另有 {len(batch_only)} 个仓库仅执行批量同步：{', '.join(names)}\n"
        else:
            head = ', '.join(names[:15])
            sections += f"\n另有 {len(batch_only)} 个仓库仅执行批量同步（前 15 个）：{head}...\n"

    # A3：拆分 system 约束 + user 内容（[系统约束]/[用户内容] 双段前缀）
    # 设计原因：fallback LLM 不一定支持 Anthropic 的 system 参数，把约束拼到 user 头部
    # 可同时兼容主备，且对 fallback 模型输出格式约束更强
    system_rules = (
        "[系统约束]\n"
        "你是一名简洁的项目管理助理，专门汇总每日 commit 工作。\n"
        "严格遵守输出格式：\n"
        "  1. 每行以 \"- \" 开头，且仅一个仓库一行\n"
        "  2. 格式：- **仓库名**：动作1（具体到文件名/功能点）；动作2\n"
        "  3. 多个动作用全角分号「；」分隔\n"
        "  4. 不要 markdown 标题、不要代码块、不要任何解释或开场白\n"
        "  5. 若无工作内容则输出：- （今日无 commit 活动）\n"
        "  6. 描述必须具体到文件名（如 docs/X.md、babel.config.js）或功能点；\n"
        "     禁止笼统措辞（如\"补充文档\"、\"工作流文件\"、\"相关代码\"）\n"
        "  7. 看到【今日批量同步】段：合并为一行 `- **批量同步（X 仓库）**：操作1；操作2…`\n"
        "     （X 为该段仓库数；操作列表取自该段\"共有 N 项操作\"，按出现仓库数倒序）\n"
        "  8. 看到【xxx 独有工作（N 条，另有批量同步）】段：只输出独有部分，\n"
        "     格式 `- **xxx**：独有动作1；独有动作2`，**不要重复批量同步动作**\n"
        "  9. 看到【xxx 独有工作（N 条）】段（无\"另有批量同步\"）：完整输出独有部分"
    )
    user_payload = (
        f"[用户内容]\n"
        f"按以下分组信息生成每日完成总结：\n\n{sections}\n\n"
        f"直接输出 bullet 列表，不要任何解释。"
    )
    prompt = f"{system_rules}\n\n{user_payload}"
    print(f"[INFO] LLM 输入：{n} 个仓库（shared={len(shared)}, mixed={len(mixed)}, unique_only={len(unique_only)}, batch_only={len(batch_only)}），sections {len(sections)} 字符")

    resp = _call_with_fallback(messages=[{"role": "user", "content": prompt}], max_tokens=1500)
    text = resp.content[0].text.strip()
    bullet_lines = [ln for ln in text.splitlines() if ln.strip().startswith("-")]
    print(f"[INFO] LLM 响应：{len(text)} 字符，提取到 {len(bullet_lines)} 条摘要行")
    completed = "\n".join(bullet_lines) if bullet_lines else "- （详见 commits）"

    # A4：在摘要末尾追加"今日活跃 X / N 个仓库"分母，让用户能立刻判断是否漏抓
    # 去重：若 LLM 已在 prompt 提示下自行输出了该统计行，不再重复
    if total_tracked_count:
        stat_line = f"- 今日活跃 {n} / {total_tracked_count} 个仓库"
        if stat_line not in completed:
            completed += f"\n{stat_line}"
    return completed, "无"


def generate_weekly_ai_review(week_info):
    """用 Claude 生成周回顾（周六早上触发）"""
    bn = _beijing_now()
    monday_bn = bn - timedelta(days=bn.weekday())
    since_iso = (monday_bn.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _fetch_repo_count(repo, info):
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
                    return f"- {info.get('name', repo)}: {count} commits"
            except Exception:
                pass
        return None

    tracked = list(_load_tracked_repos().items())
    summary_lines = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_repo_count, repo, info) for repo, info in tracked]
        for f in as_completed(futures):
            line = f.result()
            if line:
                summary_lines.append(line)

    week_summary = "\n".join(summary_lines) if summary_lines else "（本周无 commit 记录）"

    prompt = f"""根据以下本周 commits 摘要，生成周回顾。严格按格式输出，不要其他文字：

{week_summary}

完成率：[估算本周任务完成率，如 80%]
主要成果：[本周最重要成果，一句话40字以内]
下周重点：[建议下周重点方向，一句话40字以内]"""

    resp = _call_with_fallback(messages=[{"role": "user", "content": prompt}], max_tokens=150)
    if resp is None:
        return None
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
    # 匹配整个当日块：从标题行到下一个 #### / ### 之前（含新旧两种格式）
    pattern = rf"(#### {re.escape(day_label)}(?:\s+\d{{4}}-\d{{2}}-\d{{2}})?\n)完成：\n(?:.*\n)*?阻塞：[^\n]*"
    replacement = rf"\g<1>完成：\n{completed}\n阻塞：{blocked}"
    new_body = re.sub(pattern, replacement, body)
    if new_body == body:
        # 兼容旧格式 "- 完成：xxx\n- 阻塞：xxx"
        old_pattern = rf"(#### {re.escape(day_label)}(?:\s+\d{{4}}-\d{{2}}-\d{{2}})?\n)(- 完成：[^\n]*\n- 阻塞：[^\n]*)"
        new_body = re.sub(old_pattern, rf"\g<1>完成：\n{completed}\n阻塞：{blocked}", body)
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


# ── 10. 每日自动关闭已完成 Issue ─────────────────────────────────────────────

def _get_open_task_issues(repo):
    """获取指定 repo 中带 type: task 标签的 open issues"""
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", f"{GITHUB_USER}/{repo}",
         "--label", "type: task", "--state", "open",
         "--json", "number,title,url,labels", "--limit", "50"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        raw = json.loads(r.stdout)
        return [
            {"number": i["number"], "title": i["title"], "url": i["url"],
             "labels": [l["name"] for l in i.get("labels", [])]}
            for i in raw
        ]
    except Exception:
        return []


def _close_issue(repo, number, comment):
    r = subprocess.run(
        ["gh", "issue", "close", str(number), "--repo", f"{GITHUB_USER}/{repo}",
         "--comment", comment],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def _add_issue_comment(repo, number, comment):
    subprocess.run(
        ["gh", "issue", "comment", str(number), "--repo", f"{GITHUB_USER}/{repo}",
         "--body", comment],
        capture_output=True, text=True,
    )


def _ai_match_issues(repo, commits, open_issues):
    """用 Claude Haiku 判断哪些 issue 已被 commits 完成"""
    if not commits or not open_issues:
        return []
    commit_lines = "\n".join(f"- {c}" for c in commits[:20])
    issue_lines = "\n".join(f"#{i['number']} {i['title']}" for i in open_issues)
    prompt = f"""你是 issue 管理助手。根据今日 commits 判断哪些 open issue 已完成。

仓库：{repo}
今日 commits：
{commit_lines}

Open issues（type: task）：
{issue_lines}

返回 JSON 数组，每项：{{"issue": <编号>, "confidence": "high"/"low", "reason": "<一句话>"}}
- high：commit 明确对应该 issue 的工作内容
- low：可能相关但不确定
不相关的不要返回。只返回 JSON，不要其他文字。"""
    try:
        resp = _call_with_fallback(messages=[{"role": "user", "content": prompt}], max_tokens=300)
        text = resp.content[0].text.strip()
        # 提取 JSON 部分
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if not m:
            return []
        return json.loads(m.group())
    except Exception as e:
        print(f"⚠️  AI 匹配失败（{repo}）：{e}", file=sys.stderr)
        return []


def auto_close_resolved_issues():
    """每日运行：根据今日 commits 自动关闭或提醒已完成的 type:task Issue"""
    today_by_repo, _total = get_today_commits_by_repo()
    if not today_by_repo:
        print("ℹ️  今日无 commit 活动，跳过自动关闭")
        return

    keyword_re = re.compile(r'(?:closes?|fixes?|resolves?)\s+#(\d+)', re.I)

    for repo, data in today_by_repo.items():
        commits = data["commits"]
        open_issues = _get_open_task_issues(repo)
        if not open_issues:
            print(f"   {repo}：无 open type:task issue，跳过")
            continue

        open_by_num = {i["number"]: i for i in open_issues}
        keyword_closed = set()

        # 1. 关键词匹配
        for msg in commits:
            for num_str in keyword_re.findall(msg):
                num = int(num_str)
                if num in open_by_num:
                    comment = f"🤖 根据 commit 关键词自动关闭：`{msg}`"
                    if _close_issue(repo, num, comment):
                        print(f"   ✅ {repo}#{num} 已关闭（关键词匹配：{msg[:60]}）")
                        keyword_closed.add(num)
                    else:
                        print(f"   ❌ {repo}#{num} 关闭失败", file=sys.stderr)

        # 2. AI 匹配剩余 issues
        remaining = [i for n, i in open_by_num.items() if n not in keyword_closed]
        if not remaining:
            continue
        matches = _ai_match_issues(repo, commits, remaining)
        for m in matches:
            num = m.get("issue")
            confidence = m.get("confidence", "low")
            reason = m.get("reason", "")
            if num not in open_by_num:
                continue
            if confidence == "high":
                comment = f"🤖 AI 自动关闭（高置信度）：{reason}"
                if _close_issue(repo, num, comment):
                    print(f"   ✅ {repo}#{num} 已关闭（AI high：{reason}）")
            else:
                comment = f"🤖 可能已完成，请确认后手动关闭\n\n**依据**：{reason}"
                _add_issue_comment(repo, num, comment)
                print(f"   💬 {repo}#{num} 留评论提醒（AI low：{reason}）")


def _get_recently_closed_issues(repo, days=7):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = subprocess.run(
        ["gh", "issue", "list", "--repo", f"{GITHUB_USER}/{repo}",
         "--label", "type: task", "--state", "closed",
         "--json", "number,title,closedAt", "--limit", "50"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []
    try:
        return [i for i in json.loads(r.stdout) if i.get("closedAt", "") >= since]
    except Exception:
        return []


def _create_issue_in_repo(repo, title, body, labels="type: task"):
    import tempfile as _tf, os as _os
    with _tf.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        tmp = f.name
    r = subprocess.run(
        ["gh", "issue", "create", "--repo", f"{GITHUB_USER}/{repo}",
         "--title", title, "--body-file", tmp, "--label", labels],
        capture_output=True, text=True,
    )
    _os.unlink(tmp)
    if r.returncode == 0:
        url = r.stdout.strip()
        try:
            return int(url.split("/")[-1])
        except (ValueError, IndexError):
            return None
    print(f"   ❌ 创建 Issue 失败（{repo}）：{r.stderr.strip()}", file=sys.stderr)
    return None


def backfill_missing_issues(today_commits):
    """扫描今日 commits，为没有对应 Issue 的工作补建 Issue，并评估是否立即关闭"""
    if not today_commits:
        return
    print(f"[INFO] backfill 扫描 {len(today_commits)} 个活跃仓库")
    client = _make_client()
    if not client:
        print("ℹ️  无 Claude client，跳过 Issue 补建")
        return

    for repo, data in today_commits.items():
        commits = data["commits"]
        if not commits:
            continue

        open_issues = _get_open_task_issues(repo)
        closed_recent = _get_recently_closed_issues(repo, days=7)
        all_issue_lines = "\n".join(
            f"#{i['number']} {i['title']}" for i in (open_issues + closed_recent)
        ) or "（无）"
        commit_lines = "\n".join(f"- {c}" for c in commits[:20])

        prompt = f"""你是 issue 管理助手。分析今日 commits，找出没有对应 Issue 的独立工作任务。

仓库：{repo}
今日 commits：
{commit_lines}

已有 Issues（open + 近7天已关闭，type: task）：
{all_issue_lines}

规则：
- commit 内容已有对应 Issue 的，不需要新建
- 多个相关 commit 可以合并为一个 Issue
- chore / fix typo / merge / 版本升级等维护性 commit 不需要建 Issue
- 只在有实质工作内容时才建 Issue

返回 JSON 数组，每项：
{{"title": "<一句话标题（中文）>", "body": "<两三句描述>", "commits": ["msg1", ...], "should_close": true/false, "reason": "<评估理由一句话>"}}
- should_close=true：工作已明确完成，建完 Issue 应立即关闭
- should_close=false：工作可能还在进行，保持 open
不需要补建时返回空数组 []。只返回 JSON，不要其他文字。"""

        try:
            resp = _call_with_fallback(messages=[{"role": "user", "content": prompt}], max_tokens=600)
            text = resp.content[0].text.strip()
            m = re.search(r'\[.*\]', text, re.DOTALL)
            if not m:
                print(f"   ℹ️  {repo}：AI 返回无需补建")
                continue
            suggestions = json.loads(m.group())
        except Exception as e:
            print(f"   ⚠️  AI 分析失败（{repo}）：{e}", file=sys.stderr)
            continue

        if not suggestions:
            print(f"   ℹ️  {repo}：无需补建 Issue")
            continue

        for s in suggestions:
            title = s.get("title", "").strip()
            body = s.get("body", "").strip()
            should_close = s.get("should_close", False)
            reason = s.get("reason", "")
            ref_commits = s.get("commits", [])
            if not title:
                continue
            num = _create_issue_in_repo(repo, title, body)
            if num is None:
                continue
            if should_close:
                commit_ref = "\n".join(f"- {c}" for c in ref_commits)
                _close_issue(repo, num,
                             f"🤖 自动补建并关闭：{reason}\n\n相关 commits：\n{commit_ref}")
                print(f"   ✅ {repo}#{num} 已补建并关闭：{title}")
            else:
                _add_issue_comment(repo, num, f"🤖 自动补建 Issue（{reason}）")
                print(f"   📌 {repo}#{num} 已补建（保持 open）：{title}")


# ── main ───────────────────────────────────────────────────────────────────

def main():
    fill_ai = "--fill-ai" in sys.argv
    daily_review = "--daily-review" in sys.argv
    weekly_review = "--weekly-review" in sys.argv
    auto_close = "--auto-close" in sys.argv

    # BACKFILL_DATE：手动回填历史日期（workflow_dispatch 传入 YYYY-MM-DD）
    _backfill_target_bn = None
    _backfill_date_str = os.environ.get("BACKFILL_DATE", "").strip()
    if daily_review and _backfill_date_str:
        try:
            _bd = datetime.strptime(_backfill_date_str, "%Y-%m-%d")
            _backfill_target_bn = datetime(_bd.year, _bd.month, _bd.day, 21, 30, 0,
                                           tzinfo=timezone(timedelta(hours=8)))
            print(f"[INFO] 回填模式：BACKFILL_DATE={_backfill_date_str}，"
                  f"目标北京时间 {_backfill_target_bn.strftime('%Y-%m-%d %H:%M')}")
        except ValueError:
            print(f"[WARN] BACKFILL_DATE 格式错误（{_backfill_date_str}），忽略", file=sys.stderr)

    # B1：schedule 日期修正 + 极端延迟容差
    # GitHub Actions schedule 经常延迟 4+ 小时，13:30 UTC → 17:40 UTC（01:40 北京次日）
    # 用 UTC 日期推算目标北京日期，而非 _beijing_now()，避免写到错误的日期
    _schedule_target_bn = None
    if daily_review and not _backfill_target_bn and os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        now_utc = datetime.now(timezone.utc)
        expected_utc_hour = 13  # 北京 21:30 = UTC 13:30
        expected_min = expected_utc_hour * 60 + 30
        now_min = now_utc.hour * 60 + now_utc.minute
        delay_min = (now_min - expected_min) % (24 * 60)
        if delay_min > 8 * 60:
            delay_h = delay_min / 60
            print(
                f"⚠️  schedule 延迟约 {delay_h:.1f} 小时（当前 UTC {now_utc.hour:02d}:{now_utc.minute:02d}，"
                f"预期 {expected_utc_hour:02d}:30），跳过本次 daily review 避免污染次日 Issue"
            )
            print("   提示：如需补跑昨日回顾，请用 workflow_dispatch 手动触发")
            return
        # 13:30 UTC 属于同一 UTC 日，即目标北京日期（21:30 北京 = 13:30 UTC）
        td = now_utc.date()
        _schedule_target_bn = datetime(td.year, td.month, td.day, 21, 30, 0,
                                       tzinfo=timezone(timedelta(hours=8)))
        if delay_min > 0:
            print(f"[INFO] schedule 延迟 {delay_min} 分钟，目标日期修正为 {td}（UTC 日期）")

    week_info = get_week_info()
    print(f"📅 生成周计划：{week_info['week_id']}  ({week_info['monday']} ~ {week_info['sunday']})")

    # ── 每日回顾模式 ──────────────────────────────────────────────
    if daily_review:
        bn = _backfill_target_bn or _schedule_target_bn or _beijing_now()
        day_idx = bn.weekday()  # 0=Monday … 6=Sunday
        day_label = WEEKDAY_ZH[day_idx]
        print(f"📝 --daily-review：生成 {day_label} 每日回顾（北京时间 {bn.strftime('%Y-%m-%d')}）")
        _since_utc = bn.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
        print(f"[INFO] 北京时间：{bn.strftime('%Y-%m-%d %H:%M')}，今日数据起点（UTC）：{_since_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")
        # 回填模式：限制 until，防止抓到目标日期之后的 commits
        _until_iso = None
        if _backfill_target_bn:
            _end_utc = _backfill_target_bn.replace(hour=23, minute=59, second=59) - timedelta(hours=8)
            _until_iso = _end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"[INFO] 回填数据窗口上限（UTC）：{_until_iso}")
        issue_number = get_existing_issue_number(week_info["title"])
        if not issue_number:
            print("⚠️  本周计划 Issue 不存在，跳过", file=sys.stderr)
            return
        today_commits, total_tracked = get_today_commits_by_repo(target_bn=bn, until_iso=_until_iso)
        total_commits = sum(len(d["commits"]) for d in today_commits.values())
        print(f"   今日 commits：{total_commits} 条（{len(today_commits)} / {total_tracked} 个仓库有活动）")
        completed, blocked = generate_daily_ai_review(today_commits, total_tracked_count=total_tracked)
        if completed is None:
            completed = "- （无 AI 摘要）"
            blocked = "无"
        print(f"   完成：\n{completed}")
        print(f"   阻塞：{blocked}")
        patch_daily_review(issue_number, day_label, completed, blocked)
        print("🔍 扫描今日 commits，补建遗漏 Issue...")
        backfill_missing_issues(today_commits)
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

    if auto_close:
        print("🔍 --auto-close：自动关闭已完成 Issue")
        auto_close_resolved_issues()
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
