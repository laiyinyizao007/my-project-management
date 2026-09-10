# scripts/common.ps1
# 共享工具函数，供其他脚本 dot-source：. "$PSScriptRoot/common.ps1"

function Get-ProjectToken {
    param([string]$ProjectToken = '')

    $cleanup = $false
    if (-not $ProjectToken) {
        if ($env:GH_TOKEN) {
            $ProjectToken = $env:GH_TOKEN
            Write-Host "🔑 使用环境变量 GH_TOKEN" -ForegroundColor DarkGray
        } else {
            Write-Host "🔑 请输入 PAT（需要 repo + project scope，输入时不可见）：" -ForegroundColor Cyan -NoNewline
            $secure = Read-Host -AsSecureString
            $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
            try {
                $ProjectToken = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
            } finally {
                [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
            }
            Write-Host ""
            $env:GH_TOKEN = $ProjectToken
            $cleanup = $true
        }
    }

    return @{ Token = $ProjectToken; Cleanup = $cleanup }
}

function Remove-ProjectToken {
    param([bool]$Cleanup)
    if ($Cleanup) {
        Remove-Item env:GH_TOKEN -ErrorAction SilentlyContinue
    }
}

function Get-CurrentRepo {
    $owner = gh api user --jq '.login'
    $name  = gh repo view --json name --jq '.name'
    return @{ Owner = $owner; Name = $name; Full = "$owner/$name" }
}
