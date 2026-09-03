<#
  setup_dashboard.ps1 — 대시보드 상시구동(부팅 자동) + tailnet 전용 노출 (VM, 관리자 권한)

  모의매매 페르소나 3종 비교 대시보드를 VM 에서 상시 띄우고, Tailscale 사설망 안에서만
  접근 가능하게 한다(공개 인터넷 노출 0). 폰/PC 에 Tailscale 깔고 같은 계정 로그인하면
  http://<VM_tailscale_IP>:<Port> 로 어디서나 본다.

  보안 3겹:
   · 방화벽 inbound <Port> 를 Tailscale 대역(100.64.0.0/10)만 허용 → 공개 IP 로는 차단.
   · control(매매·정지) 는 DASH_TOKEN 미설정 → 읽기전용(폰서 실수 매매 불가).
   · DASH_SITE_PASS(머신 env) 사이트 비번게이트(?k=<pass>). 필수 — server.py 가 비-loopback
     바인드(0.0.0.0)에 패스를 강제한다(fail-closed, dashboard/server.py:472). 미설정이면 아래 가드가 중단.

  사용 (VM, 관리자 PowerShell) — 비번게이트 값 설정이 선행 필수(새 셸에서 한 번):
    setx DASH_SITE_PASS "<비밀값>" /M
    powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\setup_dashboard.ps1
  해제:
    Unregister-ScheduledTask ustrade-dashboard -Confirm:$false
    Get-NetFirewallRule -DisplayName 'ustrade-dashboard' | Remove-NetFirewallRule
#>
[CmdletBinding()]
param(
  [string]$ProjRoot    = (Split-Path $PSScriptRoot -Parent),   # 기본 = 이 스크립트의 상위(= 레포 루트)
  [int]   $Port        = 8765,
  [string]$TailnetCidr = "100.64.0.0/10"   # Tailscale CGNAT 대역(모든 tailnet 피어 포함)
)
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_pyenv.ps1"   # $py = venv 우선(전역 python 은 핀 의존성 없음)
Assert-ProjRoot $ProjRoot      # 승격 전에 먼저 — OneDrive 자리표시자 경로면 여기서 중단

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { throw "관리자 권한 필요 — 관리자 PowerShell 에서 다시 실행" }

# server.py 는 비-loopback 바인드에 DASH_SITE_PASS 를 강제한다(fail-closed, dashboard/server.py:472).
# 미설정으로 태스크를 등록하면 크래시루프(RestartCount 3)만 남으므로 여기서 먼저 중단.
$pass = [Environment]::GetEnvironmentVariable("DASH_SITE_PASS","Machine")
if (-not $pass) { throw "DASH_SITE_PASS(머신 env) 미설정 — setx DASH_SITE_PASS `"<비밀값>`" /M 후 재실행" }

# 1) 방화벽 — inbound Port 를 tailnet 만 허용 (동명 룰 제거 후 재생성 = 멱등).
#    Windows 기본 inbound=차단이므로, 이 허용룰 외 공개 IP 는 자동 차단된다.
Get-NetFirewallRule -DisplayName "ustrade-dashboard" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "ustrade-dashboard" -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort $Port -RemoteAddress $TailnetCidr -Profile Any | Out-Null
Write-Host "방화벽: inbound TCP $Port 허용 = $TailnetCidr (tailnet) 만. 공개 IP 차단." -ForegroundColor Green

# 2) 부팅 자동구동 태스크 — server.py (0.0.0.0 바인딩, 무제한 실행, 크래시 시 재시작).
#    control(run/halt/resume) 은 DASH_TOKEN 미설정 → 읽기전용. DASH_SITE_PASS 는 머신 env 로 상속.
#    USTRADE_PERSONA_HOMES(setup_paper_tasks 가 설정한 머신 env)도 상속 → 모의거래 탭이 3 페르소나 읽음.
$inner = "`$env:DASH_HOST='0.0.0.0';`$env:DASH_PORT='$Port'; Set-Location '$ProjRoot'; & '$py' dashboard\server.py"
$act = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NonInteractive -WindowStyle Hidden -Command `"$inner`""
$trg = New-ScheduledTaskTrigger -AtStartup
$prn = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
# 배터리 플래그 — 기본값(DisallowStartIfOnBatteries=$true)이면 노트북 배터리 구동 시 Queued 로 대기.
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "ustrade-dashboard" -Action $act -Trigger $trg `
  -Principal $prn -Settings $set -Force | Out-Null
Write-Host "태스크 등록: ustrade-dashboard (부팅 자동구동, 무제한, 크래시 시 1분 간격 재시작)" -ForegroundColor Green

# 즉시 1회 기동 (포트 8765 가 비어있어야 함 — 수동 server.py 떠있으면 먼저 종료)
Start-ScheduledTask -TaskName "ustrade-dashboard"

$tsip = ""
try { $tsip = (& "C:\Program Files\Tailscale\tailscale.exe" ip -4 2>$null | Select-Object -First 1) } catch {}
if (-not $tsip) { $tsip = "<VM_tailscale_IP>" }
$url = "http://${tsip}:$Port"
Write-Host ""
Write-Host "완료. Tailscale 로그인된 폰/PC 브라우저로 접속:" -ForegroundColor Cyan
Write-Host "  $url/?k=<DASH_SITE_PASS>   (비번게이트 ON — 첫 접속만 ?k, 이후 쿠키)" -ForegroundColor Yellow
Write-Host "상태: Get-ScheduledTask ustrade-dashboard | Select TaskName,State" -ForegroundColor DarkGray
Write-Host "중지: Stop-ScheduledTask ustrade-dashboard   재시작: Start-ScheduledTask ustrade-dashboard" -ForegroundColor DarkGray
Write-Host "해제: Unregister-ScheduledTask ustrade-dashboard -Confirm:`$false; Get-NetFirewallRule -DisplayName 'ustrade-dashboard' | Remove-NetFirewallRule" -ForegroundColor DarkGray
