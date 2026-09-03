<#
  setup_paper_tasks.ps1 — 모의매매 페르소나 4종 상시운영 태스크 등록 (VM, 관리자 권한)

  실거래 봇은 그대로 정지 유지. 서로 다른 전략의 모의매매자 4명을 매 세션 돌려서:
    - 전략별 파이프라인 시스템 점검 (크래시·stale·정합성)
    - selection_review 가 "어느 전략이 돈 됐나" 비교 데이터 누적
    - 소수주(fractional) 실행경로 실시장가 검증 + 다일 진화(책 디스크 영속)

  페르소나 (자본 $100,000 소수주, 전략·유니버스는 personas.py 권위):
    ustrade-paper-buffett      — 워런버핏형 가치·우량 (전용 가치 스크린, FMP)
    ustrade-paper-wood         — 캐시우드형 파괴성장 (고P/S·저배당·모멘텀, FMP)
    ustrade-paper-oneil        — 오닐 CANSLIM 성장 (A엔진 — 봇 실거래 전략 검증)
    ustrade-paper-buffett_v2   — 버핏 v2 (ROIC·섹터중립) — buffett 과 12주 A/B 실험군
    ustrade-paper-canslim_rdcf — 오닐 CANSLIM + 역DCF 밸류틸트 — oneil 과 12주 A/B 실험군

  ⚠️ A/B 무결성: buffett↔buffett_v2, oneil↔canslim_rdcf 각 짝은 **같은 머신·같은 트리거 기준**으로
     돌아야 한다. 한쪽만 다른 PC 에 등록하면 시계·네트워크·캐시 온도가 변인으로 섞여 12주가 무효.

  격리: 페르소나마다 별도 home(C:\ustrade-paper-<name>) → 책·락·킬스위치 독립(동시 실행 안전).
        캐시(가격·FMP)는 -CacheHome 공유 → 중복 다운로드·FMP 레이트 절감.
        review·대시보드 HALT 순회가 USTRADE_PERSONA_HOMES(머신 env)로 4 home 을 합쳐 본다
        (아래 $homes 병합 — 신규 페르소나는 여기 등록만으로 HALT·리뷰·대시보드에 자동 편입).
  알림은 기본 음소거(노이즈 차단; 실패는 종료코드·저널로 포착). 켜려면 -MutePaper:$false.

  사용 (VM, 관리자 PowerShell):
    powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\setup_paper_tasks.ps1
    powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\setup_paper_tasks.ps1 -At 05:10   # entry 없을 때만
  해제:
    Get-ScheduledTask ustrade-paper-* | Unregister-ScheduledTask -Confirm:$false
    [Environment]::SetEnvironmentVariable('USTRADE_PERSONA_HOMES',$null,'Machine')
#>
[CmdletBinding()]
param(
  [string]$At        = "",                  # ustrade-entry 미존재 시 일일 실행시각 (HH:mm, 로컬)
  [string]$ProjRoot  = (Split-Path $PSScriptRoot -Parent),   # 기본 = 이 스크립트의 상위(= 레포 루트)
  [string]$CacheHome = "C:\ustrade-data",   # 공유 캐시 루트(가격·FMP) — 기본 home 의 따뜻한 캐시 재사용
  [bool]$MutePaper   = $true
)
$ErrorActionPreference = "Stop"

. "$PSScriptRoot\_pyenv.ps1"   # $py = venv 우선(전역 python 은 핀 의존성 없음)
Assert-ProjRoot $ProjRoot      # 승격 전에 먼저 — OneDrive 자리표시자 경로면 여기서 중단

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) { throw "관리자 권한 필요 — 관리자 PowerShell 에서 다시 실행" }

# 알림 음소거 prefix — 텔레그램·슬랙 env 비우기 (has_channel False → paper notify no-op, HTTP·flag 없음)
$mute = ""
if ($MutePaper) { $mute = "`$env:TELEGRAM_BOT_TOKEN='';`$env:TELEGRAM_CHAT_ID='';`$env:SLACK_WEBHOOK_URL='';" }

$personas = @(
  @{ name = "buffett";      label = "워런버핏형 가치·우량" },
  @{ name = "wood";         label = "캐시우드형 파괴성장" },
  @{ name = "oneil";        label = "오닐 CANSLIM 성장" },
  @{ name = "buffett_v2";   label = "버핏 v2(ROIC·섹터중립)" },
  @{ name = "canslim_rdcf"; label = "오닐 CANSLIM + 역DCF 밸류틸트" }
)

function New-PersonaAction([string]$persona, [string]$homeDir) {
  # 별 home(상태·책·로그 격리) + 공유 캐시 + FMP 무료티어 호출최소화 + 음소거 후 run_live --persona 실행.
  # 실측: 402 가 호출간격 무관(4s 75%·8s 62%) = 분당버스트 아닌 일일쿼터 한계. 스페이싱 무용 →
  # FMP_RETRY_402=0(402 즉시스킵, 종목당 1콜=쿼터낭비0; 스킵분은 7일캐시로 다음 런이 채움),
  # FMP_MIN_INTERVAL=2(완만, 잔여 버스트 보험). 정상운영=1일1런 → buffett+wood ~50콜, 캐시 데워지면 ~0.
  # buffett_v2 는 v1 과 같은 pool 이라 ratios·key-metrics 는 캐시 재사용, profile(섹터)만 +20콜
  # 콜드(30일 TTL·섹터는 준정적이라 사실상 월 1회). 무료티어 일일한도 대비 무시 가능.
  $envp = "`$env:USTRADE_HOME='$homeDir';`$env:USTRADE_CACHE_HOME='$CacheHome';`$env:FMP_MIN_INTERVAL='2';`$env:FMP_RETRY_402='0';`$env:FMP_CACHE_TTL_DAYS='30';"
  $inner = "$envp$mute Set-Location '$ProjRoot'; & '$py' run_live.py --persona $persona"
  New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NonInteractive -WindowStyle Hidden -Command `"$inner`""
}

# 트리거·실행계정: ustrade-entry 복제 (별 home=별 락 → 동시 안전). 단 FMP 공유쿼터라 페르소나별 +오프셋
# 스태거(아래 $offsetMin) → 동시 버스트 429 회피 + 공유캐시 점진 워밍(먼저 뜬 페르소나가 겹치는 종목 캐싱).
$entry = Get-ScheduledTask -TaskName "ustrade-entry" -ErrorAction SilentlyContinue
if ($entry) {
  $trigger   = $entry.Triggers
  $principal = $entry.Principal
  Write-Host "트리거·계정 = ustrade-entry 복제 (실거래와 동일 세션 타이밍)" -ForegroundColor Cyan
} elseif ($At) {
  $trigger   = New-ScheduledTaskTrigger -Daily -At $At
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
  Write-Host "ustrade-entry 없음 → -Daily $At / SYSTEM 계정" -ForegroundColor Yellow
} else {
  throw "ustrade-entry 태스크 없음 + -At 미지정 — 실행시각을 -At HH:mm 로 주거나 entry 먼저 등록"
}

# 배터리 플래그 필수 — New-ScheduledTaskSettingsSet 기본값은 DisallowStartIfOnBatteries=$true 라
# 노트북이 배터리 구동이면 태스크가 실행되지 않고 Queued 로 대기한다(2026-07-27 실증: State=Queued,
# LastRunTime 없음, home·로그 미생성). 데스크탑에선 드러나지 않는 조건.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -ExecutionTimeLimit (New-TimeSpan -Hours 1)

# 구버전(generic mirror/big) 정리 — 페르소나로 대체
Get-ScheduledTask -TaskName "ustrade-paper", "ustrade-paper-big" -ErrorAction SilentlyContinue |
  Unregister-ScheduledTask -Confirm:$false
Write-Host "구버전 ustrade-paper / ustrade-paper-big 제거(있으면)" -ForegroundColor DarkGray

# buffett·wood·oneil·buffett_v2·canslim_rdcf 순 스태거(분) — FMP 동시버스트 회피. 콜드페치 ~1분<8분 간격이라 무충돌.
# buffett_v2(+24)·canslim_rdcf(+32) 를 뒤에 둔 이유: 선행 페르소나가 공유캐시(ratios·key-metrics)를
# 데워두면 뒤 페르소나는 추가 콜만 낸다(v2=profile 섹터, rdcf=key-metrics 의 fcf_yield·marketCap).
# canslim_rdcf 는 sp500 유니버스라 buffett/oneil(둘 다 sp500)이 데운 캐시와 종목이 겹쳐 콜드콜 최소.
$offsetMin = @(0, 8, 16, 24, 32)
$homes = @()
for ($i = 0; $i -lt $personas.Count; $i++) {
  $p = $personas[$i]
  $off = $offsetMin[$i]
  $homeDir = "C:\ustrade-paper-$($p.name)"
  $homes += $homeDir
  $act = New-PersonaAction $p.name $homeDir
  # 페르소나별 트리거 = 기준 + 오프셋. entry 복제본을 매번 새로 떠 StartBoundary 시각만 이동(주간/평일 재발 유지).
  $trg = $trigger
  if ($off -gt 0) {
    if ($entry) {
      $trg = (Get-ScheduledTask -TaskName "ustrade-entry").Triggers
      foreach ($t in $trg) {
        if ($t.StartBoundary) {
          try { $t.StartBoundary = ([datetimeoffset]$t.StartBoundary).AddMinutes($off).ToString("yyyy-MM-ddTHH:mm:sszzz") } catch {}
        }
      }
    } elseif ($At) {
      $trg = New-ScheduledTaskTrigger -Daily -At ([datetime]$At).AddMinutes($off)
    }
  }
  Register-ScheduledTask -TaskName "ustrade-paper-$($p.name)" -Action $act -Trigger $trg `
    -Principal $principal -Settings $settings -Force | Out-Null
  # ${off} 중괄호 필수 — 한글은 PowerShell 식별자로 유효해 "$off분" 은 변수 `off분`(미정의)로 파싱된다.
  Write-Host "등록: ustrade-paper-$($p.name)  ($($p.label), +${off}분 스태거, home=$homeDir)" -ForegroundColor Green
}

# review·대시보드가 페르소나 home 들을 합쳐 읽도록 머신 env 설정 (SYSTEM 태스크가 읽음).
# 기존값과 *병합* — setup_intraday.ps1 이 추가한 intraday 전용 home(livermore)을 덮어쓰지 않게.
$existing = [Environment]::GetEnvironmentVariable("USTRADE_PERSONA_HOMES", "Machine")
$merged = @($homes)
if ($existing) { $merged += ($existing.Split(";") | Where-Object { $_.Trim() -and $homes -notcontains $_.Trim() }) }
[Environment]::SetEnvironmentVariable("USTRADE_PERSONA_HOMES", ($merged -join ";"), "Machine")
Write-Host "USTRADE_PERSONA_HOMES = $($merged -join ';')" -ForegroundColor Cyan

Write-Host "`n완료. 실거래 봇 변동 없음(여전히 정지). 다음 세션부터 4 페르소나 자동 실행·진화." -ForegroundColor Green
Write-Host "즉시 1회 스모크: Start-ScheduledTask ustrade-paper-buffett  (wood·oneil·buffett_v2 동일)" -ForegroundColor Cyan
Write-Host "  확인: Get-Content C:\ustrade-paper-buffett\logs\runs.jsonl -Tail 1" -ForegroundColor DarkGray
Write-Host "책 리셋(페르소나 초기화): Remove-Item C:\ustrade-paper-<name>\state\paper_book_<name>.json" -ForegroundColor DarkGray
Write-Host "비교 리포트: review 작업이 생성 → C:\ustrade-data\logs\selection_review\<date>_h20.md (페르소나 차원)" -ForegroundColor DarkGray
Write-Host "상태: Get-ScheduledTask ustrade-paper-* | Select TaskName,State" -ForegroundColor DarkGray
Write-Host "해제: Get-ScheduledTask ustrade-paper-* | Unregister-ScheduledTask -Confirm:`$false" -ForegroundColor DarkGray
