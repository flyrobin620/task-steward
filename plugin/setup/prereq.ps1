<#
.SYNOPSIS
  TaskSteward 安装的前提探测（DN009 §4、CC009 §8）：Python 3.10+、git、Claude Code 命令行。

.DESCRIPTION
  不依赖 Python。两个入口共用这一段代码：
  - 终端入口：仓库根的 setup.ps1 用点号引入本文件（. prereq.ps1），调用其中的函数；
  - 会话入口：setup skill 直接运行本文件，读取输出的一行 JSON。

  直接运行时输出 JSON：python、git、claude（各为 {path, version} 或 null）、winget（是否可用）、
  missing（缺少的项）、installed（本次经 winget 安装的项）、notes。
  -Install python / git：用 winget 安装缺少的项（调用者已经取得用户同意），装好后重新探测；
  新装的解释器不在当前进程的 PATH 中，所以按安装位置查找并给出完整路径。
  环境变量 TSW_WINGET 指向 winget 的替身（测试用）。
#>
param(
    [switch]$Json,
    [ValidateSet('python', 'git')]
    [string[]]$Install = @()
)

$TswMinPython = [Version]'3.10'
$TswWingetIds = @{ python = 'Python.Python.3.12'; git = 'Git.Git' }

function Get-TswPythonVersion([string]$Path) {
    # The interpreter's version, or $null when it does not run (the Microsoft Store stub).
    try {
        $text = & $Path -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $text) { return [Version]($text | Select-Object -First 1).Trim() }
    } catch { }
    return $null
}

function Find-TswPython {
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($name in 'python', 'python3') {
        foreach ($cmd in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $candidates.Add($cmd.Source)
        }
    }
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates.Add(($exe | Select-Object -First 1).Trim()) }
        } catch { }
    }
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        if ($root -and (Test-Path $root)) {
            Get-ChildItem -Path $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending |
                ForEach-Object { $candidates.Add((Join-Path $_.FullName 'python.exe')) }
        }
    }
    foreach ($path in $candidates) {
        if (-not $path -or -not (Test-Path $path)) { continue }
        if ($path -match '\\WindowsApps\\') { continue }  # the Microsoft Store stub
        $version = Get-TswPythonVersion $path
        if ($version -and $version -ge $TswMinPython) {
            return [ordered]@{ path = (Resolve-Path $path).Path; version = $version.ToString() }
        }
    }
    return $null
}

function Find-TswGit {
    $cmd = Get-Command git -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $path = if ($cmd) { $cmd.Source } else { $null }
    if (-not $path) {
        foreach ($root in @($env:ProgramFiles, "$env:LOCALAPPDATA\Programs")) {
            if ($root -and (Test-Path (Join-Path $root 'Git\cmd\git.exe'))) {
                $path = Join-Path $root 'Git\cmd\git.exe'
                break
            }
        }
    }
    if (-not $path) { return $null }
    $version = (& $path --version 2>$null | Select-Object -First 1)
    return [ordered]@{ path = $path; version = "$version".Trim() }
}

function Find-TswClaude {
    # On PATH first, then where the Claude desktop app installs its command line (CC009 §3).
    $cmd = Get-Command claude -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $path = if ($cmd) { $cmd.Source } else { $null }
    if (-not $path) {
        $app = Join-Path "$env:LOCALAPPDATA" 'Programs\ClaudeCode\claude.exe'
        if (Test-Path $app) { $path = $app }
    }
    if (-not $path) { return $null }
    return [ordered]@{ path = $path; version = $null }
}

function Get-TswWinget {
    if ($env:TSW_WINGET) { return $env:TSW_WINGET }
    $cmd = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

function Get-TswPrereqs {
    $found = [ordered]@{
        python = Find-TswPython
        git    = Find-TswGit
        claude = Find-TswClaude
        winget = [bool](Get-TswWinget)
    }
    $found.missing = @('python', 'git', 'claude' | Where-Object { -not $found[$_] })
    $notes = New-Object System.Collections.Generic.List[string]
    if (-not $found.python) {
        $stub = Get-Command python -All -CommandType Application -ErrorAction SilentlyContinue |
            Where-Object { $_.Source -match '\\WindowsApps\\' }
        if ($stub) {
            $notes.Add('PATH 中的 python 是 Microsoft Store 的占位程序：装好 Python 后在“设置 > 应用 > 高级应用设置 > 应用执行别名”中关闭 python.exe 与 python3.exe')
        }
    }
    if (-not $found.claude) {
        $notes.Add('找不到 Claude Code：先安装 Claude 桌面 app（含 claude 命令行）或 Claude Code CLI，再重新运行安装')
    }
    $found.notes = @($notes)
    return $found
}

function Install-TswPrereq([string]$Name) {
    # Install one missing item with winget; the caller has the user's consent.
    $winget = Get-TswWinget
    if (-not $winget) { throw "没有 winget，不能自动安装 $Name" }
    $id = $TswWingetIds[$Name]
    if ($winget.EndsWith('.py')) {
        & python $winget install -e --id $id --accept-package-agreements --accept-source-agreements | Out-Host
    } else {
        & $winget install -e --id $id --accept-package-agreements --accept-source-agreements | Out-Host
    }
    return $LASTEXITCODE
}

if ($MyInvocation.InvocationName -ne '.') {
    $installed = New-Object System.Collections.Generic.List[string]
    foreach ($name in $Install) {
        $before = Get-TswPrereqs
        if (-not $before[$name]) {
            $code = Install-TswPrereq $name
            if ($code -eq 0) { $installed.Add($name) }
        }
    }
    $result = Get-TswPrereqs
    $result.installed = @($installed)
    $result | ConvertTo-Json -Compress -Depth 4
}
