<#
  setup_intraday.ps1 — 장중 액티브 트레이딩 루프 상시 태스크 (VM, 관리자 권한)

  run_intraday.py 를 장중 기동한다. 장 마감(16:00 ET)에 자가 종료(market_is_open 게이트).
  두 태스크로 분리(진입 타이밍이 다름) — --only 목록은 personas.py 에서 동적 산출(신규 페르소나 자동 배선):
   · ustrade-intraday       : intraday+daily_run(oneil·wood, 일1런 공유책) — 일1런 후 기동
                              (트리거=ustrade-entry 복제 +지연, 공유책 last-writer-wins 회피).
   · ustrade-intraday-open  : intraday 장중전용(livermore·chartist·*_ctl 대조군) — *개장(09:30 ET)부터*
                              기동. livermore ORB 는 개장 레인지 앵커가 필요해 개장근처 시작 필수
                              (늦게 뜨면 ORB 게이트가 상시 스킵). VM=UTC → 13:30 UTC=09:30 EDT.

  안전:
   · paper 전용 — 체결은 PaperBroker, 호가만 TossQuoteClient(주문 메서드 부재). 실주문 0.
   · Toss 자격증명은 이 헤드리스·비청취 SYSTEM 태스크 env(머신 env 상속)에만 — 대시보드 0.
   · 페르소나(책)별 락(persona_home\state\intraday.lock) — 두 태스크는 서로 다른 페르소나라 동시 가동 무해, 같은 페르소나 이중기동은 차단.

  사용 (VM, 관리자 PowerShell):
    powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\setup_intraday.ps1
  해제:
    Unregister-ScheduledTask ustrade-intraday,ustrade-intraday-open -Confirm:$false
#>
[CmdletBinding()]
param(
  [string]$ProjRoot  = (Split-Path $PSScriptRoot -Parent),   # 기본 = 이 스크립트의 상위(= 레포 루트)
  [string]$DelayMin  = "10"          # 개장 후 지연(분) — 일1런 선정·초기매수 완료 대기(책 레이스 회피)
)
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_pyenv.ps1"   # $py = venv 우선(전역 python 은 핀 의존성 없음)
Assert-ProjRoot $ProjRoot      # 승격 전에 먼저 — OneDrive 자리표시자 경로면 여기서 중단

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { throw "관리자 권한 필요 — 관리자 PowerShell 에서 다시 실행" }

# 트리거·계정 = ustrade-entry 복제(실거래와 동일 세션 타이밍) + Delay 스태거. 없으면 -Daily 폴백.
$entry = Get-ScheduledTask -TaskName "ustrade-entry" -ErrorAction SilentlyContinue
if ($entry) {
  $trigger   = $entry.Triggers
  $principal = $entry.Principal
  # 소프트 스태거 — daily/time 트리거는 .Delay 무시, .RandomDelay 만 적용됨(0~N분 랜덤 지연).
  # 책 레이스의 *실제* 방어는 run_intraday._await_daily_runs(일1런 run.lock 떴다사라짐 대기) + book_lock
  # 직렬화이고, 이 랜덤지연은 개시 시점만 분산하는 보조 수단(부팅/로그온 트리거면 .Delay 도 시도).
  try { $trigger[0].RandomDelay = "PT${DelayMin}M" } catch { }
  try { $trigger[0].Delay = "PT${DelayMin}M" } catch { }
  Write-Host "트리거·계정 = ustrade-entry 복제 + ~${DelayMin}분 랜덤지연(보조; 순서보장은 _await_daily_runs)" -ForegroundColor Cyan
} else {
  $trigger   = New-ScheduledTaskTrigger -Daily -At "22:40"   # KST ≈ 09:40 ET(EDT) 폴백 — DST 시 -At 조정
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Write-Host "ustrade-entry 없음 → -Daily 22:40 / SYSTEM (DST 시 시간 조정 필요)" -ForegroundColor Yellow
}

# 루프는 장마감 자가종료 — 안전망으로 8h 상한(개장~마감 6.5h + 여유). --only별 락 분리로 동시가동 무해.
# 배터리 플래그 — 기본값(DisallowStartIfOnBatteries=$true)이면 노트북 배터리 구동 시 Queued 로 대기.
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 8)

# --only 목록 = personas.py 동적 산출 — daily(일1런 공유책) / open(장중전용, 대조군 포함).
# personas import 실패 시 폴백 하드코딩(구버전 동작 — 대조군은 다음 재실행서 합류).
$dailyOnly = "oneil,wood"; $openOnly = "livermore,chartist"
try {
  Push-Location $ProjRoot
  $d = & $py -c "import personas;print(','.join(sorted(n for n,m in personas.PERSONAS.items() if m.get('intraday') and m.get('daily_run'))))"
  $o = & $py -c "import personas;print(','.join(sorted(n for n,m in personas.PERSONAS.items() if m.get('intraday') and not m.get('daily_run'))))"
  Pop-Location
  if ($d) { $dailyOnly = $d.Trim() }
  if ($o) { $openOnly = $o.Trim() }
} catch { Write-Host "personas 산출 실패 — --only 폴백($dailyOnly / $openOnly): $_" -ForegroundColor Yellow }

# (1) 일1런 공유책(oneil·wood) — 일1런 후 기동(트리거=ustrade-entry 복제 + 지연). 개장 늦어도 무해(ORB 불사용).
$innerDaily = "Set-Location '$ProjRoot'; & '$py' run_intraday.py --only $dailyOnly"
$actDaily = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NonInteractive -WindowStyle Hidden -Command `"$innerDaily`""
Register-ScheduledTask -TaskName "ustrade-intraday" -Action $actDaily -Trigger $trigger `
  -Principal $principal -Settings $set -Force | Out-Null
Write-Host "등록: ustrade-intraday ($dailyOnly, 일1런 후 기동)" -ForegroundColor Green

# (2) 장중전용(livermore·chartist·대조군) — 일1런 조율 불요. *개장부터* 기동해야 ORB 앵커가 실개장레인지 잡음.
#     VM=UTC → 13:30 UTC=09:30 EDT. 겨울(EST)엔 08:30 ET로 떠도 run_intraday._wait_until_open 이 개장까지 대기.
$openTrigger = New-ScheduledTaskTrigger -Daily -At "13:30"
$innerOpen = "Set-Location '$ProjRoot'; & '$py' run_intraday.py --only $openOnly"
$actOpen = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NonInteractive -WindowStyle Hidden -Command `"$innerOpen`""
Register-ScheduledTask -TaskName "ustrade-intraday-open" -Action $actOpen -Trigger $openTrigger `
  -Principal $principal -Settings $set -Force | Out-Null
Write-Host "등록: ustrade-intraday-open ($openOnly, 개장 09:30 ET~ / 13:30 UTC)" -ForegroundColor Green

# 장중전용(intraday=True, 일1런 없음) 페르소나 home 을 USTRADE_PERSONA_HOMES 에 병합 → 대시보드·리뷰 인식.
# personas.py 에서 동적 산출(향후 장중전용 페르소나 추가 시 자동 등록) — livermore·chartist 등.
$intradayOnly = @()
try {
  Push-Location $ProjRoot
  $names = & $py -c "import personas;print(';'.join(n for n,m in personas.PERSONAS.items() if m.get('intraday') and not m.get('daily_run')))"
  Pop-Location
  if ($names) { $intradayOnly = $names.Trim().Split(";") | Where-Object { $_.Trim() } }
} catch { Write-Host "personas 산출 실패 — 폴백 목록 사용: $_" -ForegroundColor Yellow }
if (-not $intradayOnly) { $intradayOnly = @("livermore", "chartist") }   # 폴백(personas import 실패 대비)

$cur = [Environment]::GetEnvironmentVariable("USTRADE_PERSONA_HOMES", "Machine")
$homes = @()
if ($cur) { $homes = $cur.Split(";") | Where-Object { $_.Trim() } }
$added = @()
foreach ($n in $intradayOnly) {
  $h = "C:\ustrade-paper-$n"
  if ($homes -notcontains $h) { $homes += $h; $added += $h }
}
if ($added.Count -gt 0) {
  [Environment]::SetEnvironmentVariable("USTRADE_PERSONA_HOMES", ($homes -join ";"), "Machine")
  Write-Host "USTRADE_PERSONA_HOMES 에 장중전용 home 병합: $($added -join ', ')" -ForegroundColor Cyan
} else {
  Write-Host "USTRADE_PERSONA_HOMES 이미 장중전용($($intradayOnly -join ', ')) 포함" -ForegroundColor DarkGray
}

Write-Host "`n완료. 실거래 봇 변동 없음(여전히 정지). livermore·chartist=개장부터, oneil·wood=일1런 후 가동." -ForegroundColor Green
Write-Host "즉시 1틱 스모크: & '$py' $ProjRoot\run_intraday.py --once --ignore-hours --only livermore,chartist" -ForegroundColor Cyan
Write-Host "상태: Get-ScheduledTask ustrade-intraday,ustrade-intraday-open | Select TaskName,State" -ForegroundColor DarkGray
Write-Host "로그: Get-Content C:\ustrade-paper-livermore\logs\intraday.jsonl -Tail 5" -ForegroundColor DarkGray
Write-Host "해제: Unregister-ScheduledTask ustrade-intraday,ustrade-intraday-open -Confirm:`$false" -ForegroundColor DarkGray
