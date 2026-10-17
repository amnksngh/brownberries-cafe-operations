# Run in PowerShell after reviewing and staging the files you want to publish.
[CmdletBinding()]
param(
    [string]$Message = 'Update staff payroll and manual attendance register',
    [switch]$Login
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$aws = 'C:/Users/hp/AppData/Local/Programs/Amazon/AWSCLIV2/aws.exe'
$python = 'C:/Brownberries/brownberries-cafe-operations/.venv/Scripts/python.exe'
$branch = 'feature/explicit-menu-serving-hours'
$awsArgs = @('--profile','brownberries-cafe','--region','ap-southeast-2','--no-cli-pager')
$instance = 'i-0cb2411232770a113'
function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE). Stopped." }
}
Push-Location $repo
try {
    $actualBranch = & git branch --show-current
    Check-Exit 'Read branch'
    if ($actualBranch -ne $branch) { throw "Expected branch $branch, found $actualBranch." }
    $remote = & git remote get-url origin
    Check-Exit 'Read remote'
    if ($remote -ne 'https://github.com/amnksngh/brownberries-cafe-operations.git') { throw 'Unexpected GitHub remote.' }
    & git status --short
    & git diff --exit-code
    Check-Exit 'Unstaged tracked changes check: stage or set aside your changes first'
    $untracked = & git ls-files --others --exclude-standard
    Check-Exit 'Untracked files check'
    if ($untracked) { throw 'Untracked files remain. Review and stage intended files first; do not accidentally publish secrets or database files.' }
    & git diff --cached --check
    Check-Exit 'Staged whitespace check'
    & $python -W ignore -m unittest discover -s tests -q
    Check-Exit 'Local tests'
    $env:PYTHON = $python
    $env:PLAYWRIGHT_MODULE = 'C:/Users/hp/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'
    & 'C:/Program Files/nodejs/node.exe' scripts/check_payroll_periods_ui.cjs
    Check-Exit 'Payroll browser tests'
    & 'C:/Program Files/nodejs/node.exe' scripts/check_thermal_receipt_ui.cjs
    Check-Exit 'Receipt browser tests'
    $staged = & git diff --cached --name-only
    Check-Exit 'List staged files'
    if ($staged) {
        Write-Host "Files to commit:`n$($staged -join "`n")"
        if ((Read-Host 'Type COMMIT to commit these staged files and push to GitHub') -cne 'COMMIT') { throw 'Cancelled.' }
        & git commit -m $Message
        Check-Exit 'Git commit'
    }
    $sha = (& git rev-parse HEAD).Trim()
    Check-Exit 'Read commit'
    if ($sha -notmatch '^[0-9a-f]{40}$') { throw 'Invalid commit SHA.' }
    & git push origin $branch
    Check-Exit 'GitHub push (do not force-push if rejected)'
    & $aws --version
    Check-Exit 'AWS CLI'
    if ($Login) {
        & $aws login --profile brownberries-cafe
        Check-Exit 'AWS sign-in'
    }
    $identityRaw = & $aws sts get-caller-identity @awsArgs --output json
    Check-Exit 'AWS identity: rerun with -Login if session expired'
    $identity = $identityRaw | ConvertFrom-Json
    if ($identity.Account -ne '774770453295') { throw 'Wrong AWS project. No deployment performed.' }
    Write-Host "GitHub commit: $sha`nAWS identity: $($identity.Arn)"
    if ((Read-Host 'Type DEPLOY to back up and briefly restart the AWS live application') -cne 'DEPLOY') { throw 'Deployment cancelled; GitHub push is retained.' }
    $shell = @'
set -eu
exec 9>/srv/brownberries/deploy.lock
flock -n 9 || { echo 'Another deployment is running'; exit 1; }
cd /srv/brownberries/app
target=__SHA__
previous=$(runuser -u brownberries -- git rev-parse HEAD)
echo "Rollback commit: $previous"
test -z "$(runuser -u brownberries -- git status --porcelain --untracked-files=no)"
runuser -u brownberries -- git fetch origin feature/explicit-menu-serving-hours
test "$(runuser -u brownberries -- git rev-parse origin/feature/explicit-menu-serving-hours)" = "$target"
runuser -u brownberries -- git merge-base --is-ancestor "$previous" "$target"
if test "$previous" = "$target"; then
  curl -fsS http://127.0.0.1:5050/healthz
  echo 'Requested commit already live.'
  exit 0
fi
if test -n "$(runuser -u brownberries -- git diff --name-only "$previous" "$target" -- 'requirements*' pyproject.toml uv.lock poetry.lock migrations)"; then
  echo 'Dependency or migration changes require a separately reviewed deployment.'
  exit 1
fi
preflight=$(mktemp -d /srv/brownberries/deploy-check.XXXXXX)
chown brownberries:brownberries "$preflight"
switched=0
success=0
cleanup() {
  result=$?
  trap - EXIT
  if test "$switched" = 1 && test "$success" = 0; then
    echo "Health/start failure: restoring code $previous (database is NOT restored)."
    systemctl stop brownberries.service || true
    if runuser -u brownberries -- git -C /srv/brownberries/app checkout --detach "$previous" && systemctl start brownberries.service; then
      echo 'Previous code restarted; verify health before retrying.'
    else
      echo 'ROLLBACK FAILED: operator intervention required.'
    fi
  fi
  runuser -u brownberries -- git -C /srv/brownberries/app worktree remove "$preflight" || true
  exit "$result"
}
trap cleanup EXIT
runuser -u brownberries -- git worktree add --detach "$preflight" "$target"
cd "$preflight"
PYTHONWARNINGS=ignore /srv/brownberries/app/.venv/bin/python -m unittest discover -s tests -q
started=$(date +%s)
systemctl start brownberries-backup.service
/srv/brownberries/app/.venv/bin/python -c 'import json,os,sys; p="/srv/brownberries/last-backup.json"; d=json.load(open(p)); assert d.get("verified") and os.path.getmtime(p)>=int(sys.argv[1]); print(d)' "$started"
cd /srv/brownberries/app
switched=1
systemctl stop brownberries.service
runuser -u brownberries -- git checkout --detach "$target"
systemctl start brownberries.service
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:5050/healthz && systemctl is-active --quiet brownberries-tunnel.service; then
    success=1
    echo "LIVE COMMIT: $(runuser -u brownberries -- git rev-parse HEAD)"
    exit 0
  fi
  sleep 2
done
echo 'Health check failed.'
exit 1
'@
    $shell = $shell.Replace('__SHA__', $sha).Replace("`r`n", "`n")
    $parameters = @{commands=@($shell);executionTimeout=@('900')} | ConvertTo-Json -Compress
    $command = & $aws ssm send-command --instance-ids $instance --document-name AWS-RunShellScript --parameters $parameters @awsArgs --query Command.CommandId --output text
    Check-Exit 'Submit deployment'
    $command = $command.Trim()
    Write-Host "Deployment command ID: $command`nDo not submit a second deployment while this is running."
    $deadline = (Get-Date).AddMinutes(18)
    do {
        Start-Sleep -Seconds 10
        $raw = & $aws ssm get-command-invocation --command-id $command --instance-id $instance @awsArgs --output json
        Check-Exit "Read deployment status. Remote command $command may still be running; inspect it before retrying"
        $run = $raw | ConvertFrom-Json
        Write-Host "AWS: $($run.Status)"
        if ((Get-Date) -gt $deadline) { throw "Polling timeout. Inspect command $command in AWS Systems Manager before retrying." }
    } while ($run.Status -in @('Pending','InProgress','Delayed','Cancelling'))
    Write-Host $run.StandardOutputContent
    Write-Host $run.StandardErrorContent
    if ($run.Status -ne 'Success') { throw "Deployment ended with $($run.Status). Read the output; do not assume the version changed." }
    $health = Invoke-RestMethod 'https://brownberriescafe.com/healthz' -TimeoutSec 30
    if (-not $health.ok) { throw 'Public health check failed; inspect the tunnel. No additional deployment was attempted.' }
    Write-Host "SUCCESS: GitHub and AWS verified for $sha. Backup and rollback commit are shown above."
} finally {
    Pop-Location
}
