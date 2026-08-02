# Launch Gajae-Code (gjc) in the entity repo without tmux (recommended on native Windows).
$ErrorActionPreference = "Stop"

$bunBin = Join-Path $env:USERPROFILE ".bun\bin"
if (Test-Path (Join-Path $bunBin "bun.exe")) {
  $env:Path = "$bunBin;" + $env:Path
}

if (-not (Get-Command gjc -ErrorAction SilentlyContinue)) {
  Write-Host "gjc not found. Install with:" -ForegroundColor Yellow
  Write-Host '  powershell -c "irm bun.sh/install.ps1|iex"'
  Write-Host "  bun install -g gajae-code"
  exit 1
}

Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
$env:GJC_SESSION_ID = if ($env:GJC_SESSION_ID) { $env:GJC_SESSION_ID } else { "entity" }

Write-Host "gjc $($(gjc --version))  |  session=$env:GJC_SESSION_ID  |  cwd=$PWD" -ForegroundColor Cyan
Write-Host "Tips: /skill:deep-interview  →  /skill:ralplan  →  implement in Cursor Cloud/Local" -ForegroundColor DarkGray
Write-Host ""

if ($args.Count -gt 0) {
  & gjc @args
} else {
  & gjc
}
