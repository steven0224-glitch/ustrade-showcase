# 대시보드 서버 상시운영 — 시작프로그램 폴더 등록 (관리자 권한 불필요).
#
# 로그온 시 wscript(.vbs)가 pythonw 로 server.py 백그라운드 실행. 기본 읽기전용(토큰 미설정)·localhost.
# 제어/외부노출 원하면 생성된 .vbs 의 Environment 주석(') 해제 후 값 변경.
#
# .cmd 배치를 쓰지 않는 이유: 한글 경로가 든 배치는 콘솔 코드페이지에 따라 cd 줄이 깨져
# server.py 를 못 찾고 무음 종료함(2026-07-10 실측). wscript 는 UTF-16 BOM 파일을 직접
# 파싱하므로 코드페이지와 무관하게 동작. server.py 쪽 pythonw stdout=None 가드도 같은 날 추가.
#
#   pwsh dashboard/install_autostart.ps1     # 등록(시작프로그램에 .vbs 생성)
#   해제: Remove-Item "$([Environment]::GetFolderPath('Startup'))\ustrade-dashboard.vbs"

$ErrorActionPreference = "Stop"
$proj = Split-Path $PSScriptRoot -Parent
$pw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
if (-not (Test-Path $pw)) { $pw = (Get-Command python).Source }
$startup = [Environment]::GetFolderPath('Startup')
$vbsPath = Join-Path $startup 'ustrade-dashboard.vbs'

$body = @"
' 대시보드 서버 자동시작 — pythonw 백그라운드(창 없음). install_autostart.ps1 이 생성.
' 제어/외부 노출: 아래 주석 해제 (프로세스 env 로 주입)
Set sh = CreateObject("WScript.Shell")
' sh.Environment("PROCESS")("DASH_TOKEN") = "제어비번"
' sh.Environment("PROCESS")("DASH_SITE_PASS") = "사이트비번"
' sh.Environment("PROCESS")("DASH_HOST") = "0.0.0.0"
sh.CurrentDirectory = "$proj"
sh.Run """$pw"" dashboard\server.py", 0, False
"@
[IO.File]::WriteAllText($vbsPath, $body, [Text.Encoding]::Unicode)                      # UTF-16 BOM — wscript 네이티브
Remove-Item (Join-Path $startup 'ustrade-dashboard.cmd') -ErrorAction SilentlyContinue  # 구 .cmd 잔재 제거(이중 기동 방지)

Write-Host "등록됨: $vbsPath" -ForegroundColor Green
Write-Host "로그온 시 자동 실행. 지금 시작하려면: wscript `"$vbsPath`"" -ForegroundColor Cyan
Write-Host "해제: Remove-Item `"$vbsPath`"" -ForegroundColor Yellow
