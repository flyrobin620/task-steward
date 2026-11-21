<#
.SYNOPSIS
  在终端安装 TaskSteward（DN009 §4.1、§16，CC009 §2、§8、§11）。

.DESCRIPTION
  在产品检出的根目录运行（Windows PowerShell 5.1 即可）：

      & "$env:LOCALAPPDATA\TaskSteward\task-steward\setup.ps1"

  先探测 Python 3.10+、git 与 Claude Code 命令行（与会话入口共用 plugin\setup\prereq.ps1），缺少
  Python 或 git 时请你用方向键选择是否用 winget 安装；然后用找到的解释器运行
  tsw setup install --interactive：逐项提问（选择题用方向键，数据目录位置必须由你给出）、显示计划
  并请你确认，再执行全部步骤并核对。做完后请重启 Claude app，在数据目录中打开主会话。
  -Plain 时全部用逐行文字问答、不用颜色；输入或输出被重定向时自动如此。
#>
param(
    [switch]$NoBrowser,
    [switch]$Plain
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'plugin\setup\prereq.ps1')

# The arrow-key choice needs a console on both sides and a terminal that is not "dumb"; colour
# also needs no NO_COLOR (DN009 §16.4, CC009 §11). Otherwise lines, as before.
$script:Keys = -not $Plain -and -not [Console]::IsInputRedirected -and
    -not [Console]::IsOutputRedirected -and $Host.Name -eq 'ConsoleHost' -and $env:TERM -ne 'dumb'
$script:Color = $script:Keys -and -not $env:NO_COLOR
$script:Marks = @{ ok = [char]0x221A; bad = [char]0x00D7; warn = '!'; cursor = [char]0x25BA }

function Write-TswText([string]$Text, [string]$Color, [switch]$NoNewline) {
    if ($script:Color -and $Color) {
        Write-Host $Text -ForegroundColor $Color -NoNewline:$NoNewline
    } else {
        Write-Host $Text -NoNewline:$NoNewline
    }
}

function Write-TswMark([string]$Kind, [string]$Text) {
    if ($script:Keys) {
        $color = @{ ok = 'Green'; bad = 'Red'; warn = 'Yellow' }[$Kind]
        Write-TswText " $($script:Marks[$Kind]) " $color -NoNewline
    }
    Write-Host $Text
}

function Select-TswYesNo([string]$Question) {
    # Yes or no with the arrow keys (DN009 §16.3); Read-Host when there is no console. The
    # redraw moves up a fixed number of rows: on a console narrower than 80 columns a line
    # could wrap, so that gets Read-Host as well.
    if (-not $script:Keys -or [Console]::WindowWidth -lt 80) {
        $reply = Read-Host "$Question [是 / 否]"
        return ($reply -match '^(y|yes|是|好)$')
    }
    $options = @('是', '否')
    $index = 0
    Write-Host ''
    Write-TswText " $Question" 'White'
    $first = $true
    [Console]::CursorVisible = $false
    try {
        while ($true) {
            if (-not $first) { [Console]::SetCursorPosition(0, [Console]::CursorTop - 3) }
            $first = $false
            for ($i = 0; $i -lt $options.Count; $i++) {
                if ($i -eq $index) {
                    Write-TswText "   $($script:Marks.cursor) $($options[$i])  " 'Cyan'
                } else {
                    Write-Host "     $($options[$i])  "
                }
            }
            Write-TswText '   ↑↓ 选择  回车 确认  y / n 直接选  Ctrl+C 取消' 'DarkGray'
            $key = [Console]::ReadKey($true)
            $char = "$($key.KeyChar)".ToLowerInvariant()
            if ($char -eq 'y') { $index = 0; break }
            if ($char -eq 'n') { $index = 1; break }
            if ($key.Key -eq 'Enter') { break }
            if ($key.Key -in 'UpArrow', 'DownArrow', 'LeftArrow', 'RightArrow') {
                $index = 1 - $index
            }
        }
    } finally {
        [Console]::CursorVisible = $true
    }
    # Collapse the question to one line, as the questions after it do.
    $top = [Console]::CursorTop - 4
    [Console]::SetCursorPosition(0, $top)
    $blank = ' ' * ([Console]::WindowWidth - 1)
    for ($i = 0; $i -lt 4; $i++) { Write-Host $blank }
    [Console]::SetCursorPosition(0, $top)
    Write-TswMark 'ok' "$Question$($options[$index])"
    return ($index -eq 0)
}

$found = Get-TswPrereqs
foreach ($name in 'python', 'git') {
    if ($found[$name]) { continue }
    $label = @{ python = 'Python 3.10 以上'; git = 'git' }[$name]
    if (-not $found.winget) {
        Write-TswMark 'bad' "没有找到 $label，也没有 winget。请先安装 $label，再重新运行本脚本。"
        exit 1
    }
    if (-not (Select-TswYesNo "没有找到 $label。现在用 winget 安装吗？")) {
        Write-TswMark 'bad' "没有安装 $label。请自己安装后重新运行本脚本。"
        exit 1
    }
    Install-TswPrereq $name | Out-Null
    $found = Get-TswPrereqs
    if (-not $found[$name]) {
        Write-TswMark 'bad' "安装后仍然找不到 $label；请打开一个新的终端再运行本脚本。"
        exit 1
    }
}
foreach ($note in $found.notes) { Write-TswMark 'warn' $note }
if (-not $found.claude) { exit 1 }

# A newly installed git is not on this process's PATH yet; tsw runs git by name.
$gitDir = Split-Path -Parent $found.git.path
if (($env:Path -split ';') -notcontains $gitDir) { $env:Path = "$env:Path;$gitDir" }

$launcher = Join-Path $PSScriptRoot 'plugin\bin\tsw_launch.py'
$arguments = @('-X', 'utf8', $launcher, 'setup', 'install', '--interactive',
    '--python', $found.python.path, '--claude', $found.claude.path)
if ($NoBrowser) { $arguments += '--no-browser' }
if ($Plain) { $arguments += '--plain' }
& $found.python.path @arguments
exit $LASTEXITCODE
