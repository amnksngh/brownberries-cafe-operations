param(
  [string]$Branch = "main",
  [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

# Safe source-only deployment. The Windows instance/ directory remains the source
# of truth for production data and is backed up before code is changed.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\.."))
$BackupRoot = Join-Path $RepoDir "instance_windows_backup"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$BackupDir = Join-Path $BackupRoot $Stamp
$AppServiceName = "BrownberriesApp"
$HealthUrl = "http://127.0.0.1:5050/healthz"

Set-Location $RepoDir

if (-not (Test-Path (Join-Path $RepoDir ".git"))) {
  throw "Not a Git repository: $RepoDir"
}

$dirtySource = git status --porcelain --untracked-files=no
if ($dirtySource) {
  throw "Tracked local source changes exist. Commit or stash them before running this update."
}

New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
$appService = Get-Service -Name $AppServiceName -ErrorAction SilentlyContinue
$wasRunning = $null -ne $appService -and $appService.Status -eq "Running"

try {
  if ($null -ne $appService -and $wasRunning) {
    Write-Host "Stopping $AppServiceName before backup..."
    Stop-Service -Name $AppServiceName -Force
    Start-Sleep -Seconds 3
  }

  foreach ($runtimePath in @("instance", "static\uploads")) {
    $source = Join-Path $RepoDir $runtimePath
    if (Test-Path $source) {
      $destination = Join-Path $BackupDir $runtimePath
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
      Copy-Item -Path $source -Destination $destination -Recurse -Force
    }
  }

  Write-Host "Runtime backup created at $BackupDir"
  git fetch origin $Branch
  git pull --ff-only origin $Branch

  if (-not $SkipDependencyInstall) {
    $python = Join-Path $RepoDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $python)) {
      throw "Python virtual environment not found at $python"
    }
    & $python -m pip install -r (Join-Path $RepoDir "requirements.txt")
  }

  if ($null -ne $appService) {
    Write-Host "Starting $AppServiceName..."
    Start-Service -Name $AppServiceName
  } else {
    throw "Service $AppServiceName is not installed. Install/configure it before using this updater."
  }

  $healthy = $false
  for ($attempt = 1; $attempt -le 12; $attempt++) {
    Start-Sleep -Seconds 3
    try {
      $response = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 5
      if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
  }
  if (-not $healthy) {
    throw "The app did not pass $HealthUrl after restart. Check logs before serving traffic."
  }
  Write-Host "Deployment complete. Local health check passed. Cloudflare Tunnel was left running."
}
catch {
  Write-Error $_
  if ($null -ne $appService -and $wasRunning) {
    try { Start-Service -Name $AppServiceName } catch { Write-Warning "Could not restore ${AppServiceName}: $($_.Exception.Message)" }
  }
  throw
}
