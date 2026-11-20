<#
.SYNOPSIS
  在终端安装 TaskSteward（DN009 §4.1、CC009 §2、§8）。

.DESCRIPTION
  在产品检出的根目录运行（Windows PowerShell 5.1 即可）：

      & "$env:LOCALAPPDATA\TaskSteward\task-steward\setup.ps1"

  先探测 Python 3.10+、git 与 Claude Code 命令行（与会话入口共用 plugin\setup\prereq.ps1），缺少
  Python 或 git 时问你是否用 winget 安装；然后用找到的解释器运行
  tsw setup install --interactive：逐项提问（数据目录位置必须由你给出）、显示计划并请你确认，
  再执行全部步骤并核对。做完后请重启 Claude app，在数据目录中打开主会话。
#>
param(
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'plugin\setup\prereq.ps1')

$found = Get-TswPrereqs
foreach ($name in 'python', 'git') {
    if ($found[$name]) { continue }
    $label = @{ python = 'Python 3.10 以上'; git = 'git' }[$name]
    if (-not $found.winget) {
        Write-Host "没有找到 $label，也没有 winget。请先安装 $label，再重新运行本脚本。"
        exit 1
    }
    $reply = Read-Host "没有找到 $label。现在用 winget 安装吗？[是 / 否]"
    if ($reply -notmatch '^(y|yes|是|好)$') {
        Write-Host "没有安装 $label。请自己安装后重新运行本脚本。"
        exit 1
    }
    Install-TswPrereq $name | Out-Null
    $found = Get-TswPrereqs
    if (-not $found[$name]) {
        Write-Host "安装后仍然找不到 $label；请打开一个新的终端再运行本脚本。"
        exit 1
    }
}
foreach ($note in $found.notes) { Write-Host $note }
if (-not $found.claude) { exit 1 }

# A newly installed git is not on this process's PATH yet; tsw runs git by name.
$gitDir = Split-Path -Parent $found.git.path
if (($env:Path -split ';') -notcontains $gitDir) { $env:Path = "$env:Path;$gitDir" }

$launcher = Join-Path $PSScriptRoot 'plugin\bin\tsw_launch.py'
$arguments = @('-X', 'utf8', $launcher, 'setup', 'install', '--interactive',
    '--python', $found.python.path, '--claude', $found.claude.path)
if ($NoBrowser) { $arguments += '--no-browser' }
& $found.python.path @arguments
exit $LASTEXITCODE
