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
Set-Location $pacSource

function Invoke-PacBench($pacSha, $pacName, [bool]$pacVerify = $false) {
    git checkout --detach $pacSha
    if ($LASTEXITCODE -ne 0) { throw "Checkout failed." }
    if ((git rev-parse HEAD).Trim() -ne $pacSha) { throw "Wrong code SHA." }
    if (git status --porcelain) { throw "Checkout is dirty." }
    $pacOut = Join-Path $pacRunRoot $pacName
    $pacArgs = @("-m", "personal_ai_core.app.bench",
        "--runs", "5", "--languages", "en", "ar", "--num-ctx", "8192",
        "--only", "git-last-commit-file", "verify-off-by-one", "--out", $pacOut)
    if ($pacVerify) { $pacArgs += "--verify-completion" }
    & $pacPython @pacArgs | Tee-Object -FilePath (Join-Path $pacRunRoot ($pacName + ".log"))
    if ($LASTEXITCODE -ne 0) { throw "Harness/setup error or context mismatch; inspect the log." }
}
Invoke-PacBench "a43c6f4443f8ed8ea4aac39570060ecedd2a5591" "commands-baseline"
Invoke-PacBench "ab448b5273d291dd420422e929e14345c8e07b83" "commands-guidance"
Write-Output "COMPARISON A COMPLETE"
