# release.ps1 - create the GitHub repo + tagged release for Farever Pal.
#
# PREREQUISITE: you must be authenticated with the GitHub CLI first:
#     gh auth login
# (the agent could not run this for you because it is interactive).
#
# Run this script from the companion\ directory:
#     powershell -ExecutionPolicy Bypass -File release.ps1
#
# It is idempotent-ish: if the repo already exists it skips creation and just
# pushes; if the release already exists `gh release create` will error (delete
# it first with `gh release delete v<version>` if you need to re-cut).

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# --- read version from the package ---
$initPy  = Join-Path $PSScriptRoot 'farever_companion\__init__.py'
$verLine = Select-String -Path $initPy -Pattern '__version__\s*=\s*"([^"]+)"' | Select-Object -First 1
if (-not $verLine) { throw "Could not find __version__ in $initPy" }
$version = $verLine.Matches[0].Groups[1].Value
$tag     = "v$version"
$exe     = Join-Path $PSScriptRoot 'dist\FareverPal.exe'

Write-Host "Farever Pal version: $version  (tag $tag)"

# --- confirm auth ---
gh auth status
if ($LASTEXITCODE -ne 0) { throw "Not authenticated. Run 'gh auth login' first." }

# --- confirm the built exe exists ---
if (-not (Test-Path $exe)) {
    throw "Missing $exe. Build it first: build.bat then package.bat"
}
$exeMB = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "Release asset: $exe ($exeMB MB)"

# --- create the repo (public) + push, if origin doesn't exist yet ---
$hasOrigin = $false
git remote get-url origin 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { $hasOrigin = $true }

if (-not $hasOrigin) {
    Write-Host "Creating public repo 'FareverPal' and pushing main..."
    gh repo create FareverPal --public --source=. --remote=origin --push
    if ($LASTEXITCODE -ne 0) { throw "gh repo create failed." }
} else {
    Write-Host "origin already configured; pushing main..."
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { throw "git push failed." }
}

# --- create the release with the exe attached ---
$notes = @"
Farever Pal $tag

A read-only, out-of-process live companion + QA overlay for Farever
(entity/loot overlay, DPS meter, minimap, offline loot predictor).

Download ``FareverPal.exe`` below and run it. Self-contained, no install.
"@

Write-Host "Creating release $tag with FareverPal.exe attached..."
gh release create $tag $exe --title "Farever Pal $version" --notes $notes
if ($LASTEXITCODE -ne 0) { throw "gh release create failed." }

# --- report the asset download URL (for GITHUB_RELEASE_URL in the website .env) ---
$repo = (gh repo view --json nameWithOwner -q .nameWithOwner)
$assetUrl = "https://github.com/$repo/releases/download/$tag/FareverPal.exe"
Write-Host ""
Write-Host "DONE. Release asset download URL (use for GITHUB_RELEASE_URL):"
Write-Host "  $assetUrl"
