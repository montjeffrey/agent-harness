<#
.SYNOPSIS
    Once-per-machine setup for the agent harness.

.DESCRIPTION
    Installs pipx if missing, then installs this checkout as an isolated
    pipx application so the `agent-harness-*` commands are available from
    every project directory.

    Installs from THIS CHECKOUT, not from PyPI. The name `agent-harness` on
    PyPI belongs to an unrelated project, so `pipx install agent-harness`
    would fetch a stranger's package. Until this project is published under
    a name it owns, the local path is the only correct source.

    Idempotent: safe to re-run after pulling. `pipx install` refuses an
    already-installed package, so `--force` is used to reinstall in place.

    Per-project setup is a separate command -- see `agent-harness-enroll`.

.PARAMETER NoSemantic
    Skip the `semantic` extra (chromadb + onnxruntime, a few hundred MB).
    Everything still works; context selection stays deterministic.

.EXAMPLE
    .\bootstrap.ps1
    .\bootstrap.ps1 -NoSemantic
#>
[CmdletBinding()]
param(
    [switch]$NoSemantic
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
$MinPython = [version]'3.11'

function Write-Step { param([string]$Text) Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "    OK  $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "    !   $Text" -ForegroundColor Yellow }

# --- 1. Python ------------------------------------------------------------
Write-Step "Checking Python"
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    throw "python is not on PATH. Install Python $MinPython or newer from python.org, then re-run."
}
$versionText = (& $python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if ([version]$versionText -lt $MinPython) {
    throw "Python $versionText found, but this project requires $MinPython or newer."
}
Write-Ok "Python $versionText at $python"

# --- 2. pipx --------------------------------------------------------------
# `python -m pipx` is used throughout rather than the `pipx` shim: ensurepath
# edits the user PATH, but the CURRENT shell keeps its old copy, so a freshly
# installed pipx.exe is not callable until a new terminal opens. Going through
# the module makes this script work in the same shell that just installed it.
Write-Step "Checking pipx"
& $python -m pipx --version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warn "pipx not found -- installing for the current user"
    & $python -m pip install --user --quiet pipx
    if ($LASTEXITCODE -ne 0) { throw "failed to install pipx" }
    & $python -m pipx ensurepath
    Write-Warn "pipx added its script directory to your user PATH."
    Write-Warn "Open a NEW terminal before using the agent-harness-* commands."
}
$pipxVersion = (& $python -m pipx --version).Trim()
Write-Ok "pipx $pipxVersion"

# --- 3. the harness -------------------------------------------------------
# The distribution used to be called `agent-harness`. pipx keys venvs by
# distribution name, so installing under the new name leaves the old venv --
# and its shims, which shadow the new ones on PATH -- in place.
$installed = (& $python -m pipx list --short 2>$null) -join "`n"
if ($installed -match '(?m)^agent-harness\s') {
    Write-Step 'Removing the previous agent-harness install'
    & $python -m pipx uninstall agent-harness 2>$null | Out-Null
    Write-Ok "old install removed"
}

# `mcp` and `graph` are not optional here even though pyproject marks them as
# extras. Step 5 registers the MCP server for every project on this machine,
# and a registered server whose SDK is missing fails at connect time with an
# import error rather than degrading; `graph` (graphifyy) is what makes
# `agent-harness-refresh` able to build a graph at all. Both are small. The
# extras exist so a library consumer can skip them; a bootstrapped machine
# always wants them. Only `semantic` is worth opting out of -- it is the one
# that costs hundreds of megabytes.
$extras = @('mcp', 'graph')
if (-not $NoSemantic) { $extras += 'semantic' }
$spec = '{0}[{1}]' -f $RepoRoot, ($extras -join ',')

Write-Step "Installing the harness from $RepoRoot"
if ($NoSemantic) {
    Write-Warn "skipping the semantic extra; context selection will be deterministic"
} else {
    Write-Host "    (chromadb + onnxruntime; this can take a few minutes)"
}

& $python -m pipx install --force $spec
if ($LASTEXITCODE -ne 0) {
    throw "pipx install failed. Re-run with -NoSemantic to skip chromadb, or read the error above."
}
Write-Ok "harness installed"

# --- 4. verify ------------------------------------------------------------
# Resolve the installed console script by path rather than by name: this
# shell's PATH may predate ensurepath, so `agent-harness-memory` can be
# absent even though the install succeeded. Ask pipx where its venvs live
# rather than hardcoding a path -- PIPX_LOCAL_VENVS is platform- and
# install-method-dependent (on this machine it is under AppData, not
# ~/.local, which a hardcoded guess got wrong).
Write-Step "Verifying"
$venvRoot = (& $python -m pipx environment --value PIPX_LOCAL_VENVS 2>$null)
if (-not $venvRoot) { $venvRoot = Join-Path $env:USERPROFILE ".local\pipx\venvs" }
$venvBin = Join-Path $venvRoot "agent-harness-kit\Scripts"
$memoryCmd = Join-Path $venvBin "agent-harness-memory.exe"
if (-not (Test-Path $memoryCmd)) {
    $memoryCmd = (Get-Command agent-harness-memory -ErrorAction SilentlyContinue).Source
}
if (-not $memoryCmd) {
    Write-Warn "could not locate agent-harness-memory. Open a new terminal and run:"
    Write-Warn "  agent-harness-memory --help"
} else {
    & $memoryCmd --help *> $null
    if ($LASTEXITCODE -eq 0) { Write-Ok "agent-harness-memory responds" }
    else { Write-Warn "agent-harness-memory exited non-zero -- check the install above" }
}

# --- 5. user-level hook + MCP registration --------------------------------
# Registered once here, in the USER settings, rather than per project. That
# is what lets `agent-harness-enroll` stay memory-only: the SessionStart
# context hook and the MCP server apply to every project automatically, and
# there is one copy of the configuration instead of N drifting copies.
Write-Step "Registering the SessionStart hook and MCP server (user scope)"
$venvPython = Join-Path $venvBin "python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Warn "could not find the pipx venv python; skipping hook and MCP registration"
} else {
    # The merge itself lives in `harness.bootstrap_register`, not here.
    #
    # It used to be inline PowerShell built on `ConvertFrom-Json -AsHashtable`,
    # which is PowerShell 6+ only. On stock Windows -- 5.1, no pwsh -- that
    # threw, and with $ErrorActionPreference = 'Stop' it killed the bootstrap
    # right after the pipx install had succeeded: every command worked, no
    # hook was ever registered, and nothing said so. One Python implementation
    # behaves the same under 5.1, 7, and POSIX, and reuses the JSON validation
    # and atomic writes the project-scope installer already uses.
    #
    # Registered at USER scope, so it covers every project:
    #   SessionStart -> memory context packet (and optional auto-enrollment)
    #   PreToolUse   -> block direct reads of a large graph.json
    #   MCP server   -> agent-harness tools in ~/.claude.json
    # This is why `agent-harness-enroll` never writes to a project's settings.
    & $venvPython -m harness.bootstrap_register --harness-root $RepoRoot --python $venvPython
    if ($LASTEXITCODE -ne 0) {
        throw "hook/MCP registration failed -- see the error above. The harness itself is installed; re-run after resolving it."
    }
    Write-Ok "hooks and MCP server registered"
    Write-Warn "restart Claude Code for the new hooks and MCP server to load"

    Write-Host "    To auto-enroll projects under your workspace, set these once:"
    Write-Host '      setx AGENT_HARNESS_AUTO_ENROLL 1'
    Write-Host ('      setx AGENT_HARNESS_WORKSPACE "{0}"' -f (Split-Path $RepoRoot -Parent))
}

Write-Host "`nMachine setup complete." -ForegroundColor Green
Write-Host @"

The SessionStart hook and MCP server are registered once, above, at user
scope -- they apply to every project you open, so per-project setup only has
to deal with that project's own memory:

    agent-harness-enroll "C:\path\to\your\project"

That creates .agent-harness/ (memory), builds the code graph if graphify is
available, and builds the semantic index. It writes no instruction files.
Idempotent -- re-run it whenever you like.

With AGENT_HARNESS_AUTO_ENROLL and AGENT_HARNESS_WORKSPACE set, projects
under your workspace get their memory created automatically the first time
you open a session in them, and you never run enroll by hand at all.

If the commands are not found, open a NEW terminal first (PATH was updated).
"@
