<#
  _pyenv.ps1 — 런처 공통 환경 해석 (dot-source 전용)
    · $py            : 파이썬 인터프리터
    · Assert-ProjRoot: 스케줄 태스크에 박아도 안전한 프로젝트 루트인지 검증

  ── 왜 인터프리터를 한 곳에서 정하나
  deploy_push.ps1 / setup_*.ps1 이 각자 `Get-Command python` 으로 골랐다. 그게 집는 건
  전역 Python 이고 핀 의존성은 venv 에만 있다 — 2026-07-27 실측으로 사용자 site 에
  yfinance·backtrader·vectorbt·numba·schedule·pandas_market_calendars 가 전부 부재였고,
  배포 게이트는 `ModuleNotFoundError: yfinance` 로 RC=1. 더 나쁜 건 setup_*.ps1 인데,
  Register-ScheduledTask 는 경로를 문자열로 박을 뿐 실행하지 않아 **등록은 초록불이고
  매일 트리거될 때만 죽는다**.

  우선순위: $env:USTRADE_PY > %USERPROFILE%\.venvs\ustrade > PATH 의 python

  ── 왜 ProjRoot 를 검증하나
  태스크는 SYSTEM 으로 돈다. SYSTEM 은 사용자 세션의 OneDrive 동기화 엔진을 쓸 수 없어
  Files On-Demand 자리표시자(RecallOnDataAccess, 0x80000)를 읽지 못한다. 자리표시자
  경로를 cwd 로 박으면 Test-Path 는 True 인데(메타데이터는 존재) 실행은 아무 흔적 없이
  끝난다 — 2026-07-27 실증: 등록 성공 · LastTaskResult=0 · 로그 0줄 · home 미생성.
  등록 시점에 막지 않으면 06:10 에 조용히 실패한다.

  사용:
    . "$PSScriptRoot\_pyenv.ps1"     # → $py 설정
    Assert-ProjRoot $ProjRoot        # → 부적합하면 throw
#>

$py = $null

if ($env:USTRADE_PY -and (Test-Path $env:USTRADE_PY)) {
    $py = $env:USTRADE_PY
}
else {
    $venvPy = Join-Path $env:USERPROFILE '.venvs\ustrade\Scripts\python.exe'
    if (Test-Path $venvPy) {
        $py = $venvPy
    }
    else {
        $py = (Get-Command python -ErrorAction SilentlyContinue).Source
    }
}

if (-not $py) {
    throw "python 을 찾지 못함 — venv(~\.venvs\ustrade) 를 만들거나 USTRADE_PY 로 지정할 것"
}

# PYTHONPATH 는 venv site-packages 보다 sys.path 앞에 꽂힌다 — venv 가
# include-system-site-packages=false 여도 무력화된다. 2026-07-27 데스크탑 실측:
# PYTHONPATH=C:\pylibs 의 numpy 2.5.0·pandas 3.0.3 이 venv 의 2.4.6·2.3.3 을 눌러
# `ImportError: Numba needs NumPy 2.4 or less` 로 게이트 RC=1. 위 인터프리터 선택과
# 같은 실패(런처가 핀 아닌 패키지를 집음)라 여기서 함께 막는다. 이 셸과 자식
# 프로세스에만 적용되므로 다른 프로젝트에는 영향 없다.
$env:PYTHONPATH = $null


function Assert-ProjRoot {
    <#
      스케줄 태스크(SYSTEM)의 cwd 로 박아도 안전한 루트인지 검사. 부적합하면 throw.
      호출 위치는 관리자 권한 확인보다 **앞** — 승격 없이도 잘못된 경로를 즉시 알리기 위함.
    #>
    param([Parameter(Mandatory)][string]$Path)

    $entry = Join-Path $Path 'run_live.py'
    if (-not (Test-Path $entry)) {
        throw "ProjRoot 에 run_live.py 가 없음: $Path"
    }

    # 0x80000 = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS (OneDrive Files On-Demand 자리표시자).
    # .NET FileAttributes 열거형이 이 비트를 이름으로 못 내놓아 문자열 비교로는 놓친다 →
    # 반드시 정수 비트로 판정할 것.
    $attr = (Get-Item $entry -Force).Attributes.value__
    if ($attr -band 0x80000) {
        throw @"
ProjRoot 가 OneDrive Files On-Demand 자리표시자다: $Path
  SYSTEM 계정 태스크는 이 파일을 읽지 못해 흔적 없이 종료한다(LastTaskResult=0, 로그 0줄).
  → 로컬 실체 경로에서 등록할 것. 예: git clone <repo> C:\ustrade 후
     pwsh -File C:\ustrade\tools\<이 스크립트> ...
"@
    }
}
