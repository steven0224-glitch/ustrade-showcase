<#
  vm_selfheal.ps1 — VM 인바운드/Tailscale 자가치유 (멱등). vm_autopull 이 매 라운드 호출.

  배경(2026-07-18 사고): 게스트 네트워크 프로필이 Public 으로 플립(Stop/Start 로 새 NIC =
  새 네트워크 = Public 기본)되며 인바운드 전멸(RDP·SSH·ping·브라우저RDP 519) + Tailscale
  서비스 정지. 아웃바운드만 생존해 들어갈 문이 없었고, repo(autopull)가 유일한 복구 채널.

  원칙: 전 단계 멱등·fail-open(한 단계 실패가 다음을 못 막게), 실제 변경/실패가 있었던
  라운드만 logs\selfheal.log 에 1줄 기록. PS 5.1 호환.
#>
$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
$log  = Join-Path $root 'logs\selfheal.log'
$acts = @()

function Try-Step($name, [scriptblock]$b) {
  try { & $b } catch { $script:acts += "FAIL ${name}: $($_.Exception.Message)" }
}

# 1) 네트워크 프로필 Public -> Private (Private 스코프 인바운드 룰 복원)
Try-Step 'profile' {
  Get-NetConnectionProfile -ErrorAction SilentlyContinue |
    Where-Object { $_.NetworkCategory -eq 'Public' } | ForEach-Object {
      Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private
      $script:acts += "profile: '$($_.Name)'($($_.InterfaceAlias)) Public -> Private"
    }
}

# 2) 방화벽 프로필이 '룰 무시 전면차단'(AllowInboundRules=False) 모드면 원복.
#    DefaultInboundAction=Block 은 정상값이라 건드리지 않는다.
Try-Step 'fw-allow-rules' {
  Get-NetFirewallProfile -ErrorAction SilentlyContinue |
    Where-Object { "$($_.AllowInboundRules)" -eq 'False' } | ForEach-Object {
      Set-NetFirewallProfile -Name $_.Name -AllowInboundRules True
      $script:acts += "fw: $($_.Name) AllowInboundRules -> True"
    }
}

# 3) 핵심 서비스 기동 보장: Tailscale / sshd / TermService(RDP)
foreach ($svc in @('Tailscale', 'sshd', 'TermService')) {
  Try-Step "svc-$svc" {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
      if ($s.StartType -ne 'Automatic') {
        Set-Service -Name $svc -StartupType Automatic
        $script:acts += "svc ${svc}: StartupType -> Automatic"
      }
      if ($s.Status -ne 'Running') {
        Start-Service -Name $svc
        $script:acts += "svc ${svc}: started"
      }
    }
  }
}

# 4) RDP OS 스위치 + 방화벽 룰 활성 (영문 AMI 그룹명 기준)
Try-Step 'rdp' {
  $k = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
  if ((Get-ItemProperty -Path $k -ErrorAction SilentlyContinue).fDenyTSConnections -ne 0) {
    Set-ItemProperty -Path $k -Name fDenyTSConnections -Value 0
    $script:acts += 'rdp: fDenyTSConnections -> 0'
  }
  $off = @(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue |
           Where-Object { "$($_.Enabled)" -eq 'False' })
  if ($off.Count) { $off | Enable-NetFirewallRule; $script:acts += "fw: 'Remote Desktop' 룰 $($off.Count)개 활성" }
  $off2 = @(Get-NetFirewallRule -DisplayName 'OpenSSH*' -ErrorAction SilentlyContinue |
            Where-Object { "$($_.Enabled)" -eq 'False' })
  if ($off2.Count) { $off2 | Enable-NetFirewallRule; $script:acts += "fw: OpenSSH 룰 $($off2.Count)개 활성" }
}

if ($acts.Count) {
  if (-not (Test-Path (Split-Path $log))) { New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null }
  "{0}  selfheal: {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), ($acts -join ' | ') |
    Add-Content -Path $log -Encoding UTF8
}
exit 0
