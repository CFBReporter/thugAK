<#
.SYNOPSIS
  Register (or re-register) the hourly thugsak agent cycle in Windows Task Scheduler.

.DESCRIPTION
  Fires scripts\run_agents.py --agent auto once an hour, 07:00 through 01:00
  local time (the machine is on Central; the project's whole clock is Central),
  which is 19 runs a day with the 02:00-06:00 dead hours skipped.

  Why Task Scheduler and not a daemon: each run is a fresh process, so a crash
  costs one cycle rather than the schedule, and it survives reboots. Local-time
  triggers also handle the DST fold for free.

  Settings that matter:
    -MultipleInstances IgnoreNew   a run that overlaps the next trigger is not
                                   started twice (the runner's lockfile is the
                                   belt to this brace -- two SQLite writers is
                                   corruption)
    -StartWhenAvailable            a laptop asleep at the trigger catches up on
                                   wake instead of skipping the hour
    -ExecutionTimeLimit 2h         a hung run is killed before it can hold the
                                   lock into the next cycle's stale window
    -RunOnlyIfNetworkAvailable     no network, no run; the cache's stale-serve
                                   would otherwise make an offline cycle look
                                   like "nothing changed"

  The task runs as the current user, interactive-or-not, because the Claude
  Agent SDK inherits this user's Claude Code credentials.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Unregister
#>
param(
    [switch]$Unregister,
    [string]$TaskName = "thugsak-agents-hourly"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Script = Join-Path $Root "scripts\run_agents.py"
$LogDir = Join-Path $Root "data\logs"

if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Unregistered $TaskName"
    exit 0
}

if (-not (Test-Path $Python)) { throw "venv python not found at $Python" }
if (-not (Test-Path $Script)) { throw "runner not found at $Script" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# One trigger per hour of the window. A single daily trigger with an hourly
# repetition cannot express "stop at 01:00 the next day" cleanly, and 19
# explicit triggers are easier to read in the Task Scheduler UI anyway.
$Hours = @(7..23) + @(0, 1)
$Triggers = foreach ($h in $Hours) {
    New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours($h))
}

# cmd /c so stdout+stderr land in a dated log file; Task Scheduler itself
# keeps no output.
# -u: unbuffered, so a run that hangs or is killed at the 2h limit still leaves
# its partial output in the log. Buffered, the first scheduled run wrote a
# 0-byte file until it exited.
$Cmd = "/c `"`"$Python`" -X utf8 -u `"$Script`" --agent auto >> `"$LogDir\agents-%DATE:~-4%-%DATE:~4,2%-%DATE:~7,2%.log`" 2>&1`""
$Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument $Cmd -WorkingDirectory $Root

$Settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RunOnlyIfNetworkAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Triggers `
    -Settings $Settings -Principal $Principal -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Host "Registered $TaskName  state=$($t.State)  triggers=$($t.Triggers.Count)"
Write-Host "Next run: $((Get-ScheduledTaskInfo -TaskName $TaskName).NextRunTime)"
Write-Host "Logs:     $LogDir"
Write-Host ""
Write-Host "Run it now to test:   Start-ScheduledTask -TaskName $TaskName"
Write-Host "Read the log:         Get-Content $LogDir\agents-*.log -Tail 40"
