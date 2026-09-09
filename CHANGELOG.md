# 变更日志（Changelog）

所有重要变更均记录于此文件。

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，并遵循 [语义化版本号](https://semver.org/lang/zh-CN/) 规范。

## [Unreleased]

## [1.3.0] - 2026-09-09

### Added（新增）

- **`scripts/setup-project-board.ps1`**：一次性配置 Project v2 看板
  - 通过 GraphQL API 创建 Priority / Type / Size / Sprint 四个自定义字段
  - 创建 Table 视图（表格）和 Sprint 视图（迭代看板）
  - 幂等设计，重复运行安全

- **`.github/workflows/auto-set-project-fields.yml`**：标签联动字段自动化
  - 打标签时自动同步 Project 字段值（Priority / Type / Size / Status）
  - 动态查询字段 ID，无需维护额外变量
  - 已纳入全仓库自动部署

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
[Unreleased]: https://github.com/laiyinyizao007/my-project-management/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/laiyinyizao007/my-project-management/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/laiyinyizao007/my-project-management/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/laiyinyizao007/my-project-management/releases/tag/v1.0.0
