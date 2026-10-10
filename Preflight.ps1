$ErrorActionPreference = "Continue"
$pacRunRoot = Get-Content "C:\Users\loyal\AppData\Local\Temp\opencode\pac-boss-runroot.txt"
$pf = Join-Path $pacRunRoot "preflight"
$verify = Join-Path $pf "scratch"
New-Item -ItemType Directory -Path $verify -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $verify "test_ok.py"),
    "def test_ok():`n    assert 1 + 1 == 2`n", [Text.UTF8Encoding]::new($false))

$venvScripts = Join-Path $pacRunRoot "venv\Scripts"
$env:PATH = $venvScripts + ";" + $env:PATH
$env:PYTHONUTF8 = "1"

$cmdPytest = @(Get-Command pytest -All -ErrorAction SilentlyContinue)
$cmdPython = @(Get-Command python -All -ErrorAction SilentlyContinue)
$pytestPath = if ($cmdPytest.Count) { $cmdPytest[0].Source } else { $null }
$pythonPath = if ($cmdPython.Count) { $cmdPython[0].Source } else { $null }
$venvPytest = Join-Path $venvScripts "pytest.exe"
$venvPytestCmd = Join-Path $venvScripts "pytest.cmd"

[ordered]@{
    pytest_resolved = @($cmdPytest | ForEach-Object { $_.Source })
    python_resolved = @($cmdPython | ForEach-Object { $_.Source })
    venv_scripts    = $venvScripts
    venv_pytest_exe = $venvPytest
    venv_pytest_cmd = $venvPytestCmd
    pytest_exe_exists = (Test-Path $venvPytest)
    pytest_cmd_exists = (Test-Path $venvPytestCmd)
} | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $pf "resolved.json")

if ($pytestPath) {
    $sig = Get-AuthenticodeSignature $pytestPath
    $fi = (Get-Item $pytestPath).VersionInfo
    [ordered]@{
        pytest = $pytestPath; signature_status = $sig.Status.ToString()
        signer = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { $null }
        size = (Get-Item $pytestPath).Length
        product = $fi.ProductName; company = $fi.CompanyName; version = $fi.FileVersion
    } | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $pf "pytest-signature.json")
}

$results = New-Object System.Collections.Generic.List[object]
function Invoke-Run($label, $exe, [string[]]$argList) {
    $start = Get-Date
    $out = ""; $code = $null
    try { $out = (& $exe @argList 2>&1 | Out-String); $code = $LASTEXITCODE }
    catch { $out = "EXCEPTION: " + $_.Exception.Message; $code = -999 }
    $end = Get-Date
    $results.Add([pscustomobject]@{
        label = $label; executable = $exe; args = ($argList -join ' ')
        start = $start.ToString("o"); end = $end.ToString("o"); exit = $code
        output = $out.Trim()
    })
    $first = ($out.Trim() -split "`r?`n" | Select-Object -First 2) -join " | "
    Write-Output ("[{0}] exit={1} at {2} :: {3}" -f $label, $code, $start.ToString("HH:mm:ss.fff"), $first)
}

Set-Location $verify
$t0 = Get-Date
for ($i = 1; $i -le 5; $i++) {
    Invoke-Run "bare-pytest-$i" "pytest" @("-q", "-p", "no:cacheprovider", "test_ok.py")
    Invoke-Run "python-m-pytest-$i" $pythonPath @("-m", "pytest", "-q", "-p", "no:cacheprovider", "test_ok.py")
}
$t1 = Get-Date
$results | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $pf "runs.json")

# CodeIntegrity Operational 3077/3089 within the run window (read-only)
$ci3077_3089 = @()
try {
    $ci3077_3089 = @(Get-WinEvent -FilterHashtable @{
        LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
        Id = 3077, 3089
        StartTime = $t0.AddSeconds(-10); EndTime = $t1.AddSeconds(10)
    } -ErrorAction SilentlyContinue)
} catch { Write-Output ("3077/3089 query error: " + $_.Exception.Message) }
[ordered]@{
    window_start = $t0.ToString("o"); window_end = $t1.ToString("o")
    count = $ci3077_3089.Count
    events = @($ci3077_3089 | ForEach-Object {
        [ordered]@{ time = $_.TimeCreated.ToString("o"); id = $_.Id
                    provider = $_.ProviderName; message = $_.Message } })
} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $pf "codeintegrity-events.json")
Write-Output ("CodeIntegrity 3077/3089 events in window: {0}" -f $ci3077_3089.Count)

# All CodeIntegrity events in window, by id (context)
$ciAll = @()
try {
    $ciAll = @(Get-WinEvent -FilterHashtable @{
        LogName = 'Microsoft-Windows-CodeIntegrity/Operational'
        StartTime = $t0.AddSeconds(-10); EndTime = $t1.AddSeconds(10)
    } -ErrorAction SilentlyContinue)
} catch {}
$ciAll | Group-Object Id | Select-Object Name, Count | Format-Table -AutoSize

$ciAll | ForEach-Object {
    [ordered]@{ time = $_.TimeCreated.ToString("o"); id = $_.Id; message = $_.Message }
} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $pf "codeintegrity-all.json")

# Read-only policy status
$sac = $null
try { $sac = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState } catch {}
$dg = $null
try { $dg = Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard -ErrorAction Stop | Select-Object * -ExcludeProperty CimClass, CimInstanceProperties, CimSystemProperties } catch {}
[ordered]@{
    execution_policy = (Get-ExecutionPolicy).ToString()
    machine_policy   = (Get-ExecutionPolicy -Scope MachinePolicy).ToString()
    user_policy      = (Get-ExecutionPolicy -Scope UserPolicy).ToString()
    smart_app_control_state = $sac
    device_guard = $dg
} | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $pf "policy.json")

Write-Output "PREFLIGHT DONE"