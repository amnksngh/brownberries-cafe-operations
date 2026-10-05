$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\.."))
if (Test-Path -LiteralPath (Join-Path $RepoDir "instance\cloud_production.active")) {
  exit 0
}
$LogDir = Join-Path $RepoDir "logs"
$LogFile = Join-Path $LogDir "windows-watchdog.log"
$LocalHealthUrl = "http://127.0.0.1:5050/healthz"
$PublicHealthUrl = "https://brownberriescafe.com/healthz"
$AppRestartRequestFile = Join-Path $RepoDir "instance\restart_app.request"
$TunnelRestartRequestFile = Join-Path $RepoDir "instance\restart_tunnel.request"
$InternetProbeHost = "one.one.one.one"
$AppServiceName = "BrownberriesApp"
$TunnelServiceCandidates = @(
  "BrownberriesTunnel",
  "cloudflared"
)

if (-not (Test-Path $LogDir)) {
  New-Item -ItemType Directory -Path $LogDir | Out-Null
}

function Write-Log {
  param(
    [string]$Message
  )

  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $LogFile -Value "[$timestamp] $Message"
}

function Ensure-ServiceRunning {
  param(
    [string]$ServiceName
  )

  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if ($null -eq $service) {
    Write-Log "Service not found: $ServiceName"
    return $false
  }

  if ($service.Status -ne "Running") {
    Write-Log "Starting service: $ServiceName"
    Start-Service -Name $ServiceName
    Start-Sleep -Seconds 5
    $service.Refresh()
  }

  if ($service.Status -eq "Running") {
    Write-Log "Service running: $ServiceName"
    return $true
  }

  Write-Log "Service failed to reach running state: $ServiceName (status=$($service.Status))"
  return $false
}

function Test-UrlOk {
  param(
    [string]$Url,
    [int]$TimeoutSec = 10,
    [int]$Attempts = 1
  )

  for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
      $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
      if ($response.StatusCode -eq 200) { return $true }
    } catch {}
    if ($attempt -lt $Attempts) { Start-Sleep -Seconds 3 }
  }
  return $false
}

function Restart-ServiceSafe {
  param(
    [string]$ServiceName
  )

  try {
    Write-Log "Restarting service: $ServiceName"
    Restart-Service -Name $ServiceName -Force -ErrorAction Stop
    Start-Sleep -Seconds 8
  } catch {
    Write-Log "Failed to restart $ServiceName : $($_.Exception.Message)"
  }
}

Write-Log "Watchdog run started"

$TunnelServiceName = $null
foreach ($candidate in $TunnelServiceCandidates) {
  $candidateService = Get-Service -Name $candidate -ErrorAction SilentlyContinue
  if ($null -ne $candidateService) {
    $TunnelServiceName = $candidate
    break
  }
}

[void](Ensure-ServiceRunning -ServiceName $AppServiceName)

# A deploy can request one privileged restart without granting the interactive
# account service-control rights.  The SYSTEM watchdog consumes the marker once
# and follows the same logged restart path used by health recovery.
if (Test-Path -LiteralPath $AppRestartRequestFile) {
  Write-Log "One-shot application restart requested"
  Remove-Item -LiteralPath $AppRestartRequestFile -Force -ErrorAction SilentlyContinue
  Restart-ServiceSafe -ServiceName $AppServiceName
}

if ($null -ne $TunnelServiceName) {
  [void](Ensure-ServiceRunning -ServiceName $TunnelServiceName)
  if (Test-Path -LiteralPath $TunnelRestartRequestFile) {
    Write-Log "One-shot tunnel restart requested"
    Remove-Item -LiteralPath $TunnelRestartRequestFile -Force -ErrorAction SilentlyContinue
    Restart-ServiceSafe -ServiceName $TunnelServiceName
  }
} else {
  Write-Log "No tunnel service found. Checked: $($TunnelServiceCandidates -join ', ')"
}

if (-not (Test-UrlOk -Url $LocalHealthUrl -TimeoutSec 10 -Attempts 3)) {
  Write-Log "Local health check failed at $LocalHealthUrl"
  Restart-ServiceSafe -ServiceName $AppServiceName
  Start-Sleep -Seconds 5

  if (-not (Test-UrlOk -Url $LocalHealthUrl -TimeoutSec 10)) {
    Write-Log "Local health still failing after restart"
  } else {
    Write-Log "Local health restored after BrownberriesApp restart"
  }
} else {
  Write-Log "Local health check passed"
}

$internetOk = $false
try {
  $internetOk = Test-NetConnection -ComputerName $InternetProbeHost -Port 443 -InformationLevel Quiet -WarningAction SilentlyContinue
} catch {
  $internetOk = $false
}

if ($internetOk) {
  Write-Log "Internet probe passed"
  if (-not (Test-UrlOk -Url $PublicHealthUrl -TimeoutSec 15 -Attempts 3)) {
    Write-Log "Public health check failed at $PublicHealthUrl"
    if ($null -ne $TunnelServiceName) {
      Restart-ServiceSafe -ServiceName $TunnelServiceName
      Start-Sleep -Seconds 5

      if (-not (Test-UrlOk -Url $PublicHealthUrl -TimeoutSec 15)) {
        Write-Log "Public health still failing after tunnel restart"
      } else {
        Write-Log "Public health restored after tunnel restart"
      }
    } else {
      Write-Log "No tunnel service available to restart"
    }
  } else {
    Write-Log "Public health check passed"
  }
} else {
  Write-Log "Internet probe failed; skipping public health check"
}

Write-Log "Watchdog run finished"
