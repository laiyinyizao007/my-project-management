# ─────────────────────────────────────────────────────────────
# scripts/install-to-repo.ps1
# 把当前仓库的 .github/workflows/add-to-project.yml 部署到任意其他仓库
# 同时把该仓库的 Actions 权限改为 write（避免新仓库再踩同样的坑）
#
# 用法：
#   pwsh scripts/install-to-repo.ps1 -TargetRepo "laiyinyizao007/other-repo"
#   pwsh scripts/install-to-repo.ps1 -TargetRepo "laiyinyizao007/foo" -ProjectNumber 1
#
# 前提：
#   - 已 `gh auth login`，或已设置 GH_TOKEN 环境变量
#   - token 包含 repo + workflow scope
# ─────────────────────────────────────────────────────────────

[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$TargetRepo,

    [string]$ProjectNumber = '1'
)

$ErrorActionPreference = 'Stop'

# 1) 读取源仓库的 workflow 内容
$SOURCE_REPO = 'laiyinyizao007/my-project-management'
$WORKFLOW_PATH = '.github/workflows/add-to-project.yml'

Write-Host "📦 读取源 workflow..." -ForegroundColor Cyan
$sourceContent = gh api "repos/$SOURCE_REPO/contents/$WORKFLOW_PATH" `
    --jq '.content' | ForEach-Object { [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($_)) }

if (-not $sourceContent) {
    throw "❌ 无法读取源文件 $WORKFLOW_PATH"
}

Write-Host "   ✅ 已加载 $(($sourceContent -split "`n").Count) 行 workflow 内容" -ForegroundColor Green
Write-Host ""

# 2) 把 workflow 推送到目标仓库
Write-Host "🚀 部署到 $TargetRepo ..." -ForegroundColor Yellow

# 检查目标仓库是否有同名 workflow
$existing = gh api "repos/$TargetRepo/contents/$WORKFLOW_PATH" 2>$null
if ($LASTEXITCODE -eq 0 -and $existing) {
    $sha = $existing | ConvertFrom-Json | Select-Object -ExpandProperty sha
    Write-Host "   🔄 工作流已存在，将更新（SHA: $($sha.Substring(0,7))）" -ForegroundColor DarkYellow
    $body = @{
        message = "chore: 部署 add-to-project workflow（同步自 $SOURCE_REPO）"
        content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($sourceContent))
        sha     = $sha
    } | ConvertTo-Json -Depth 10
    gh api "repos/$TargetRepo/contents/$WORKFLOW_PATH" --method PUT --input - <<< $body | Out-Null
    Write-Host "   ✅ 工作流已更新" -ForegroundColor Green
} else {
    $body = @{
        message = "chore: 部署 add-to-project workflow（同步自 $SOURCE_REPO）"
        content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($sourceContent))
    } | ConvertTo-Json -Depth 10
    gh api "repos/$TargetRepo/contents/$WORKFLOW_PATH" --method PUT --input - <<< $body | Out-Null
    Write-Host "   ✅ 工作流已创建" -ForegroundColor Green
}

Write-Host ""

# 3) 设置目标仓库的 PROJECT_NUMBER 变量
Write-Host "🔧 [2/3] 设置 $TargetRepo 的 PROJECT_NUMBER=$ProjectNumber ..." -ForegroundColor Yellow

gh variable set PROJECT_NUMBER --body $ProjectNumber --repo $TargetRepo
Write-Host "   ✅ 仓库变量已设置" -ForegroundColor Green
Write-Host ""

# 4) 修目标仓库的 Actions 权限为 write（避免新增的仓库再失败）
Write-Host "🔐 [3/3] 设置 $TargetRepo 的 Actions workflow 权限为 write ..." -ForegroundColor Yellow

$permBody = @{
    default_workflow_permissions     = 'write'
    can_approve_pull_request_reviews = $false
} | ConvertTo-Json

gh api "repos/$TargetRepo/actions/permissions/workflow" --method PUT --input - <<< $permBody | Out-Null
Write-Host "   ✅ Actions 权限已更新为 write" -ForegroundColor Green
Write-Host ""

Write-Host "🎉 全部完成！$TargetRepo 已配置为自动将 Issue 加入 Project #${ProjectNumber}。" -ForegroundColor Cyan
Write-Host ""
Write-Host "📌 下一步：" -ForegroundColor Yellow
Write-Host "   1. 在 $TargetRepo 中创建任意 Issue，验证自动入看板"
Write-Host "   2. 确认 Project 看板中能看到新 Issue"
Write-Host ""