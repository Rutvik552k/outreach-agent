# scripts\check.ps1 - local pre-push gate (H-2). Mirrors .github\workflows\ci.yml.
# NOTE: keep this file pure ASCII - Windows PowerShell 5.1 mis-decodes BOM-less
# UTF-8 and a single multi-byte character breaks the parser.
#
# Run this BEFORE every push (documented in README "Pre-push gate"):
#   powershell -ExecutionPolicy Bypass -File scripts\check.ps1
#
# Gate 1  Lockfile conformance (C-5, offline half): every locked package
#         installed at its locked version, hash-bearing lock, no unpinned
#         ingress. NOTE (honest gap): pip cannot re-verify archive hashes of
#         already-installed packages offline - cryptographic hash verification
#         happens at install time only (pip install --require-hashes), which
#         CI does on every run and README mandates for fresh setups.
# Gate 2  Full default pytest lane (mocked CI lane, ADR section 12).
# Gate 3  The C-1 structural-incapability scanner by explicit path - cannot
#         be deselected silently (pytest exits non-zero if missing/deselected).

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "FAIL: venv python not found at $python - create the venv per README." -ForegroundColor Red
    exit 1
}

Write-Host "[1/3] Lockfile conformance (C-5 offline check)..." -ForegroundColor Cyan
& $python scripts\verify_lock.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL: venv does not conform to requirements.lock." -ForegroundColor Red
    exit 1
}

Write-Host "[2/3] Full default pytest lane..." -ForegroundColor Cyan
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL: test suite not green." -ForegroundColor Red
    exit 1
}

Write-Host "[3/3] C-1 structural-incapability scanner (explicit)..." -ForegroundColor Cyan
& $python -m pytest -q tests\test_no_client_outside_gateway.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL: C-1 scanner gate not green." -ForegroundColor Red
    exit 1
}

Write-Host "PASS: all pre-push gates green." -ForegroundColor Green
exit 0
