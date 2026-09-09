# ─────────────────────────────────────────────────────────────
# scripts/setup.ps1
# 一键完成 GitHub Project 看板创建 + 仓库变量设置 + 首个 Sprint Milestone
# 用法：pwsh scripts/setup.ps1
# 前置：已安装 gh CLI 并通过 `gh auth login` 登录
# ─────────────────────────────────────────────────────────────

$ErrorActionPreference = 'Stop'

$OWNER = 'laiyinyizao007'
$REPO  = 'laiyinyizao007/my-project-management'

Write-Host "🚀 开始一次性设置..." -ForegroundColor Cyan
Write-Host "   Owner: $OWNER"
Write-Host "   Repo : $REPO"
Write-Host ""

# ─────────────────────────────────────────
# 1) 创建 GitHub Project
# ─────────────────────────────────────────
Write-Host "📋 [1/4] 创建 GitHub Project '任务看板'..." -ForegroundColor Yellow

# 先检查是否已存在同名 Project
$existingProject = gh project list --owner $OWNER --format json --limit 100 |
    ConvertFrom-Json |
    Where-Object { $_.title -eq '任务看板' } |
    Select-Object -First 1

if ($existingProject) {
    $projectNumber = $existingProject.number
    Write-Host "   ⏭️  Project '任务看板' 已存在（#$projectNumber），跳过创建。" -ForegroundColor DarkYellow
} else {
    $projectUrl = gh project create --owner $OWNER --title '任务看板' --format json |
        ConvertFrom-Json |
        Select-Object -ExpandProperty url
    $projectNumber = ($projectUrl.Split('/')[-1]).Trim()
    Write-Host "   ✅ Project 已创建：$projectUrl" -ForegroundColor Green
}

Write-Host ""

# ─────────────────────────────────────────
# 2) 将 Project 编号设置为仓库变量
# ─────────────────────────────────────────
Write-Host "🔧 [2/4] 设置仓库变量 PROJECT_NUMBER=$projectNumber..." -ForegroundColor Yellow

gh variable set PROJECT_NUMBER --body $projectNumber --repo $REPO
Write-Host "   ✅ 仓库变量已设置。" -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────
# 3) 计算当周 Sprint 信息（ISO 周 + 周五日期）
# ─────────────────────────────────────────
Write-Host "📅 [3/4] 计算当周 Sprint 信息..." -ForegroundColor Yellow

$today   = Get-Date
$culture = [System.Globalization.CultureInfo]::InvariantCulture

# ISO 周数（FirstFourDayWeek 规则 + Monday 为周首日）
$weekNum = $culture.Calendar.GetWeekOfYear(
    $today,
    [System.Globalization.CalendarWeekRule]::FirstFourDayWeek,
    [System.DayOfWeek]::Monday
)
$weekStr  = $weekNum.ToString('00')
$isoYear  = (Get-Culture).Calendar.GetYear($today)  # 简化：跨年边角案例由 GitHub Actions 处理

# 计算当周周一和周五
$todayDayOfWeek = [int]$today.DayOfWeek  # 0=Sun, 1=Mon, ..., 6=Sat
if ($todayDayOfWeek -eq 0) { $todayDayOfWeek = 7 }

$mondayOffset = 1 - $todayDayOfWeek
$fridayOffset = 5 - $todayDayOfWeek

$monday  = $today.AddDays($mondayOffset)
$friday  = $today.AddDays($fridayOffset)

$sprintTitle = "Sprint $isoYear-W$weekStr"
$mondayStr   = $monday.ToString('yyyy-MM-dd')
$fridayStr   = $friday.ToString('yyyy-MM-dd')

Write-Host "   本周: $mondayStr ~ $fridayStr" -ForegroundColor Gray
Write-Host "   Sprint: $sprintTitle" -ForegroundColor Gray
Write-Host ""

# ─────────────────────────────────────────
# 4) 创建首个 Sprint Milestone
# ─────────────────────────────────────────
Write-Host "🎯 [4/4] 创建 Milestone '$sprintTitle'..." -ForegroundColor Yellow

# 检查是否已存在同名 Milestone
$existingMilestone = gh api "repos/$REPO/milestones?state=open&per_page=50" --jq '.[].title' |
    Select-String -Pattern "^$([regex]::Escape($sprintTitle))$" -SimpleMatch

if ($existingMilestone) {
    Write-Host "   ⏭️  Milestone '$sprintTitle' 已存在，跳过创建。" -ForegroundColor DarkYellow
} else {
    $dueOnUtc = $friday.ToString('yyyy-MM-ddT23:59:59Z')
    gh api "repos/$REPO/milestones" --method POST `
        --field title="$sprintTitle" `
        --field due_on="$dueOnUtc" `
        --field description="${sprintTitle}（$mondayStr ~ $fridayStr）" |
        Out-Null
    Write-Host "   ✅ Milestone 已创建（截止 $dueOnUtc）。" -ForegroundColor Green
}

Write-Host ""
Write-Host "🎉 全部完成！" -ForegroundColor Cyan
Write-Host ""
Write-Host "📌 后续仅剩 1 步手动操作：" -ForegroundColor Yellow
Write-Host "   前往 GitHub Project '任务看板' → ⚙️ Settings → Workflows"
Write-Host "   开启 'Item closed' → 状态改为 'Done'"
Write-Host "   （这样关闭 Issue 时会自动移到 Done 列）"
Write-Host ""
