param([Parameter(Mandatory=$true)][string]$PythonExe)
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DataDir = (& $PythonExe -c "from homefull.config import DATA; print(DATA)").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Install HomeFull in the selected Python environment first.' }
$ProcessFile = Join-Path $DataDir 'homefull_process.json'
if (Test-Path -LiteralPath $ProcessFile) {
    $BotState = Get-Content -LiteralPath $ProcessFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $BotProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($BotState.pid)"
    if ($BotProcess -and $BotProcess.CommandLine -match '(?:\-m\s+homefull\.bot|homefull)(?:\s|$)') { exit 0 }
}
$null = New-Item -ItemType Directory -Path $DataDir -Force
$env:PYTHONUTF8 = '1'
Start-Process -FilePath $PythonExe -ArgumentList '-m','homefull.bot' `
    -WorkingDirectory (Join-Path $ProjectRoot 'src') -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $DataDir 'homefull_stdout.log') `
    -RedirectStandardError (Join-Path $DataDir 'homefull_stderr.log')
