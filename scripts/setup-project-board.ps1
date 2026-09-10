# ─────────────────────────────────────────────────────────────
# scripts/setup-project-board.ps1
# 一次性配置 GitHub Project v2 看板：
#   - 创建 Priority / Type / Size 自定义字段
#   - 创建 Sprint 迭代字段
#   - 创建 Table 视图 + Sprint 视图
#
# 用法：
#   pwsh scripts/setup-project-board.ps1
#   pwsh scripts/setup-project-board.ps1 -ProjectNumber 2
#
# 前置条件：
#   已 gh auth login，且 PAT 含 project scope
# ─────────────────────────────────────────────────────────────

[CmdletBinding()]
param(
    [string]$ProjectNumber = '',
    [string]$Owner = '',
    [string]$ProjectToken = ''
)

$ErrorActionPreference = 'Stop'

# ── PAT 输入（需要 project scope）────────────────────────────
$cleanupToken = $false
if (-not $ProjectToken) {
    if ($env:GH_TOKEN) {
        $ProjectToken = $env:GH_TOKEN
        Write-Host "🔑 使用环境变量 GH_TOKEN" -ForegroundColor DarkGray
    } else {
        Write-Host "🔑 请输入 PAT（需要 repo + project scope，输入时不可见）：" -ForegroundColor Cyan -NoNewline
        $secure = Read-Host -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try { $ProjectToken = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr) }
        finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        Write-Host ""
        $env:GH_TOKEN = $ProjectToken
        $cleanupToken = $true
    }
}

# ── 参数推断 ──────────────────────────────────────────────────
if (-not $Owner) {
    $Owner = gh api user --jq '.login'
}
if (-not $ProjectNumber) {
    $raw = gh variable get PROJECT_NUMBER --repo "$Owner/my-project-management" 2>$null
    $ProjectNumber = if ($raw) { $raw.Trim() } else { '1' }
}

Write-Host "🔧 配置 Project v2 看板" -ForegroundColor Cyan
Write-Host "   Owner: $Owner  |  Project: #$ProjectNumber" -ForegroundColor DarkGray
Write-Host ""

# ── GraphQL 辅助函数 ──────────────────────────────────────────
function Invoke-GQL {
    param([hashtable]$Body)
    $json = $Body | ConvertTo-Json -Depth 20 -Compress
    $result = $json | gh api graphql --input - | ConvertFrom-Json
    if ($result.errors) {
        $msg = ($result.errors | ForEach-Object { $_.message }) -join '; '
        throw "GraphQL error: $msg"
    }
    return $result.data
}

# ── Step 1：查询 Project 信息 ─────────────────────────────────
Write-Host "📋 [1/6] 查询 Project 节点 ID 和现有字段..." -ForegroundColor Yellow

$data = Invoke-GQL @{
    query = 'query($login: String!, $num: Int!) {
  user(login: $login) {
    projectV2(number: $num) {
      id
      fields(first: 30) {
        nodes {
          __typename
          ... on ProjectV2Field             { id name }
          ... on ProjectV2SingleSelectField { id name options { id name } }
          ... on ProjectV2IterationField    { id name configuration { iterations { id title startDate duration } } }
        }
      }
      views(first: 10) { nodes { id name layout } }
    }
  }
}'
    variables = @{ login = $Owner; num = [int]$ProjectNumber }
}

$project = $data.user.projectV2
if (-not $project) {
    throw "❌ 找不到 Project #$ProjectNumber，请确认项目存在且 PAT 含 project scope。"
}

$projectId     = $project.id
$existingFields = @($project.fields.nodes)
$existingViews  = @($project.views.nodes)

Write-Host "   ✅ Project ID: $projectId" -ForegroundColor Green
Write-Host ("   现有字段: " + ($existingFields | ForEach-Object { $_.name } | Where-Object { $_ } | Join-String ', ')) -ForegroundColor DarkGray
Write-Host ("   现有视图: " + ($existingViews  | ForEach-Object { $_.name } | Where-Object { $_ } | Join-String ', ')) -ForegroundColor DarkGray
Write-Host ""

function Get-ExistingField([string]$Name) {
    $existingFields | Where-Object { $_.name -eq $Name } | Select-Object -First 1
}

# ── Step 2：Priority 字段 ──────────────────────────────────────
Write-Host "🎯 [2/6] Priority 字段..." -ForegroundColor Yellow

$priorityField = Get-ExistingField 'Priority'
if ($priorityField) {
    Write-Host "   ⏭️  已存在，跳过" -ForegroundColor DarkYellow
} else {
    $r = Invoke-GQL @{
        query = 'mutation($pid: ID!, $name: String!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {
  createProjectV2Field(input: { projectId: $pid dataType: SINGLE_SELECT name: $name singleSelectOptions: $opts }) {
    projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } }
  }
}'
        variables = @{
            pid  = $projectId
            name = 'Priority'
            opts = @(
                @{ name = '🔴 P0'; color = 'RED';    description = '紧急，立即处理' }
                @{ name = '🟠 P1'; color = 'ORANGE'; description = '高优先级' }
                @{ name = '🟡 P2'; color = 'YELLOW'; description = '中优先级' }
                @{ name = '🟢 P3'; color = 'GREEN';  description = '低优先级' }
            )
        }
    }
    $priorityField = $r.createProjectV2Field.projectV2Field
    Write-Host "   ✅ 已创建（ID: $($priorityField.id)）" -ForegroundColor Green
}

# ── Step 3：Type 字段 ──────────────────────────────────────────
Write-Host "🏷️  [3/6] Category 字段..." -ForegroundColor Yellow

$typeField = Get-ExistingField 'Category'
if ($typeField) {
    Write-Host "   ⏭️  已存在，跳过" -ForegroundColor DarkYellow
} else {
    $r = Invoke-GQL @{
        query = 'mutation($pid: ID!, $name: String!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {
  createProjectV2Field(input: { projectId: $pid dataType: SINGLE_SELECT name: $name singleSelectOptions: $opts }) {
    projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } }
  }
}'
        variables = @{
            pid  = $projectId
            name = 'Category'
            opts = @(
                @{ name = 'task';     color = 'BLUE';   description = '日常任务' }
                @{ name = 'bug';      color = 'RED';    description = 'Bug 报告' }
                @{ name = 'feature';  color = 'GREEN';  description = '新功能' }
                @{ name = 'research'; color = 'GRAY';   description = '调研' }
                @{ name = 'epic';     color = 'PURPLE'; description = '大型任务' }
            )
        }
    }
    $typeField = $r.createProjectV2Field.projectV2Field
    Write-Host "   ✅ 已创建（ID: $($typeField.id)）" -ForegroundColor Green
}

# ── Step 4：Size 字段 ──────────────────────────────────────────
Write-Host "📏 [4/6] Size 字段..." -ForegroundColor Yellow

$sizeField = Get-ExistingField 'Size'
if ($sizeField) {
    Write-Host "   ⏭️  已存在，跳过" -ForegroundColor DarkYellow
} else {
    $r = Invoke-GQL @{
        query = 'mutation($pid: ID!, $name: String!, $opts: [ProjectV2SingleSelectFieldOptionInput!]!) {
  createProjectV2Field(input: { projectId: $pid dataType: SINGLE_SELECT name: $name singleSelectOptions: $opts }) {
    projectV2Field { ... on ProjectV2SingleSelectField { id name options { id name } } }
  }
}'
        variables = @{
            pid  = $projectId
            name = 'Size'
            opts = @(
                @{ name = 'XS'; color = 'GREEN';  description = '< 1 小时' }
                @{ name = 'S';  color = 'BLUE';   description = '1-4 小时' }
                @{ name = 'M';  color = 'YELLOW'; description = '半天到 1 天' }
                @{ name = 'L';  color = 'ORANGE'; description = '2-3 天' }
                @{ name = 'XL'; color = 'RED';    description = '> 3 天，考虑拆分' }
            )
        }
    }
    $sizeField = $r.createProjectV2Field.projectV2Field
    Write-Host "   ✅ 已创建（ID: $($sizeField.id)）" -ForegroundColor Green
}

# ── Step 5：Sprint 迭代字段 ────────────────────────────────────
Write-Host "🏃 [5/6] Sprint 迭代字段..." -ForegroundColor Yellow

$sprintField = Get-ExistingField 'Sprint'
$sprintIsNew = $false
if ($sprintField) {
    Write-Host "   ⏭️  已存在，跳过" -ForegroundColor DarkYellow
} else {
    $r = Invoke-GQL @{
        query = 'mutation($pid: ID!) {
  createProjectV2Field(input: { projectId: $pid dataType: ITERATION name: "Sprint" }) {
    projectV2Field { ... on ProjectV2IterationField { id name } }
  }
}'
        variables = @{ pid = $projectId }
    }
    $sprintField = $r.createProjectV2Field.projectV2Field
    $sprintIsNew = $true
    Write-Host "   ✅ 已创建（ID: $($sprintField.id)）" -ForegroundColor Green
}

# 确保至少有 Sprint 1
$existingIter = if ($sprintIsNew) { @() } else { @($sprintField.configuration.iterations) }
if ($existingIter.Count -eq 0) {
    $today  = Get-Date
    $dow    = [int]$today.DayOfWeek                              # 0=Sunday
    $back   = if ($dow -eq 0) { 6 } elseif ($dow -eq 1) { 0 } else { $dow - 1 }
    $monday = $today.AddDays(-$back).ToString('yyyy-MM-dd')
    $gql = @"
mutation(`$fid: ID!) {
  updateProjectV2Field(input: {
    fieldId: `$fid
    iterationConfiguration: {
      startDate: "$monday"
      duration: 7
      iterations: [{ startDate: "$monday" title: "Sprint 1" duration: 7 }]
    }
  }) { projectV2Field { ... on ProjectV2IterationField { id } } }
}
"@
    Invoke-GQL @{ query = $gql; variables = @{ fid = $sprintField.id } } | Out-Null
    Write-Host "   ✅ Sprint 1 已创建（$monday 开始，1 周）" -ForegroundColor Green
} else {
    Write-Host ("   ✅ 已有 {0} 个迭代" -f $existingIter.Count) -ForegroundColor DarkGray
}

# ── Step 6：创建视图 ───────────────────────────────────────────
Write-Host "👁️  [6/6] 创建视图..." -ForegroundColor Yellow

function New-ProjectView([string]$Name, [string]$Layout) {
    $r = Invoke-GQL @{
        query = 'mutation($pid: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {
  createProjectV2View(input: { projectId: $pid name: $name layout: $layout }) {
    projectV2View { id name layout }
  }
}'
        variables = @{ pid = $projectId; name = $Name; layout = $Layout }
    }
    return $r.createProjectV2View.projectV2View
}

$tableView = $existingViews | Where-Object { $_.name -eq 'Table' } | Select-Object -First 1
if ($tableView) {
    Write-Host "   ⏭️  Table 视图已存在，跳过" -ForegroundColor DarkYellow
} else {
    $tableView = New-ProjectView -Name 'Table' -Layout 'TABLE_LAYOUT'
    Write-Host "   ✅ Table 视图已创建" -ForegroundColor Green
}

$sprintView = $existingViews | Where-Object { $_.name -eq 'Sprint' } | Select-Object -First 1
if ($sprintView) {
    Write-Host "   ⏭️  Sprint 视图已存在，跳过" -ForegroundColor DarkYellow
} else {
    $sprintView = New-ProjectView -Name 'Sprint' -Layout 'BOARD_LAYOUT'
    Write-Host "   ✅ Sprint 视图已创建" -ForegroundColor Green
}

$byProjectView = $existingViews | Where-Object { $_.name -eq 'By Project' } | Select-Object -First 1
if ($byProjectView) {
    Write-Host "   ⏭️  By Project 视图已存在，跳过" -ForegroundColor DarkYellow
} else {
    $byProjectView = New-ProjectView -Name 'By Project' -Layout 'TABLE_LAYOUT'
    Write-Host "   ✅ By Project 视图已创建" -ForegroundColor Green
}

# ── 完成摘要 ──────────────────────────────────────────────────
Write-Host ""
Write-Host "🎉 看板配置完成！" -ForegroundColor Cyan
Write-Host ""
Write-Host "✅ 已自动完成：" -ForegroundColor Green
Write-Host "   • Priority 字段（🔴 P0 / 🟠 P1 / 🟡 P2 / 🟢 P3）"
Write-Host "   • Category 字段（task / bug / feature / research / epic）"
Write-Host "   • Size 字段（XS / S / M / L / XL）"
Write-Host "   • Sprint 迭代字段"
Write-Host "   • Table 视图（表格）"
Write-Host "   • Sprint 视图（按迭代分组的看板）"
Write-Host "   • By Project 视图（按仓库分组的表格）"
Write-Host ""
Write-Host "📌 需要在 GitHub 网页端手动完成（约 3 分钟）：" -ForegroundColor Yellow
Write-Host ""
Write-Host "   1. 添加 Status 列：" -ForegroundColor White
Write-Host "      打开 Project → 右上角 ⚙️ Settings → Manage columns"
Write-Host "      → + Add column → 'Blocked'（红色）"
Write-Host "      → + Add column → 'In Review'（蓝色）"
Write-Host ""
Write-Host "   2. 配置 Sprint 视图：" -ForegroundColor White
Write-Host "      切换到 Sprint 视图 → Group by → 选择 Sprint"
Write-Host ""
Write-Host "   3. 配置 Table 视图排序：" -ForegroundColor White
Write-Host "      切换到 Table 视图 → Sort → 选择 Priority（升序）"
Write-Host ""
Write-Host "   4. 配置 By Project 视图分组：" -ForegroundColor White
Write-Host "      切换到 By Project 视图 → Group by → 选择 Repository"
Write-Host ""
Write-Host "   5. 配置 Sprint 周期（可选）：" -ForegroundColor White
Write-Host "      Project Settings → Sprint field → 设置 1 周周期"

if ($cleanupToken) { Remove-Item env:GH_TOKEN -ErrorAction SilentlyContinue }
