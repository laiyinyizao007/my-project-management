# ─────────────────────────────────────────────────────────────
# scripts/install-to-repo.ps1
# 把当前仓库的 .github/workflows/auto-add-to-project.yml 部署到任意其他仓库
# 自动完成：部署 workflow + PROJECT_NUMBER 变量 + PROJECT_TOKEN Secret
#        + Actions 写权限 + 触发首次 push 让 GitHub 注册 workflow
#
# 用法：
#   pwsh scripts/install-to-repo.ps1 -TargetRepo "laiyinyizao007/other-repo"
#   pwsh scripts/install-to-repo.ps1 -TargetRepo "laiyinyizao007/foo" -ProjectNumber 2
#   pwsh scripts/install-to-repo.ps1 -TargetRepo "laiyinyizao007/foo" -AnthropicApiKey "sk-ant-..."
#
# 唯一手动步骤（每次脚本运行时输入一次 PAT）：
#   脚本会提示输入 PAT（不会显示到屏幕），用它自动给目标仓库加 PROJECT_TOKEN Secret。
#   PAT 需 scope: repo + project。脚本不会把 PAT 写入任何文件。
#
# 前置条件：
#   1. 已 `gh auth login`（PAT 输入脚本后再用 gh CLI 自动加 Secret）
# ─────────────────────────────────────────────────────────────

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$TargetRepo,

    [string]$ProjectNumber = '1',

    [string]$ProjectToken = '',

    [string]$AnthropicApiKey = '',

    [string]$AnthropicBaseUrl = ''
)

$ErrorActionPreference = 'Stop'

. "$PSScriptRoot/common.ps1"
$tokenResult  = Get-ProjectToken -ProjectToken $ProjectToken
$ProjectToken = $tokenResult.Token
$cleanupToken = $tokenResult.Cleanup

# 从当前仓库动态获取源仓库路径
$SOURCE_REPO = (Get-CurrentRepo).Full

# ── 辅助：读取并部署单个 workflow 文件 ────────────────────────
function Deploy-Workflow {
    param([string]$WfPath, [string]$Label)

    $b64 = gh api "repos/$SOURCE_REPO/contents/$WfPath" --jq '.content'
    $b64Clean = $b64 -replace '\s', ''
    $content = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($b64Clean))
    if (-not $content) { throw "❌ 无法读取源文件 $WfPath" }

    $existing = gh api "repos/$TargetRepo/contents/$WfPath" 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing) {
        $sha = $existing | ConvertFrom-Json | Select-Object -ExpandProperty sha
        Write-Host "   🔄 $Label 已存在，更新（SHA: $($sha.Substring(0,7))）" -ForegroundColor DarkYellow
        @{
            message = "chore: 更新 $Label（同步自 $SOURCE_REPO）"
            content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($content))
            sha     = $sha
        } | ConvertTo-Json -Depth 10 | gh api "repos/$TargetRepo/contents/$WfPath" --method PUT --input - | Out-Null
    } else {
        @{
            message = "chore: 部署 $Label（同步自 $SOURCE_REPO）"
            content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($content))
        } | ConvertTo-Json -Depth 10 | gh api "repos/$TargetRepo/contents/$WfPath" --method PUT --input - | Out-Null
        Write-Host "   ✅ $Label 已创建" -ForegroundColor Green
    }
}

# 动态枚举源仓库中待部署的 workflow（排除管理仓库专用项）
# 新增仅管理仓库的 workflow 时，在此同步更新（auto-deploy-to-new-repos.yml 有权威列表）
$exclude = @('auto-deploy-to-new-repos.yml', 'auto-create-sprint.yml', 'weekly-plan.yml', 'create-milestone.yml', 'sync-labels.yml')
$workflows = @(gh api "repos/$SOURCE_REPO/contents/.github/workflows" --jq '.[].name' |
    Where-Object { $_ -notin $exclude } |
    ForEach-Object { ".github/workflows/$_" })

# 动态枚举 .github/scripts/ 中所有脚本（无排除列表）
$scripts = @()
$rawScripts = gh api "repos/$SOURCE_REPO/contents/.github/scripts" --jq '.[].name' 2>$null
if ($LASTEXITCODE -eq 0 -and $rawScripts) {
    $scripts = @($rawScripts | ForEach-Object { ".github/scripts/$_" })
}

Write-Host "🚀 [1/5] 部署 $($workflows.Count) 个 workflow + $($scripts.Count) 个脚本到 $TargetRepo ..." -ForegroundColor Yellow
foreach ($wf in $workflows) {
    $label = [System.IO.Path]::GetFileNameWithoutExtension($wf)
    Deploy-Workflow $wf $label
}
foreach ($sc in $scripts) {
    $label = [System.IO.Path]::GetFileName($sc)
    Deploy-Workflow $sc $label
}

Write-Host ""

# 3) 设置目标仓库的 PROJECT_NUMBER 变量
Write-Host "🔧 [2/5] 设置 $TargetRepo 的 PROJECT_NUMBER=$ProjectNumber ..." -ForegroundColor Yellow

gh variable set PROJECT_NUMBER --body $ProjectNumber --repo $TargetRepo
Write-Host "   ✅ 仓库变量已设置" -ForegroundColor Green
Write-Host ""

# 4) 给目标仓库添加 PROJECT_TOKEN Secret（add-to-project workflow 需要它写 Project）
Write-Host "🔐 [3/5] 给 $TargetRepo 添加 Secret: PROJECT_TOKEN ..." -ForegroundColor Yellow

gh secret set PROJECT_TOKEN --body $ProjectToken --repo $TargetRepo
Remove-ProjectToken -Cleanup $cleanupToken
Write-Host "   ✅ Secret 已添加（不会保存到任何文件）" -ForegroundColor Green
Write-Host ""

# 4) 设置 Claude API Key（claude.yml workflow 需要）
if ($AnthropicApiKey) {
    Write-Host "🔐 [3b/5] 给 $TargetRepo 添加 Secret: ANTHROPIC_API_KEY ..." -ForegroundColor Yellow
    gh secret set ANTHROPIC_API_KEY --body $AnthropicApiKey --repo $TargetRepo
    Write-Host "   ✅ ANTHROPIC_API_KEY 已设置" -ForegroundColor Green
    if ($AnthropicBaseUrl) {
        gh secret set ANTHROPIC_BASE_URL --body $AnthropicBaseUrl --repo $TargetRepo
        Write-Host "   ✅ ANTHROPIC_BASE_URL 已设置" -ForegroundColor Green
    }
    Write-Host ""
}

# 5) 修目标仓库的 Actions 权限为 write（避免新增的仓库再失败）
Write-Host "🔧 [4/5] 设置 $TargetRepo 的 Actions workflow 权限为 write ..." -ForegroundColor Yellow

$permBody = @{
    default_workflow_permissions     = 'write'
    can_approve_pull_request_reviews = $false
} | ConvertTo-Json

$permBody | gh api "repos/$TargetRepo/actions/permissions/workflow" --method PUT --input - | Out-Null
Write-Host "   ✅ Actions 权限已更新为 write" -ForegroundColor Green
Write-Host ""

# 6) 触发空 push 让 GitHub 注册 workflow（首次部署必须）
Write-Host "🚀 [5/5] 触发空 push 让 GitHub 注册 workflow..." -ForegroundColor Yellow

$keepContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("# Generated by install-to-repo.ps1`n"))

# 检查 .github/.keep 是否已存在
$existingKeep = gh api "repos/$TargetRepo/contents/.github/.keep" 2>$null
if ($LASTEXITCODE -eq 0 -and $existingKeep) {
    Write-Host "   ⏭️  .github/.keep 已存在，跳过空 push" -ForegroundColor DarkYellow
} else {
    $keepBody = @{
        message = "chore: 初始化 .github 目录（触发 workflow 注册）"
        content = $keepContent
    } | ConvertTo-Json

    $keepBody | gh api "repos/$TargetRepo/contents/.github/.keep" --method PUT --input - | Out-Null
    Write-Host "   ✅ 已创建 .github/.keep（这次 push 会触发 workflow 注册）" -ForegroundColor Green
}

Write-Host ""

Write-Host "🎉 全部完成！$TargetRepo 已配置为自动将 Issue 加入 Project #${ProjectNumber}。" -ForegroundColor Cyan
Write-Host ""
Write-Host "✅ 本次已为 $TargetRepo 部署："
foreach ($wf in $workflows) { Write-Host "   • Workflow: $wf" }
foreach ($sc in $scripts)   { Write-Host "   • Script:   $sc" }
Write-Host "   • 仓库变量: PROJECT_NUMBER=$ProjectNumber"
Write-Host "   • 仓库 Secret: PROJECT_TOKEN（PAT 未写入文件）"
if ($AnthropicApiKey) { Write-Host "   • 仓库 Secret: ANTHROPIC_API_KEY" }
Write-Host "   • Actions workflow 权限: write"
Write-Host ""
Write-Host "📌 下一步：" -ForegroundColor Yellow
Write-Host "   1. 等待 1-2 分钟让 GitHub 完成 workflow 注册（首次部署必须）"
Write-Host "   2. 在 $TargetRepo 中创建任意 Issue，验证自动入看板"
Write-Host "   3. 确认 Project 看板中能看到新 Issue"
Write-Host ""
Write-Host "💡 提示：如 Issue 创建后未自动入看板，可去 Settings → Actions 查看 workflow 是否报错。" -ForegroundColor DarkCyan
Write-Host ""