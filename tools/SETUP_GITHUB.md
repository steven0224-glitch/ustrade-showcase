# 일회성 셋업 — GitHub 비공개 repo 연결 (최초 1회)

## STEP 1 — GitHub 에 빈 비공개 repo 생성 (사람이)
1. github.com 로그인 → 우상단 **＋ → New repository**
2. Repository name: `ustrade` (아무 이름 OK)
3. **Private** 선택
4. **README/.gitignore/license 체크 전부 해제** (빈 repo 여야 함 — 안 그러면 push 충돌)
5. Create repository → 나오는 URL 복사: `https://github.com/steven0224-glitch/ustrade.git`

## STEP 2 — PC 에서 remote 연결 + 첫 push
```powershell
$proj = "C:\Users\<you>\OneDrive\문서\Claude\Projects\미국주식 자동매매"
git -C $proj remote add origin https://github.com/steven0224-glitch/ustrade.git
git -C $proj push -u origin main
```
- 첫 push 때 **Git Credential Manager 창**이 떠서 GitHub 로그인 요구 → 브라우저로 승인하면 끝(비밀번호 입력 X). 이후 자동.

## STEP 3 — VM 인증용 읽기전용 토큰 (선택이지만 권장)
VM 이 비공개 repo 를 pull 하려면 인증 필요. VM 이 털려도 피해 최소화하려고 **읽기전용·이 repo 한정** 토큰 사용:
1. github.com → Settings → Developer settings → **Fine-grained tokens** → Generate new token
2. Resource owner = 본인, **Only select repositories → ustrade**
3. Permissions → Repository permissions → **Contents: Read-only** (이것만)
4. Expiration 90일 등 설정 → Generate → **토큰 복사** (한 번만 보임)
5. ※ 이 토큰은 채팅에 붙여넣지 말 것 — VM 에서만 사용.

## STEP 4 — VM(C:\ustrade)을 git clone 으로 전환 (in-place, 상태 보존)
RDP 접속 → VM PowerShell:
```powershell
$r = "C:\ustrade"
git -C $r init -b main
git -C $r remote add origin https://github.com/steven0224-glitch/ustrade.git
git -C $r fetch origin              # ← 여기서 인증창: username=GitHub ID, password=STEP3 토큰
git -C $r reset --hard origin/main  # 추적파일을 repo 버전으로 (state/·logs/ 는 보존됨)
git -C $r branch --set-upstream-to=origin/main main
```
- `reset --hard` 는 **추적되는 코드만** repo 버전으로 맞춤. `state/`·`logs/`·튜닝값은 .gitignore 라 그대로 보존.
- 스케줄 태스크 경로(C:\ustrade)는 그대로 → 태스크 재등록 불필요.

## 끝 — 이후 일상
PC: `pwsh tools\deploy_push.ps1 "메시지"` / VM: `powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\vm_update.ps1`
