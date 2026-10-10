$ErrorActionPreference = "Stop"
$pacRunRoot = Get-Content "C:\Users\loyal\AppData\Local\Temp\opencode\pac-boss-runroot.txt"
$pacSource = Join-Path $pacRunRoot "source"
$pacVenv = Join-Path $pacRunRoot "venv"
$pacPython = Join-Path $pacVenv "Scripts\python.exe"
$env:PATH = (Join-Path $pacVenv "Scripts") + ";" + $env:PATH
$env:PYTHONPATH = Join-Path $pacSource "src"
$env:PYTHONUTF8 = "1"
$env:PAC_BOSS_MODEL = "huihui_ai/qwen2.5-abliterate:7b"
$env:PAC_BOSS_CONTEXT_WINDOW = "8192"
$env:PAC_REQUEST_TIMEOUT_SECONDS = "600"
# Belt and braces: even though the observer now emits pure ASCII, keep the pipe UTF-8.
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Text.UTF8Encoding]::new($false)
Set-Location $pacSource
$pacUtf8 = [Text.UTF8Encoding]::new($false)
$pacCases = Get-Content (Join-Path $pacRunRoot "exact-read-fixtures.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$pacObserver = Join-Path $pacRunRoot "ai-core-windows-exact-read-pilot.corrected.py"
$checks = New-Object System.Collections.Generic.List[object]

function Invoke-PacExact($pacPhase, $pacSha) {
    git checkout --detach $pacSha
    if ($LASTEXITCODE -ne 0) { throw "Checkout failed." }
    if ((git rev-parse HEAD).Trim() -ne $pacSha) { throw "Wrong code SHA." }
    if (git status --porcelain) { throw "Checkout is dirty." }
    $pacSink = Join-Path $pacRunRoot ("exact-read-" + $pacPhase + "-v2.jsonl")
    if (Test-Path $pacSink) { throw "Preserve the existing result; use a fresh sink name." }
    foreach ($pacCase in $pacCases) {
        $pacCaseRoot = Join-Path $pacRunRoot ("copy2-" + $pacPhase + "-" + $pacCase.id)
        $pacWorkspace = Join-Path $pacCaseRoot "workspace"
        $pacDatabase = Join-Path $pacCaseRoot "core.db"
        $pacProfile = Join-Path $pacCaseRoot "profile.md"
        New-Item -ItemType Directory -Path $pacWorkspace -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $pacWorkspace "notes.txt"), $pacCase.text, $pacUtf8)
        [IO.File]::WriteAllText($pacProfile, "", $pacUtf8)
        $pacRecord = & $pacPython $pacObserver --phase $pacPhase --language $pacCase.language `
            --case-id $pacCase.id --workspace $pacWorkspace --database $pacDatabase --profile $pacProfile
        if ($LASTEXITCODE -ne 0) { throw "Observer failed before producing evidence." }
        $pacLine = $pacRecord -join "`n"
        $pacParsed = $pacLine | ConvertFrom-Json
        if ($pacParsed.case_id -ne $pacCase.id) { throw "Wrong case record." }
        # Capture fix: the line must be pure ASCII after escaping.
        $nonAscii = @($pacLine.ToCharArray() | Where-Object { [int]$_ -gt 127 }).Count
        if ($null -eq $pacParsed.expected) {
            throw ("No 'expected' in record for {0}: {1}" -f $pacCase.id,
                   $pacLine.Substring(0, [Math]::Min(200, $pacLine.Length)))
        }
        $expectedStr = [string]$pacParsed.expected
        $fixtureStr = [string]$pacCase.text
        # Fix acceptance: the captured expected must equal the frozen fixture text.
        $expectedMatches = ($expectedStr -eq $fixtureStr)
        $checks.Add([pscustomobject]@{
            phase = $pacPhase; case_id = $pacCase.id; language = $pacCase.language
            non_ascii_chars = $nonAscii; expected_equals_fixture = $expectedMatches
            fixture_len = $fixtureStr.Length; expected_len = $expectedStr.Length
        })
        if ($nonAscii -ne 0) { throw "Captured line is not pure ASCII for $($pacCase.id)." }
        if (-not $expectedMatches) { throw "Captured expected != fixture for $($pacCase.id)." }
        [IO.File]::AppendAllText($pacSink, $pacLine + "`n", $pacUtf8)
        Write-Host "$pacPhase $($pacCase.id): ascii_ok expected==fixture=$expectedMatches exact=$($pacParsed.useful_exact_copy) fabricated=$($pacParsed.accepted_fabrication)"
    }
}
Invoke-PacExact "baseline" "a43c6f4443f8ed8ea4aac39570060ecedd2a5591"
Invoke-PacExact "candidate" "086c35548dce4e4d113879886695e2a465b16b93"
$checks | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $pacRunRoot "exact-read-v2-capture-checks.json")
Write-Output "COMPARISON B-v2 COMPLETE"
