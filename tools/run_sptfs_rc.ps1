param(
    [Parameter(Mandatory = $true)][string]$StageRoot,
    [Parameter(Mandatory = $true)][string]$SptfsRoot,
    [switch]$HelpOnly,
    [switch]$RequireAfS
)
$ErrorActionPreference = 'Stop'
$stage = (Resolve-Path -LiteralPath $StageRoot).Path
$source = (Resolve-Path -LiteralPath $SptfsRoot).Path
$python = Join-Path $stage 'venv\Scripts\python.exe'
$manifest = Get-Content -LiteralPath (Join-Path $stage 'stage-manifest.json') -Raw | ConvertFrom-Json
if ($manifest.sdk_version -ne '0.1.0b4') { throw 'Unexpected staged SDK version' }
foreach ($entry in $manifest.binaries.PSObject.Properties) {
    $binary = Join-Path (Join-Path $stage 'native') $entry.Name
    if ((Get-FileHash -LiteralPath $binary -Algorithm SHA256).Hash.ToLowerInvariant() -ne $entry.Value) {
        throw "Staged binary hash mismatch: $($entry.Name)"
    }
}
$keys = @('CRSDKPY_HOST','CRSDKPY_BRIDGE','CRSDKPY_SAVE_DIR','PYTHONPATH','PYTHONHOME','PYTHONNOUSERSITE')
$prior = @{}
foreach ($key in $keys) { $prior[$key] = [Environment]::GetEnvironmentVariable($key, 'Process') }
try {
    $env:CRSDKPY_HOST = Join-Path $stage 'native\crsdkpy_host.exe'
    $env:CRSDKPY_BRIDGE = Join-Path $stage 'native\crsdkpy_bridge.dll'
    $env:CRSDKPY_SAVE_DIR = Join-Path $stage 'host-save'
    $env:PYTHONNOUSERSITE = '1'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
    & $python -c "import crsdkpy,sys; from pathlib import Path; assert crsdkpy.__version__=='0.1.0b4'; assert Path(crsdkpy.__file__).is_relative_to(Path(sys.prefix)); print('RC SDK:',crsdkpy.__version__,crsdkpy.__file__)"
    if ($LASTEXITCODE -ne 0) { throw 'Staged interpreter/package verification failed' }
    Push-Location -LiteralPath $source
    try {
        if ($HelpOnly) {
            & $python controller.py --help
        } elseif ($RequireAfS) {
            & $python (Join-Path $PSScriptRoot 'run_sptfs_afs_gate.py') --sptfs-root $source --closure-dir (Join-Path $stage 'evidence') --camera sony --sony-autofocus auto --sony-afs-dwell-ms 35 --sony-capture-trace (Join-Path $stage 'evidence\captures') --data-root (Join-Path $stage 'data')
        } else {
            # Startup does not request a photograph. Operator scan commands
            # require separate authorization; nothing is piped to the REPL.
            & $python controller.py --camera sony --sony-autofocus auto --sony-afs-dwell-ms 35 --sony-capture-trace (Join-Path $stage 'evidence\captures') --data-root (Join-Path $stage 'data')
        }
        if ($LASTEXITCODE -ne 0) { throw 'SPTFS test process failed' }
    } finally { Pop-Location }
} finally {
    foreach ($key in $keys) { [Environment]::SetEnvironmentVariable($key, $prior[$key], 'Process') }
}
