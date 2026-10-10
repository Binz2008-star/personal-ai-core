$ErrorActionPreference = "Continue"
$pacRunRoot = Get-Content "C:\Users\loyal\AppData\Local\Temp\opencode\pac-boss-runroot.txt"
$venvScripts = Join-Path $pacRunRoot "venv\Scripts"
$venvPython = Join-Path $venvScripts "python.exe"
$venvPytest = Join-Path $venvScripts "pytest.exe"
$pf = Join-Path $pacRunRoot "preflight"
$scratch = Join-Path $pf "scratch"
New-Item -ItemType Directory -Path $scratch -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $scratch "test_ok.py"),
    "def test_ok():`n    assert 1 + 1 == 2`n", [Text.UTF8Encoding]::new($false))
$helper = Join-Path $pf "subprocess_check.py"

$runs = New-Object System.Collections.Generic.List[object]
for ($i = 1; $i -le 5; $i++) {
    $start = Get-Date
    $raw = & $venvPython $helper $venvPytest $scratch
    $wrapperExit = $LASTEXITCODE
    $end = Get-Date
    $parsed = $raw | ConvertFrom-Json
    $runs.Add([pscustomobject]@{
        index = $i; start = $start.ToString("o"); end = $end.ToString("o")
        wrapper_exit = $wrapperExit; returncode = $parsed.returncode
        stdout = $parsed.stdout; stderr = $parsed.stderr
    })
    Write-Output ("subprocess-check-{0} returncode={1}" -f $i, $parsed.returncode)
}
[ordered]@{
    description = "Core launch path reproduced: venv python subprocess -> venv pytest.exe, 5 checks"
    venv_python = $venvPython; venv_pytest = $venvPytest; cwd = $scratch
    runs = $runs
} | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $pf "subprocess-checks.json")

$log = 'Microsoft-Windows-CodeIntegrity/Operational'
function Export-Ids($ids, $name) {
    $ev = @()
    try { $ev = @(Get-WinEvent -FilterHashtable @{ LogName = $log; Id = $ids } -ErrorAction SilentlyContinue) } catch {}
    $out = @($ev | Sort-Object TimeCreated | ForEach-Object {
        [ordered]@{ time = $_.TimeCreated.ToString("o"); id = $_.Id
                    provider = $_.ProviderName; message = $_.Message }
    })
    [ordered]@{ log = $log; ids = @($ids); count = $out.Count; events = $out } |
        ConvertTo-Json -Depth 6 | Set-Content (Join-Path $pf $name)
    Write-Output ("exported {0}: {1} events" -f $name, $out.Count)
}
Export-Ids @(3077) "codeintegrity-3077-all.json"
Export-Ids @(3089) "codeintegrity-3089-all.json"
Export-Ids @(3118) "codeintegrity-3118-all.json"
Export-Ids @(3033) "codeintegrity-3033-all.json"

try {
    Get-WinEvent -ListLog $log | Select-Object LogName, IsEnabled, RecordCount, LogMode, MaximumSizeInBytes |
        ConvertTo-Json | Set-Content (Join-Path $pf "codeintegrity-log-meta.json")
} catch {}
Write-Output "PREFLIGHT2 DONE"