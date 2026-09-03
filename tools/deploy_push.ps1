<#
  deploy_push.ps1  —  PC 쪽 "배포 버튼"
  변경 → (테스트 게이트) → 커밋 → GitHub push.  VM 은 vm_update.ps1 로 받아감.

  사용:
    pwsh tools\deploy_push.ps1 "커밋 메시지"            # 테스트 통과해야 push
    pwsh tools\deploy_push.ps1 "급한 핫픽스" -SkipTests # 테스트 건너뜀(비권장)
    pwsh tools\deploy_push.ps1 "wip" -NoPush           # 커밋만, push 안 함
#>
[CmdletBinding()]
param(
    # Mandatory 금지 — 비대화 셸(SSH/에이전트/스케줄러)에서 누락 시 에러 대신 입력 프롬프트
    # *무한 대기*(출력 0줄, 2026-07-07 실증). 커밋할 변경이 있을 때만 아래에서 명시 검사.
    [Parameter(Position = 0)]
    [string]$Message,
    [switch]$SkipTests,
    [switch]$NoPush
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent

# 0) 게이트 인터프리터 — 핀 의존성이 설치된 venv 를 쓴다(_pyenv.ps1 이 결정).
#    구버전은 전역 python + 사용자 site 를 PYTHONPATH 에 주입했으나, 사용자 site 에는
#    yfinance 등이 애초에 없어 게이트가 RC=1 로 죽었다(2026-07-27 실측). 주입 제거.
. "$PSScriptRoot\_pyenv.ps1"

# 1) 테스트 게이트 — 실거래 코드라 회귀 깨지면 push 막음 (pytest 불필요)
if (-not $SkipTests) {
    Write-Host "[1/4] 테스트 게이트 (전 스위트, $py)..." -ForegroundColor Cyan
    Push-Location $root
    try { & $py tools\run_tests.py } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "테스트 실패 → push 중단. 고치고 다시." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[1/4] 테스트 건너뜀 (-SkipTests)" -ForegroundColor Yellow
}

# 2) 스테이징 (.gitignore 가 state/·logs/·비밀 제외)
Write-Host "[2/4] 변경 스테이징..." -ForegroundColor Cyan
& git -C $root add -A
$staged = & git -C $root diff --cached --name-only
if (-not $staged) {
    # 변경 없음이어도 이미 커밋된 미push 분(ahead)이 있으면 push 진행 — 예전엔 여기서
    # exit 0 해 수동커밋 후 스크립트 호출 시 push 를 조용히 건너뛰었다(배포 안 됨).
    $ahead = [int](& git -C $root rev-list --count 'origin/main..HEAD')
    if ($ahead -gt 0) {
        Write-Host "  변경 없음 — 미push 커밋 $ahead 개 → push 로 진행." -ForegroundColor Yellow
    } else {
        Write-Host "변경도 미push 커밋도 없음 — 할 것 없음." -ForegroundColor Yellow
        exit 0
    }
} else {
    Write-Host ("  {0}개 파일:" -f $staged.Count)
    $staged | ForEach-Object { Write-Host "    $_" }
    if (-not $Message) {
        Write-Host "커밋 메시지 필요: pwsh tools\deploy_push.ps1 `"메시지`"  (스테이징 롤백: git reset)" -ForegroundColor Red
        exit 1
    }
    # 3) 커밋
    Write-Host "[3/4] 커밋..." -ForegroundColor Cyan
    & git -C $root commit -q -m $Message
    # 종료코드 확인 필수 — 확인 없이 진행하면 커밋 실패(예: user.email 미설정)에도 다음 단계가
    # "Everything up-to-date" 로 통과해 **완료(push) 로 오보**된다(2026-07-27 실측, 새 clone 에서 재현).
    if ($LASTEXITCODE -ne 0) {
        Write-Host "커밋 실패 → 중단. 스테이징은 유지됨(롤백: git -C $root reset)." -ForegroundColor Red
        exit 1
    }
    & git -C $root log --oneline -1
}

# 4) push
if ($NoPush) {
    Write-Host "[4/4] -NoPush → push 생략. 나중에: git -C `"$root`" push" -ForegroundColor Yellow
    exit 0
}
Write-Host "[4/4] GitHub push..." -ForegroundColor Cyan
& git -C $root push origin main
if ($LASTEXITCODE -ne 0) { Write-Host "push 실패." -ForegroundColor Red; exit 1 }

Write-Host "`n[5/5] VM 즉시 동기화 (SSH autopull 트리거)..." -ForegroundColor Cyan
# push 직후 VM 을 바로 당김 — autopull 1회 실행(fetch+pull+대시보드 재기동). 실패(VM offline·tailnet
# 끊김)해도 push 는 이미 성공 → 비치명적. 안 당겨져도 ustrade-autopull 태스크(10분 주기)가 백업.
$VMHost = "Administrator@<vm-tailscale-ip>"  # tailnet VM (<vm-host>, 2026-07-21 클린 재건축)
$eapV = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
# ssh 는 반드시 Windows OpenSSH 를 명시 — PATH 1순위인 Git 의 MSYS ssh 는 한글 사용자폴더(<you>)를
# CP949 로 오독해 %USERPROFILE%\.ssh\known_hosts 를 못 읽음(호스트키 영영 못 찾아 GUI TOFU 프롬프트/
# BatchMode 시 조용히 실패). System32 OpenSSH 는 USERPROFILE 로 홈을 바로 잡아 정상. accept-new =
# 최초 미지의 키만 조용히 저장(재건축 대비), 바뀐 키는 거부(MITM 방어).
$SSH = if (Test-Path "$env:SystemRoot\System32\OpenSSH\ssh.exe") { "$env:SystemRoot\System32\OpenSSH\ssh.exe" } else { "ssh" }
$sshOpts = @("-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10")
# 클린 재건축 VM(07-21)엔 pwsh 미설치 — Windows PowerShell 5.1 로 호출 (vm_*.ps1 은 5.1 호환·BOM)
# ssh.exe 가 원격 작업 완료 후에도 세션을 안 닫고 무한 대기하는 좀비 버그 실증(1시간·7.5시간
# 무응답, 백그라운드 실행에서 재현) — Start-Process 로 직접 띄워 WaitForExit(ms) 로 로컬
# 타임아웃을 걸고, 초과 시 프로세스(트리 포함) 를 강제 종료해 좀비를 남기지 않는다.
function Invoke-SshTimeout {
    param([string[]]$ArgList, [int]$TimeoutMs)
    $outFile = [IO.Path]::GetTempFileName(); $errFile = [IO.Path]::GetTempFileName()
    try {
        $proc = Start-Process -FilePath $SSH -ArgumentList $ArgList -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        $exited = $proc.WaitForExit($TimeoutMs)
        if (-not $exited) { try { $proc.Kill($true) } catch {} }  # $true = 프로세스 트리까지 종료
        $out = (Get-Content $outFile -Raw -ErrorAction SilentlyContinue) + (Get-Content $errFile -Raw -ErrorAction SilentlyContinue)
        [pscustomobject]@{ TimedOut = -not $exited; ExitCode = if ($exited) { $proc.ExitCode } else { -1 }; Output = $out }
    } finally {
        Remove-Item $outFile, $errFile -ErrorAction SilentlyContinue
    }
}

$trigger = Invoke-SshTimeout -TimeoutMs 90000 -ArgList ($sshOpts + @($VMHost, "powershell -NoProfile -ExecutionPolicy Bypass -File C:\ustrade\tools\vm_autopull.ps1"))
if ($trigger.Output) { Write-Host $trigger.Output }
if ($trigger.TimedOut) {
    Write-Host "완료(push). SSH 트리거 응답 없음(90초 타임아웃) — autopull 태스크가 10분 내 자동 반영." -ForegroundColor Yellow
} elseif ($trigger.ExitCode -eq 0) {
    Start-Sleep -Seconds 2
    $verify = Invoke-SshTimeout -TimeoutMs 20000 -ArgList ($sshOpts + @($VMHost, "git -C C:\ustrade rev-parse --short HEAD"))
    $pcHead = (& git -C $root rev-parse --short HEAD).Trim()
    if ($verify.TimedOut) {
        Write-Host "완료(push). HEAD 검증 응답 없음(20초 타임아웃) — autopull(10분)이 곧 맞춤." -ForegroundColor Yellow
    } else {
        $vmHead = $verify.Output.Trim()
        if ($vmHead -eq $pcHead) { Write-Host "완료. VM 반영됨 — HEAD $vmHead (PC 일치). 폰 새로고침으로 확인." -ForegroundColor Green }
        else { Write-Host "완료(push). VM HEAD=$vmHead / PC=$pcHead — autopull(10분)이 곧 맞춤(GitHub 전파 지연일 수)." -ForegroundColor Yellow }
    }
} else {
    Write-Host "완료(push). SSH 트리거 실패(VM offline/tailnet?) — autopull 태스크가 10분 내 자동 반영." -ForegroundColor Yellow
}
$ErrorActionPreference = $eapV
