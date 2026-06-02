param(
    [string]$Version = "",
    [ValidateSet("patch", "minor", "major")]
    [string]$Part = "patch",
    [string]$Remote = "origin",
    [string]$Branch = "",
    [string]$Message = "",
    [switch]$SkipChecks,
    [switch]$SkipPythonTests,
    [switch]$SkipTypecheck,
    [switch]$AllowNonMain,
    [switch]$DryRun,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..")
Set-Location $RepoRoot

function Invoke-Step {
    param(
        [string]$Command,
        [string[]]$Arguments = @()
    )

    $line = "$Command $($Arguments -join ' ')".Trim()
    Write-Host ""
    Write-Host "> $line" -ForegroundColor Cyan
    if ($DryRun) {
        return
    }
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $line"
    }
}

function Get-CommandOutput {
    param(
        [string]$Command,
        [string[]]$Arguments = @()
    )

    $output = & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
    return ($output -join "`n").Trim()
}

function Get-NextVersion {
    param(
        [string]$Current,
        [string]$BumpPart
    )

    if ($Current -notmatch '^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$') {
        throw "Cannot auto-bump non-semver version: $Current"
    }

    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    $patch = [int]$Matches[3]

    switch ($BumpPart) {
        "major" {
            $major += 1
            $minor = 0
            $patch = 0
        }
        "minor" {
            $minor += 1
            $patch = 0
        }
        default {
            $patch += 1
        }
    }

    return "$major.$minor.$patch"
}

function Get-GitHubActionsUrl {
    param([string]$RemoteUrl)

    if ($RemoteUrl -match '^https://github\.com/([^/]+/[^/.]+)(?:\.git)?$') {
        return "https://github.com/$($Matches[1])/actions/workflows/release.yml"
    }
    if ($RemoteUrl -match '^git@github\.com:([^/]+/[^/.]+)(?:\.git)?$') {
        return "https://github.com/$($Matches[1])/actions/workflows/release.yml"
    }
    return ""
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git is not available in PATH."
}
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    throw "node is not available in PATH."
}
if (-not (Get-Command corepack -ErrorAction SilentlyContinue)) {
    throw "corepack is not available in PATH."
}

$remoteUrl = Get-CommandOutput git @("remote", "get-url", $Remote)
$currentBranch = Get-CommandOutput git @("branch", "--show-current")
if ([string]::IsNullOrWhiteSpace($currentBranch)) {
    throw "Cannot release from a detached HEAD."
}
if ([string]::IsNullOrWhiteSpace($Branch)) {
    $Branch = $currentBranch
}
if (-not $AllowNonMain -and $Branch -ne "main") {
    throw "Refusing to release from branch '$Branch'. Switch to main or pass -AllowNonMain."
}

$pkg = Get-Content "package.json" -Raw | ConvertFrom-Json
$currentVersion = [string]$pkg.version
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = Get-NextVersion -Current $currentVersion -BumpPart $Part
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Invalid semver version: $Version"
}

$tag = "v$Version"
if ([string]::IsNullOrWhiteSpace($Message)) {
    $Message = "release: $tag"
}

Write-Host "Release target" -ForegroundColor Green
Write-Host "  repo:    $remoteUrl"
Write-Host "  branch:  $Branch"
Write-Host "  current: $currentVersion"
Write-Host "  next:    $Version"
Write-Host "  tag:     $tag"

if (-not $DryRun) {
    Invoke-Step git @("fetch", $Remote, $Branch, "--tags")
    $aheadBehind = Get-CommandOutput git @("rev-list", "--left-right", "--count", "$Remote/$Branch...HEAD")
    $parts = $aheadBehind -split '\s+'
    $behind = [int]$parts[0]
    if ($behind -gt 0) {
        throw "Local branch is behind $Remote/$Branch by $behind commit(s). Pull/rebase before releasing."
    }
}

$existingLocalTag = Get-CommandOutput git @("tag", "--list", $tag)
if (-not [string]::IsNullOrWhiteSpace($existingLocalTag)) {
    throw "Local tag already exists: $tag"
}
if (-not $DryRun) {
    $existingRemoteTag = Get-CommandOutput git @("ls-remote", "--tags", $Remote, "refs/tags/$tag")
    if (-not [string]::IsNullOrWhiteSpace($existingRemoteTag)) {
        throw "Remote tag already exists: $tag"
    }
}

if ($currentVersion -ne $Version) {
    $nodeCode = @"
const fs = require('fs');
const version = process.argv[1];
const path = 'package.json';
const pkg = JSON.parse(fs.readFileSync(path, 'utf8'));
pkg.version = version;
fs.writeFileSync(path, JSON.stringify(pkg, null, 2) + '\n');
"@
    Invoke-Step node @("-e", $nodeCode, $Version)
} else {
    Write-Host ""
    Write-Host "package.json is already at $Version" -ForegroundColor Yellow
}

Invoke-Step corepack @("pnpm", "version:sync")

if (-not $SkipChecks) {
    if (-not $SkipPythonTests) {
        $python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
        if (-not (Test-Path $python)) {
            $python = "python"
        }
        Invoke-Step $python @("-m", "pytest", "tests\test_performance_modes.py")
    }
    if (-not $SkipTypecheck) {
        Invoke-Step corepack @("pnpm", "typecheck")
    }
}

$status = Get-CommandOutput git @("status", "--short")
if ([string]::IsNullOrWhiteSpace($status)) {
    throw "No changes to commit. Bump the version or make changes before releasing."
}

Write-Host ""
Write-Host "Files to commit:" -ForegroundColor Green
Write-Host $status

if (-not $Yes -and -not $DryRun) {
    Write-Host ""
    $answer = Read-Host "Type RELEASE to commit, tag, and push $tag"
    if ($answer -ne "RELEASE") {
        throw "Release cancelled."
    }
}

Invoke-Step git @("add", "-A")
Invoke-Step git @("commit", "--no-verify", "-m", $Message)
Invoke-Step git @("tag", "-a", $tag, "-m", "Release $tag")
Invoke-Step git @("push", $Remote, "HEAD:$Branch")
Invoke-Step git @("push", $Remote, $tag)

Write-Host ""
if ($DryRun) {
    Write-Host "Dry run complete. No commit, tag, or push was created." -ForegroundColor Yellow
} else {
    Write-Host "Release pushed: $tag" -ForegroundColor Green
}
$actionsUrl = Get-GitHubActionsUrl -RemoteUrl $remoteUrl
if (-not [string]::IsNullOrWhiteSpace($actionsUrl)) {
    Write-Host "GitHub Actions: $actionsUrl"
}
