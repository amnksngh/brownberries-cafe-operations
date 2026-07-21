$ErrorActionPreference = "Stop"

$services = @(
  "BrownberriesApp",
  "cloudflared"
)

foreach ($serviceName in $services) {
  $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
  if ($null -eq $service) {
    Write-Output "Service not found: $serviceName"
    continue
  }

  if ($service.Status -ne "Running") {
    Write-Output "Starting service: $serviceName"
    Start-Service -Name $serviceName
  } else {
    Write-Output "Already running: $serviceName"
  }
}

