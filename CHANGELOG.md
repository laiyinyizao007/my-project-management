# 变更日志（Changelog）

所有重要变更均记录于此文件。

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，并遵循 [语义化版本号](https://semver.org/lang/zh-CN/) 规范。

## [Unreleased]

## [1.10.0] - 2026-09-11

### Added（新增）

- **`CLAUDE.md`**：Claude 行为规范文档
  - 定义 Claude 在各仓库 Issue 中的角色、响应语言、工作边界和行为规范
  - 响应格式：理解 / 分析 / 行动 / 注意事项四段式
  - 语言策略：与 Issue 语言一致（中文 Issue → 中文回复，英文 Issue → 英文回复）

- **`.github/ISSUE_TEMPLATE/claude-task.yml`**：Claude 任务 Issue 模板
  - 专为 `@claude` 触发设计，含"任务描述"（必填）、"任务类型"下拉框（代码分析/代码生成/技术咨询/文档撰写/其他）、"补充上下文"三个字段
  - 与 `claude.yml` 配合使用，让用户通过标准模板提交 Claude 任务

### Changed（变更）

- 合并 `laiyinyizao007/issuefighter` 仓库内容（将其独有的 `CLAUDE.md` 和 `claude-task.yml` 纳入本仓库管理）
- 归档 `laiyinyizao007/issuefighter`（保留历史记录，仅接受只读访问）

## [1.9.0] - 2026-09-11

### Added（新增）

- **`.github/workflows/claude.yml`**：Claude Issue 处理器
  - 触发：`issue_comment[created]`（含 `@claude`）、`issues[opened]`（标题/正文含 `@claude`）、`workflow_dispatch`
  - 仅限仓库 Owner 触发（防止外部调用）
  - 安装 `@anthropic-ai/claude-code` CLI，提取 prompt，调用 `claude --print`，将结果发布为 Issue 评论
  - 依赖：`secrets.ANTHROPIC_API_KEY`（必填）、`secrets.ANTHROPIC_BASE_URL`（可选）
  - 已纳入全仓库自动部署

### Changed（变更）

- **`auto-deploy-to-new-repos.yml`**：新增 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` Secret 传播
  - 部署时自动将管理仓库的两个 Secret 写入目标仓库（仅在 Secret 非空时传播）
- **`scripts/install-to-repo.ps1`**：新增 `-AnthropicApiKey` 和 `-AnthropicBaseUrl` 参数
  - 手动安装时可选传入 API Key，脚本自动调用 `gh secret set` 写入目标仓库
  - 完成摘要中动态显示 `ANTHROPIC_API_KEY` 是否已设置

## [1.8.0] - 2026-09-10

### Changed（变更）

- **`auto-deploy-to-new-repos.yml`**：WF 列表改为动态枚举 + 排除列表
  - 删除顺序变量 WF1/WF2/WF3 及 CONTENT1/2/3 预取逻辑
  - 动态调用 GitHub Contents API 枚举源仓库所有 workflow，过滤排除列表
  - `deploy_to()` 函数改为循环实现，支持任意数量 workflow
  - **新增可部署 workflow 时无需修改此文件**；新增管理仓库专用 workflow 时将文件名加入排除列表即可
- **`scripts/install-to-repo.ps1`**：镜像同样的排除列表逻辑，Deploy-Workflow 调用改为动态循环；完成摘要动态显示实际部署的 workflow 列表

## [1.7.0] - 2026-09-10

### Changed（变更）

- **`auto-create-sprint.yml`**：Sprint 迭代命名统一为 ISO 8601 周格式 `Sprint YYYY-WNN`
  - 与 `create-milestone.yml` 的 Milestone 命名对齐，消除两者不一致问题
  - 移除顺序递增编号逻辑，改用 ISO 周数计算（与 `weekly-plan.yml`/`create-milestone.yml` 算法完全一致）
  - 去重和续期逻辑不受影响（均基于 `startDate` 和迭代 ID）
- **`auto-deploy-to-new-repos.yml`**：SOURCE 仓库路径改为动态获取（`gh repo view`），不再依赖硬编码的 `my-project-management`；WF 列表处新增维护注释
- **`scripts/setup.ps1`**：`$OWNER` 和 `$REPO` 改为动态获取（`gh api user` + `gh repo view`），消除硬编码用户名/仓库名
- **`scripts/install-to-repo.ps1`**：`$SOURCE_REPO` 改为动态获取；重构 PAT 逻辑为调用 `common.ps1`
- **`scripts/setup-project-board.ps1`**：仓库名改为动态获取；重构 PAT 逻辑为调用 `common.ps1`

### Added（新增）

- **`scripts/common.ps1`**：PowerShell 共享函数库
  - `Get-ProjectToken`：统一 PAT 获取逻辑，消除两个脚本中的重复代码
  - `Remove-ProjectToken`：会话结束时清理临时环境变量
  - `Get-CurrentRepo`：从 `gh` CLI 动态获取当前仓库路径

## [1.6.0] - 2026-09-10

### Added（新增）

- **`auto-close-issue.yml`**：Issue 关闭自动将 Project Status 设为 Done
  - 触发：`issues: [closed]`
  - 查询 Project Status 字段，定位 Done 选项后更新对应 Project item
  - 已纳入全仓库自动部署

- **Sprint 续期（auto-create-sprint.yml）**：每周创建新 Sprint 时自动续期
  - 遍历上一个 Sprint 中所有未关闭的 Issue，移动到新 Sprint
  - 分页处理，支持大型看板

### Changed（变更）

- **`auto-deploy-to-new-repos.yml`**：新增 `force_update` 输入参数
  - `force_update=true` 时强制更新已部署的 workflow 文件（用于推送修复到所有仓库）
  - 新增 `auto-close-issue.yml` 到部署列表
- **`scripts/install-to-repo.ps1`**：新增 `auto-close-issue.yml` 部署

## [1.5.0] - 2026-09-10

### Added（新增）

- **`auto-create-sprint.yml`**：每周一自动创建下一个 Sprint 迭代
  - 查询现有迭代，自动递增编号（Sprint 1 → Sprint 2 → …）
  - 本周已有迭代时跳过（幂等）
  - 仅部署在管理仓库，不推送到目标仓库

## [1.4.0] - 2026-09-09

### Added（新增）

- **By Project 视图**：Table 布局，按 Repository 分组，方便聚焦单个项目的 Issue
- **每周规划工作流**：Sprint 分配流程文档（见 PROJECTWIKI.md §8）

## [1.3.0] - 2026-09-09

### Added（新增）

- **`scripts/setup-project-board.ps1`**：一次性配置 Project v2 看板
  - 通过 GraphQL API 创建 Priority / Category / Size / Sprint 四个自定义字段
  - 创建 Table 视图（表格）和 Sprint 视图（迭代看板）
  - 幂等设计，重复运行安全

- **`.github/workflows/auto-set-project-fields.yml`**：标签联动字段自动化
  - 打标签时自动同步 Project 字段值（Priority / Category / Size / Status）
  - 动态查询字段 ID，无需维护额外变量
  - 已纳入全仓库自动部署

### Fixed（修复）

- `setup-project-board.ps1`：将 "Type" 字段重命名为 "Category"，避免与 GitHub 内置保留字段名冲突

### Changed（变更）

- `auto-deploy-to-new-repos.yml`：同时部署 `auto-set-project-fields.yml`；`deploy_to()` 函数重构为独立 `deploy_file()` 辅助函数，支持多文件部署
- `scripts/install-to-repo.ps1`：使用 `Deploy-Workflow` 辅助函数同时部署两个 workflow

## [1.2.0] - 2026-09-09

### Added（新增）

- **GitHub App + Cloudflare Worker 实时自动部署**（commit `fdea78c`）
  - `cloudflare-worker/src/index.js`：webhook 中继 Worker，验证 HMAC-SHA256 签名后触发 `repository_dispatch`
  - `cloudflare-worker/wrangler.toml`：Wrangler 配置，Worker 名称 `github-repo-autodeploy`
  - GitHub App（ID: 4884198）订阅 `Repositories` 事件，webhook 指向 `https://github-repo-autodeploy.lovablelife.workers.dev`
  - 新仓库创建后实时（秒级）触发自动部署，无需人工干预

## [1.1.0] - 2026-09-09

### Added（新增）

- **`auto-deploy-to-new-repos.yml`**（commit `6f68f4e`）：向所有未接入仓库自动部署 Issue 追踪 workflow
  - 触发方式：`repository_dispatch`（实时）、`schedule`（每 6 小时兜底）、`workflow_dispatch`（手动）
  - 5 步部署：workflow 文件 + `PROJECT_NUMBER` 变量 + `PROJECT_TOKEN` Secret + Actions 写权限 + `.github/.keep` 注册

### Fixed（修复）

- **base64 解码 bug**（commit `05ca22c`）：GitHub Contents API 返回的 base64 每 60 字符含换行，`bash tr -d '\n'` 去除后再解码，修复 YAML 内容损坏问题（token 被随机插入空格如 `add-to-project @v2.0.0`）

## [1.0.1] - 2026-09-09

### Added（新增）

- **`install-to-repo.ps1` 自动化 PROJECT_TOKEN Secret**（commit `ccb2cc0`）
  - 新增 `-ProjectToken` 参数和安全 `Read-Host -AsSecureString` 输入
  - 自动调用 `gh secret set PROJECT_TOKEN` 添加 Secret，消除最后一个手动步骤
  - 使用 `SecureStringToBSTR` + `ZeroFreeBSTR` 确保 PAT 不驻留内存

### Fixed（修复）

- **PowerShell `$null` 兼容性**（commit `fd15df6`）：`$existing = cmd 2>$null` 赋值在 PowerShell 严格模式下的兼容处理

## [1.0.0] - 2026-09-09

### Added（新增）

- **`auto-add-to-project.yml`**（commit `bfe2a8a`）：Issue 自动加入 Project v2 看板
  - 触发：`issues: [opened, reopened, transferred]`
  - 使用 `actions/add-to-project@v2.0.0` + `PROJECT_TOKEN`（classic PAT with repo+project scope）
  - 读取 `vars.PROJECT_NUMBER` 确定目标看板

- **`weekly-plan.yml`**：每周一 09:00 自动创建 `[Weekly] YYYY-WXX` Issue

- **`create-milestone.yml`**：每周一 09:00 自动创建 `Sprint YYYY-WXX` Milestone（due: 周五）

- **`sync-labels.yml`**：push 到 main 时自动同步 `.github/labels.yml` 标签配置

- **`scripts/install-to-repo.ps1`**（commit `cb1854a`）：手动将 workflow 部署到任意仓库的 PowerShell 脚本

- **`.github/labels.yml`**：25 个分类标签定义

- **`.gitignore`**（commit `4ab01b8`）：忽略 `proj.json`、`orgs.json` 等临时文件

### Changed（变更）

- `auto-add-to-project.yml` 使用 `PROJECT_TOKEN` 替代 `GITHUB_TOKEN`（commit `425753c`），解决 classic PAT scope 问题
- workflow 文件重命名（commit `f944f49`）：统一命名规范

<!-- 比对链接 -->
[Unreleased]: https://github.com/laiyinyizao007/my-project-management/compare/v1.10.0...HEAD
[1.10.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.9.0...v1.10.0
[1.9.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/laiyinyizao007/my-project-management/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/laiyinyizao007/my-project-management/releases/tag/v1.0.0
