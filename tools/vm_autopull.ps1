<#
  vm_autopull.ps1 - VM self-update, run periodically by the 'ustrade-autopull' scheduled task.

  Flow each run:
    git fetch -> if local == origin/main: do nothing (silent).
                 else: pull --ff-only -> (pip if reqs changed) -> restart dashboard.
    Skips the pull if a trading bot (run_live/run_exit/review/heartbeat/panic_exit) is running,
    so a half-updated tree never gets launched mid-trade. Retries next round.

  Verification:
    Every run overwrites <root>\logs\autopull.last with one line:
      <time> as <user> fetch=<ok|FAIL> head=<sha>
    so you can confirm the task actually runs under an identity whose git auth works.
    Actions/errors are appended to <root>\logs\autopull.log (deployment ledger: gate verdict,
    pull, test verdict, restart). Full test-suite output goes to <root>\logs\autopull-tests.log,
    overwritten each deploy, so it can never bury the ledger.

  Why a dashboard restart is needed: server.py is a long-running process that imports
  personas.py / build_data.py at startup; code changes only take effect on restart.
  (The bot tasks spawn a fresh python each schedule, so they reload code on their own.)
#>
$ErrorActionPreference = 'Stop'
$root   = Split-Path $PSScriptRoot -Parent
$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force $logDir | Out-Null }
$log    = Join-Path $logDir 'autopull.log'
$last   = Join-Path $logDir 'autopull.last'
# 테스트 게이트 stdout 은 별 파일(매 배포 덮어씀). 종전엔 autopull.log 에 append 했는데 스위트
# 출력 16KB+ 가 매 배포마다 쌓여 200KB 트림(최근 300줄)을 밀어붙여 pull·게이트 이력을 지웠다 —
# 무음 차단 진단의 유일한 단서가 그 이력이다(lessons/2026-08-01-unsigned-commit-blocks-vm-autopull).
$testLog = Join-Path $logDir 'autopull-tests.log'

function Log($m) {
  "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -Path $log -Encoding UTF8
}
# Alert = Log + best-effort 텔레그램/슬랙(repo notify.py). notify 는 예외 삼킴(알림이 못 막게)이라
# 무인 VM 에서도 안전. env(TELEGRAM_*/SLACK_*) 미설정이면 notify 는 로그만 — Log 는 항상 남는다.
function Alert($level, $m) {
  Log $m
  try {
    $py = "import sys; sys.path.insert(0, r'$root'); import notify; notify.notify(sys.argv[1], sys.argv[2])"
    & python -c $py $m $level 2>&1 | Out-Null
  } catch {}
}
# keep the log small (trim to last 300 lines if it grows past ~200KB)
try { if ((Test-Path $log) -and ((Get-Item $log).Length -gt 200KB)) {
        (Get-Content $log -Tail 300) | Set-Content $log -Encoding UTF8 } } catch {}

try {
  $who    = (whoami)

  # 자가치유(멱등) — 네트워크 프로필 플립/서비스 정지로 인바운드·Tailscale 이 죽어도
  # repo 채널로 복구 (2026-07-18 사고). 매 라운드, fetch 이전(오프라인이어도 로컬 치유는 진행).
  try { & (Join-Path $root 'tools\vm_selfheal.ps1') *>> $log } catch {}

  $before = (& git -C $root rev-parse HEAD 2>$null).Trim()

  $eap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  & git -C $root fetch origin 2>&1 | Out-Null
  $fc = $LASTEXITCODE
  $ErrorActionPreference = $eap

  # heartbeat (overwrite) - proves the task ran and whether git auth worked under this principal
  "{0} as {1} fetch={2} head={3}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $who,
    $(if ($fc -eq 0) { 'ok' } else { 'FAIL' }), $(if ($before) { $before.Substring(0,7) } else { '?' }) |
    Set-Content -Path $last -Encoding UTF8

  if ($fc -ne 0) { Log "fetch FAILED (exit=$fc) as $who - check git auth (SSH key / credential)"; exit 0 }

  $remote = (& git -C $root rev-parse origin/main 2>$null).Trim()
  if ($before -eq $remote) { exit 0 }   # already current - nothing to do

  # hold off if a trading bot is mid-run (avoid pulling a half-changed tree under it)
  $busy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -match 'run_live|run_exit|review\.py|heartbeat|panic_exit' }
  if ($busy) { Log "bot running - defer pull to next round"; exit 0 }

  # --- 커밋 무결성 게이트 (Critical) -----------------------------------------
  # origin/main 팁 커밋이 신뢰 서명자(allowed_signers)로 SSH 서명됐는지 pull 前 검증.
  # 통과 못 하면 pull 중단 → 워킹트리는 이미-신뢰된 현재 커밋($before)에 그대로. 자격증명 탈취
  # 후 origin/main 직접 push 로 임의코드(+오염 requirements)가 실거래 VM 에 내려오는 걸 차단.
  # 스위치: USTRADE_REQUIRE_SIGNED_COMMITS=1 이거나 allowed_signers 파일이 있으면 fail-closed 강제.
  # 서명 인프라 미구성 시엔 매 실행 큰 경고(로그+알림 1건)로 무성 방치 불가 — 켜는 법도 함께 안내.
  $signers = Join-Path $root 'state\allowed_signers'
  $requireSigned = ($env:USTRADE_REQUIRE_SIGNED_COMMITS -eq '1') -or (Test-Path $signers)
  if ($requireSigned) {
    if (-not (Test-Path $signers)) {
      Alert 'halt' "SIGNED-COMMITS 강제(env)인데 allowed_signers 없음: $signers - pull 중단. 파일 설치 후 재시도"
      exit 0
    }
    $eapV = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    & git -C $root -c gpg.format=ssh -c "gpg.ssh.allowedSignersFile=$signers" verify-commit origin/main 2>&1 | Out-Null
    $vc = $LASTEXITCODE
    $ErrorActionPreference = $eapV
    if ($vc -ne 0) {
      Alert 'halt' ("커밋 서명 검증 실패 origin/main={0} (신뢰 서명자 아님) - pull 중단, {1} 유지. 탈취 push 의심시 GitHub 확인" -f $remote.Substring(0,7), $before.Substring(0,7))
      exit 0
    }
    Log ("commit-integrity ok origin/main={0} 서명 검증됨" -f $remote.Substring(0,7))
  } else {
    Alert 'warn' "무결성 검증 OFF (allowed_signers 없음). 탈취된 GitHub 자격증명이 임의코드를 실거래 VM 에 내릴 수 있음. 켜기: state\allowed_signers 설치 후 dev-PC 서명 커밋 (setup 주석 참조)"
  }
  # ---------------------------------------------------------------------------

  $reqChanged = & git -C $root diff --name-only HEAD origin/main -- requirements_vm.txt requirements.txt

  $eap2 = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  & git -C $root pull --ff-only origin main 2>&1 | Out-Null
  $pc = $LASTEXITCODE
  $ErrorActionPreference = $eap2
  if ($pc -ne 0) { Log "pull FAILED (ff not possible = local edits on VM) - manual fix needed"; exit 0 }
  $after = (& git -C $root rev-parse HEAD).Trim()
  Log ("pulled {0} -> {1}" -f $before.Substring(0,7), $after.Substring(0,7))

  # --- 테스트 게이트 + 롤백 (Medium) -----------------------------------------
  # pull 은 됐지만 코드가 live 되기 前(pip/대시보드 재기동 前) 배포 게이트 스위트 실행.
  # deploy_push 와 동일한 tools\run_tests.py. 실패 시 $before 로 hard reset 롤백 → 깨진 가드레일이
  # 무인 실거래로 나가지 않게. 성공 시 기존 흐름(pip + 대시보드 재기동) 그대로.
  $eapT = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  & python (Join-Path $root 'tools\run_tests.py') *> $testLog
  $tc = $LASTEXITCODE
  $ErrorActionPreference = $eapT
  if ($tc -ne 0) {
    & git -C $root reset --hard $before 2>&1 | Out-Null
    Alert 'halt' ("배포 테스트 실패(exit={0}) after pull {1} - {2} 로 롤백. 새 코드 미반영, 수동 확인 필요 (상세: logs\autopull-tests.log)" -f $tc, $after.Substring(0,7), $before.Substring(0,7))
    exit 0
  }
  Log "배포 테스트 통과 (ALL SUITES PASS, 상세: logs\autopull-tests.log) - 반영 진행"
  # ---------------------------------------------------------------------------

  if ($reqChanged) {
    Log "requirements changed -> pip install"
    & python -m pip install -r (Join-Path $root 'requirements_vm.txt') *>> $log
  }

  # restart the dashboard so server.py reloads new code (free the port if it lingers)
  try {
    Stop-ScheduledTask ustrade-dashboard -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
      ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 1
    Start-ScheduledTask ustrade-dashboard
    Log "dashboard restarted"
  } catch { Log "dashboard restart error: $_" }
}
catch { Log "exception: $_" }
exit 0
