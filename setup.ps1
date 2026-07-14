# agent-harness quick setup (Windows PowerShell / pwsh)
#
#   .\setup.ps1 <project-dir> [-NoHooks] [-Check] [-Agents claude,codex,gemini]
#
# Runs the preflight doctor first and reports anything missing; installs only
# if no check FAILs. -Check reports and stops. -NoHooks skips the Claude Code
# PreToolUse hook merge (hooks are ON by default in quick setup).

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$ProjectDir,
    [switch]$NoHooks,
    [switch]$Check,
    [string]$Agents = ''
)

$ErrorActionPreference = 'Stop'
$harnessRoot = $PSScriptRoot

# Find a Python 3 interpreter.
$python = $null
foreach ($candidate in @('python', 'python3', 'py')) {
    $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($cmd) {
        & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,) else 1)' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
}
if (-not $python) {
    Write-Host '[FAIL] python: no Python 3 interpreter found on PATH (need >= 3.10)'
    Write-Host 'Install Python from https://www.python.org/downloads/ and rerun.'
    exit 1
}

$installArgs = @((Join-Path $harnessRoot 'install.py'), $ProjectDir)
if ($Agents) { $installArgs += @('--agents', $Agents) }

if ($Check) {
    & $python @($installArgs + '--check')
    exit $LASTEXITCODE
}

if (-not $NoHooks) { $installArgs += '--hooks' }

# install.py runs the doctor preflight itself and aborts on any FAIL.
& $python @installArgs
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host ''
Write-Host 'Next steps:'
Write-Host "  1. Build the code graph:  $python `"$harnessRoot\harness\graph_refresh.py`" `"$ProjectDir`" --full"
Write-Host "  2. Query it:              $python `"$harnessRoot\harness\graph_query.py`" find <term>"
Write-Host '  3. Re-run anytime -- the install is idempotent.'
