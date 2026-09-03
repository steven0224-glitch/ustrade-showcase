# 외부망 노출 — cloudflared 빠른 터널로 대시보드를 임시 공개 URL에 띄움.
#
# 보안: 공개 URL = 누구나 접근 가능. 반드시 먼저 서버를 패스+토큰으로 띄워라:
#   $env:DASH_SITE_PASS = "사이트비번"     # 전 사이트 접근 차단
#   $env:DASH_TOKEN     = "제어비번"        # 매매·정지 제어 잠금
#   python dashboard/server.py
# 그 다음 새 창에서 이 스크립트 실행:
#   pwsh dashboard/tunnel.ps1
# 출력된 https://...trycloudflare.com URL 끝에 ?k=사이트비번 붙여 폰에서 접속.
#
# trycloudflare 빠른 터널은 계정 불필요·임시(껐다 켜면 URL 바뀜). 상시 운영은 named tunnel 사용.

param([switch]$Public)   # DASH_SITE_PASS 없이 의도적으로 공개할 때만 지정

$ErrorActionPreference = "Stop"
$port = $env:DASH_PORT; if (-not $port) { $port = "8765" }

# 보안 게이트(fail-closed) — DASH_SITE_PASS 미설정이면 공개 URL이 무방비 노출 → 기본 중단.
# (기존: 3초 경고 후 그냥 공개 → 깜빡하면 실계좌 포트폴리오가 공개 URL에 노출. 이제 명시적 -Public 필요.)
if (-not $env:DASH_SITE_PASS -and -not $Public) {
    Write-Host "중단: DASH_SITE_PASS 미설정 — 공개 URL이 무방비 노출됩니다." -ForegroundColor Red
    Write-Host "  권장:  `$env:DASH_SITE_PASS='사이트비번'  설정 후 재실행" -ForegroundColor Yellow
    Write-Host "  의도적 공개:  pwsh dashboard/tunnel.ps1 -Public" -ForegroundColor Yellow
    exit 1
}
if ($Public -and -not $env:DASH_SITE_PASS) {
    Write-Host "경고: -Public — 사이트 비번 없이 공개(읽기 노출). 제어는 DASH_TOKEN 으로 별도 잠금 확인." -ForegroundColor Yellow
}

if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Host "cloudflared 미설치 — winget 으로 설치 시도..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
    } else {
        Write-Host "winget 없음. 수동 설치: https://github.com/cloudflare/cloudflared/releases" -ForegroundColor Red
        exit 1
    }
}

Write-Host "터널 시작: http://localhost:$port → 공개 URL (아래 trycloudflare.com 주소)" -ForegroundColor Green
cloudflared tunnel --url "http://localhost:$port"
