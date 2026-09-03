<#
  setup_autopull.ps1 - register vm_autopull as a recurring scheduled task (VM, admin, RUN ONCE).

  After this runs, the VM keeps itself in sync: push from the PC with deploy_push.ps1 and the VM
  pulls + restarts the dashboard on its own within the poll interval. No more manual vm_update on VM.

  Usage (VM, elevated PowerShell):
    powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\setup_autopull.ps1
    powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\setup_autopull.ps1 -IntervalMin 5
  Remove:
    Unregister-ScheduledTask ustrade-autopull -Confirm:$false

  Runs as the CURRENT user via S4U (no stored password, runs whether logged on or not). The current
  user must be the one whose git auth (SSH key / credential) works - i.e. the account you normally
  run vm_update.ps1 from. The autopull.last heartbeat below confirms auth works under the task.
#>
[CmdletBinding()]
param([int]$IntervalMin = 10, [string]$ProjRoot = "C:\ustrade")
$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { throw "admin required - rerun in an elevated PowerShell" }

$script = Join-Path $ProjRoot 'tools\vm_autopull.ps1'
if (-not (Test-Path $script)) { throw "not found: $script  (run vm_update.ps1 first to pull it)" }

$me  = [Security.Principal.WindowsIdentity]::GetCurrent().Name   # e.g. EC2AMAZ-XXX\Administrator
$act = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$trg = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMin)
$prn = New-ScheduledTaskPrincipal -UserId $me -LogonType S4U -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName "ustrade-autopull" -Action $act -Trigger $trg `
  -Principal $prn -Settings $set -Force | Out-Null
Write-Host "registered: ustrade-autopull (every $IntervalMin min, runs as $me via S4U)" -ForegroundColor Green

# fire once now, then show the heartbeat so we can confirm git auth works under the task principal
Start-ScheduledTask -TaskName "ustrade-autopull"
Start-Sleep -Seconds 10
$last = Join-Path $ProjRoot 'logs\autopull.last'
$log  = Join-Path $ProjRoot 'logs\autopull.log'
Write-Host ""
Write-Host "--- autopull.last (heartbeat) ---" -ForegroundColor Cyan
if (Test-Path $last) { Get-Content $last } else { Write-Host "(not written yet - task may still be starting; re-check in a few sec)" -ForegroundColor Yellow }
Write-Host "--- autopull.log (tail) ---" -ForegroundColor Cyan
if (Test-Path $log) { Get-Content $log -Tail 8 } else { Write-Host "(empty - normal if already up to date)" }
Write-Host ""
Write-Host "EXPECT: heartbeat shows 'fetch=ok as $me'. If 'fetch=FAIL', git auth is missing under S4U - tell Claude." -ForegroundColor Yellow
Write-Host "status: Get-ScheduledTask ustrade-autopull | Select TaskName,State" -ForegroundColor DarkGray
Write-Host "remove: Unregister-ScheduledTask ustrade-autopull -Confirm:`$false" -ForegroundColor DarkGray
