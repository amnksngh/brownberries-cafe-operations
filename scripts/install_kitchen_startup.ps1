# Run elevated under the same Windows account that owns the installed printer.
param([string]$ServerUrl = 'https://brownberriescafe.com')
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open PowerShell as administrator using the kitchen Windows account, then run setup again.'
}
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$agent = Join-Path $PSScriptRoot 'kitchen_print_agent.py'
if (!(Test-Path -LiteralPath $python) -or !(Test-Path -LiteralPath $agent)) { throw 'Place setup and agent beside the .venv folder.' }
if (!(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'kot_windows_raster.py'))) { throw 'Copy kot_windows_raster.py beside kitchen_print_agent.py before setup.' }
$server = [uri]$ServerUrl
if ($server.Scheme -ne 'https' -or $server.UserInfo -or $server.Query -or $server.Fragment -or $server.AbsolutePath -ne '/') { throw 'Use the HTTPS server origin only.' }
$ServerUrl = $server.GetLeftPart([System.UriPartial]::Authority)
$taskName = 'Brownberries Kitchen KOT'
Write-Host 'Stop any manually running kitchen agent before continuing.'
$credential = Get-Credential -UserName $identity.Name -Message 'Windows account password (not PIN). Used by Task Scheduler to run before sign-in.'
if (!$credential -or $credential.UserName -ne $identity.Name) { throw 'Use the current Windows account so encrypted keys and printers remain accessible.' }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) { Stop-ScheduledTask -TaskName $taskName }
& $python $agent --url $ServerUrl --save-key
if ($LASTEXITCODE -ne 0) { throw 'Could not save station key. Task was not started.' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('-u "{0}" --url "{1}" --unattended' -f $agent, $ServerUrl) -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
try {
    $password = $credential.GetNetworkCredential().Password
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -User $identity.Name -Password $password -RunLevel Limited -Force | Out-Null
} finally { $password = $null; $credential = $null }
Start-ScheduledTask -TaskName $taskName
Write-Host 'Startup task installed and started. Check Connected in the live KOT settings.'
Write-Host 'Logs: %LOCALAPPDATA%\BrownberriesKitchen\agent.log'
