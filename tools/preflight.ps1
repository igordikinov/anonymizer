param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = '',
    [switch]$RequireClean,
    [switch]$RequireExe,
    [switch]$Json
)

# Read-only. No installation, venv/Beads init, hooks, config or benchmark.
$ErrorActionPreference = 'Stop'
$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string]$Name, [string]$Status, [string]$Detail) {
    $checks.Add([pscustomobject]@{ name = $Name; status = $Status; detail = $Detail })
}

try {
    $repoRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
    if (-not (Test-Path -LiteralPath $repoRoot -PathType Container)) {
        Add-Check 'project' 'FAIL' 'Project directory is missing.'
    } else {
        Add-Check 'project' 'PASS' $repoRoot
        $gitCommand = Get-Command git -ErrorAction SilentlyContinue
        if (-not $gitCommand) {
            Add-Check 'git' 'FAIL' 'git is not on PATH.'
        } else {
            $top = & $gitCommand.Source -C $repoRoot rev-parse --show-toplevel 2>$null
            $gitResult = $LASTEXITCODE
            if ($gitResult -ne 0 -or -not $top -or [System.IO.Path]::GetFullPath($top) -ne $repoRoot) {
                Add-Check 'git' 'FAIL' 'The selected directory is not the repository root.'
            } else {
                $head = & $gitCommand.Source -C $repoRoot rev-parse HEAD
                if ($LASTEXITCODE -ne 0) { throw 'Git head check failed.' }
                Add-Check 'git_head' 'PASS' $head
                $state = @(& $gitCommand.Source --no-optional-locks -C $repoRoot status --porcelain=v1)
                if ($LASTEXITCODE -ne 0) { throw 'Git status check failed.' }
                if ($state.Count) {
                    Add-Check 'git_state' $(if ($RequireClean) { 'FAIL' } else { 'WARN' }) "Working tree has $($state.Count) changed/untracked entries; inspect before editing."
                } else {
                    Add-Check 'git_state' 'PASS' 'Working tree is clean.'
                }
            }
        }

        $required = @(
            'AGENTS.md', 'CLAUDE.md', 'README.md', 'pyproject.toml',
            'docs/handoff-glm.md', 'docs/start-glm.md', 'docs/input-contract.md',
            'docs/tasks/GLM-001-composite-fk.md', 'docs/tasks/GLM-002-key-groups.md',
            '.agents/roles/orchestrator.md', '.agents/roles/planner.md', '.agents/roles/worker.md',
            '.agents/roles/code-reviewer.md', '.agents/roles/verifier.md',
            'src/anonymizer/pipeline.py', 'tools/build_windows.ps1', 'tools/smoke_exe.py'
        )
        $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $repoRoot $_) -PathType Leaf) })
        Add-Check 'files' $(if ($missing.Count) { 'FAIL' } else { 'PASS' }) $(if ($missing.Count) { 'Missing: ' + ($missing -join ', ') } else { 'Instructions, roles, source and tools are present.' })

        $snapshotFile = Join-Path $repoRoot 'src/anonymizer/resources/model.json'
        if (-not (Test-Path -LiteralPath $snapshotFile -PathType Leaf)) {
            Add-Check 'model' 'FAIL' 'Bundled model snapshot is missing.'
        } else {
            $snapshot = Get-Content -LiteralPath $snapshotFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $tableCount = @($snapshot.tables.psobject.Properties).Count
            if ($tableCount -eq 0 -or $tableCount -ne $snapshot.meta.table_count -or $snapshot.meta.content_sha256 -notmatch '^[0-9a-f]{64}$') {
                Add-Check 'model' 'FAIL' 'Invalid model metadata/table count.'
            } else {
                Add-Check 'model' 'PASS' "Version $($snapshot.meta.version), $tableCount tables, content SHA256 $($snapshot.meta.content_sha256)."
            }
        }

        $taskPython = if ($PythonPath) { [System.IO.Path]::GetFullPath($PythonPath) } else { Join-Path $repoRoot '.venv/Scripts/python.exe' }
        if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
            Add-Check 'python' 'FAIL' 'Prepared interpreter is missing; see README bootstrap. No environment was created.'
        } else {
            $probeCode = @'
import importlib, importlib.metadata, json, sys
result = {"version": ".".join(map(str, sys.version_info[:3])), "supported": sys.version_info >= (3,11), "modules": {}, "missing": []}
for module in ("tkinter", "sqlite3", "pytest", "PyInstaller", "psutil", "pglast"):
    try:
        importlib.import_module(module)
        result["modules"][module.lower()] = "available"
    except ImportError:
        result["missing"].append(module)
for package in ("pytest", "pyinstaller", "psutil", "pglast"):
    try:
        result["modules"][package] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
print(json.dumps(result))
'@
            $probeText = & $taskPython -B -c $probeCode
            $probeExit = $LASTEXITCODE
            if ($probeExit -ne 0) {
                Add-Check 'python' 'FAIL' 'Interpreter probe failed.'
            } else {
                $probe = $probeText | ConvertFrom-Json
                Add-Check 'python' $(if ($probe.supported) { 'PASS' } else { 'FAIL' }) "Python $($probe.version)."
                Add-Check 'modules' $(if (@($probe.missing).Count) { 'FAIL' } else { 'PASS' }) $(if (@($probe.missing).Count) { 'Missing: ' + ($probe.missing -join ', ') } else { ($probe.modules | ConvertTo-Json -Compress) })
            }
        }

        $exeFile = Join-Path $repoRoot 'dist/SNP-Anonymizer.exe'
        if (Test-Path -LiteralPath $exeFile -PathType Leaf) {
            $exe = Get-Item -LiteralPath $exeFile
            Add-Check 'exe' 'PASS' "Present, $($exe.Length) bytes. Presence is not a smoke test."
        } else {
            Add-Check 'exe' $(if ($RequireExe) { 'FAIL' } else { 'SKIP' }) 'EXE is not tracked; build on Windows when required.'
        }

        if (Test-Path -LiteralPath (Join-Path $repoRoot '.beads')) {
            Add-Check 'tracker' 'WARN' 'Beads directory now exists; read current project rules. No init/prime/sync was run.'
        } else {
            Add-Check 'tracker' 'SKIP' 'not_configured; handoff cards are not Beads issue history.'
        }
        $bdCommand = Get-Command bd -ErrorAction SilentlyContinue
        Add-Check 'bd_cli' $(if ($bdCommand) { 'PASS' } else { 'SKIP' }) $(if ($bdCommand) { 'CLI available; optional until a tracker is explicitly configured.' } else { 'Optional CLI absent; do not install/init automatically.' })
    }
} catch {
    Add-Check 'preflight' 'FAIL' "Read-only probe failed at script line $($_.InvocationInfo.ScriptLineNumber) ($($_.Exception.GetType().Name)). No bootstrap or configuration mutation was attempted."
}

$failed = @($checks | Where-Object { $_.status -eq 'FAIL' }).Count -gt 0
$result = [pscustomobject]@{ passed = (-not $failed); read_only = $true; checks = @($checks.ToArray()) }
if ($Json) { $result | ConvertTo-Json -Depth 6 } else { $checks | Format-Table -AutoSize -Wrap }
if ($failed) { exit 1 } else { exit 0 }
