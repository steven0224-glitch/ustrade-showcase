<#
  vm_update.ps1  —  VM 쪽 "업데이트 버튼" (C:\ustrade 에서 실행)
  GitHub 에서 최신 코드 pull. 봇이 도는 중이면 막고, 의존성 바뀌면 알려줌.

  사용 (RDP 접속 후 VM PowerShell):
    pwsh C:\ustrade\tools\vm_update.ps1
    pwsh C:\ustrade\tools\vm_update.ps1 -Force   # 실행중 프로세스 무시하고 강제

  안전장치:
    - run_live/run_exit/review/heartbeat 파이썬이 도는 중이면 중단(-Force 로 무시).
    - --ff-only: 충돌나는 머지 대신 멈춤(VM 에서 로컬수정 했단 뜻 → 사람이 판단).
    - 스케줄 태스크는 매 실행마다 새 파이썬으로 코드를 다시 읽음 → pull 후 재시작 불필요.
#>
[CmdletBinding()]
param([switch]$Force)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

# 1) 봇 실행중 가드 — pull 도중 태스크 발사되면 반쯤 바뀐 코드 읽을 수 있음
$busy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'run_live|run_exit|review\.py|heartbeat|panic_exit' }
if ($busy -and -not $Force) {
    Write-Host "봇 실행중 → 업데이트 보류 (장중일 수 있음). 끝나고 다시 / 급하면 -Force." -ForegroundColor Yellow
    $busy | ForEach-Object { Write-Host "    PID $($_.ProcessId): $($_.CommandLine)" }
    exit 1
}

# 2) fetch + 받을 것 확인
#    git 은 정상 진행도 stderr 로 출력 → PS5.1 + EAP=Stop 에서 2>&1 가 종료에러로 오인해 fetch 에서
#    중단되던 버그. native git 네트워크 구간만 Continue 로 낮추고 $LASTEXITCODE 로 성공판정.
Write-Host "[1/3] fetch..." -ForegroundColor Cyan
$eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& git -C $root fetch origin 2>&1 | Out-Host
$fetchCode = $LASTEXITCODE
$ErrorActionPreference = $eap
if ($fetchCode -ne 0) {
    Write-Host "fetch 실패 (네트워크/원격 확인). exit $fetchCode" -ForegroundColor Red
    exit 1
}
$before  = & git -C $root rev-parse HEAD
$incoming = & git -C $root log --oneline HEAD..origin/main
if (-not $incoming) {
    Write-Host "이미 최신 ($($before.Substring(0,7))). 받을 것 없음." -ForegroundColor Green
    exit 0
}
Write-Host "받을 커밋:" -ForegroundColor Cyan
$incoming | ForEach-Object { Write-Host "    $_" }

# requirements 변경 여부를 pull 전에 진단
$reqChanged = & git -C $root diff --name-only HEAD origin/main -- requirements_vm.txt requirements.txt

# 3) pull (fast-forward only)
Write-Host "[2/3] pull --ff-only..." -ForegroundColor Cyan
$eap2 = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& git -C $root pull --ff-only origin main 2>&1 | Out-Host
$pullCode = $LASTEXITCODE
$ErrorActionPreference = $eap2
if ($pullCode -ne 0) {
    Write-Host "pull 실패 (ff 불가 = VM 로컬수정 충돌). 수동 확인 필요:" -ForegroundColor Red
    Write-Host "    git -C `"$root`" status" -ForegroundColor Red
    exit 1
}
$after = & git -C $root rev-parse HEAD
Write-Host "  $($before.Substring(0,7)) → $($after.Substring(0,7))" -ForegroundColor Green

# 4) 의존성 바뀌었으면 설치
if ($reqChanged) {
    Write-Host "[3/3] requirements 변경 감지 → pip install..." -ForegroundColor Cyan
    python -m pip install -r (Join-Path $root 'requirements_vm.txt')
} else {
    Write-Host "[3/3] requirements 변경 없음 — pip 생략." -ForegroundColor Green
}

Write-Host "`n완료. 다음 스케줄 실행부터 새 코드 적용 (재시작 불필요)." -ForegroundColor Green
Write-Host "※ canslim A엔진(C:\텔레그램_시그널_알리미\engine\)은 이 repo 밖 — 거기 바뀌면 별도 복사." -ForegroundColor DarkGray
