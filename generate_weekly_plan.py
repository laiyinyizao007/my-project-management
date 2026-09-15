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


# ── 5. Claude Haiku 生成计划 ───────────────────────────────────────────────

def generate_ai_plan(week_info, weekly_progress, sprint_issues):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    issue_list = ""
    if sprint_issues:
        for i in sprint_issues[:15]:
            labels_str = f" `{'` `'.join(i['labels'])}`" if i["labels"] else ""
            issue_list += f"- #{i['number']} {i['title']}{labels_str}\n"
    else:
        issue_list = "（无续期任务）\n"

    progress_section = weekly_progress or "（上周无进展记录）"

    prompt = f"""你是一个个人效率助手。根据以下信息，为本周（{week_info['week_id']}）生成工作计划建议。

## 上周进展
{progress_section}

## 本周 Sprint 续期任务
{issue_list}

请生成：
1. **本周重点**：3 条最重要的目标（一句话，动词开头）
2. **建议执行顺序**：从续期任务中选出优先处理的 3-5 项，用 checklist 格式（`- [ ] #编号 任务名`）

要求：
- 中文输出
- 简洁直接，每条不超过 40 字
- 重点参考上周未完成的方向和高优先级标签（P0/P1）
- 仅输出这两部分内容，不要其他说明
"""

    try:
        import anthropic
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = anthropic.Anthropic(**kwargs)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"⚠️  Claude API 调用失败：{e}", file=sys.stderr)
        return None


# ── 6. 构建 Issue 正文 ─────────────────────────────────────────────────────

def build_issue_body(week_info, ai_plan, weekly_progress, sprint_issues):
    monday, sunday = week_info["monday"], week_info["sunday"]
    lines = [f"## {monday} ~ {sunday}", ""]

    # AI 建议部分
    if ai_plan:
        lines += ["### 🤖 AI 本周建议", "", ai_plan, "", "---", ""]
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

    # 人工填写部分
    lines += [
        "### 本周目标",
        "",
        "（基于上方 AI 建议编辑，或自行填写）",
        "",
        "### 计划任务",
        "",
        "- [ ] ",
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
    """为已存在但 AI 建议缺失的 Issue 生成并更新 AI 建议区块"""
    ai_plan = generate_ai_plan(week_info, weekly_progress, sprint_issues)
    if not ai_plan:
        print("ℹ️  AI 计划生成失败，跳过更新", file=sys.stderr)
        return

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

    # 替换 AI 建议区块（无论是占位文字还是已有内容，都整块替换）
    body = re.sub(
        r"### 🤖 AI 本周建议\n\n.*?(?=\n---)",
        f"### 🤖 AI 本周建议\n\n{ai_plan}",
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
        print(f"✅ Issue #{issue_number} AI 建议已更新")
    else:
        print(f"❌ 更新失败: {r2.stderr[:200]}", file=sys.stderr)


# ── main ───────────────────────────────────────────────────────────────────

def main():
    fill_ai = "--fill-ai" in sys.argv

    week_info = get_week_info()
    print(f"📅 生成周计划：{week_info['week_id']}  ({week_info['monday']} ~ {week_info['sunday']})")

    # 查本周 Sprint issues（同时获取 project 上下文，供后续 add_issue_to_project 使用）
    sprint_title = os.environ.get("SPRINT_TITLE") or week_info["sprint"]
    print(f"🔍 查询 Sprint：{sprint_title}")
    sprint_issues, project_id, sprint_field_id, iteration_id = get_sprint_issues(sprint_title)
    if sprint_issues is None and project_id is None:
        print("⚠️  GraphQL 查询失败，降级为 repo 全量 open issues")
        sprint_issues = get_open_issues_fallback() or []
    elif sprint_issues is None:
        sprint_issues = get_open_issues_fallback() or []
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
