# 배포 파이프라인 (PC → GitHub → VM)

PC 가 git 소스, GitHub 비공개 repo 가 중계, VM(C:\ustrade)이 pull 받아 실행.

```
  PC 코드수정 ──deploy_push.ps1──▶ GitHub(private) ──vm_update.ps1──▶ VM C:\ustrade
   (소스)        테스트+커밋+push                       pull+pip            (실행)
```

## 일상 사용 (셋업 끝난 뒤)

**PC 에서 — 코드 바꾼 뒤:**
```powershell
pwsh tools\deploy_push.ps1 "무엇을 바꿨는지"
```
테스트 통과해야 push. 실패하면 push 안 됨.

**VM 에서 — RDP 접속 후:** (VM 은 pwsh7 없으니 Windows PowerShell)
```powershell
powershell -ExecutionPolicy Bypass -File C:\ustrade\tools\vm_update.ps1
```
봇 실행중이면 자동 보류. 다음 스케줄 실행부터 새 코드 적용(재시작 불필요).

## 규칙 / 한계
- **비밀은 git 에 안 들어감.** TOSS/TELEGRAM 키는 VM 환경변수(User/Machine scope)에만. `.gitignore` 가 state/·logs/·.env·*.key 제외.
- **런타임 상태(state/, logs/)는 추적 안 함** → pull 해도 VM 의 tuning.json·sleeve·저널 안 건드림.
- **canslim A엔진**(`C:\텔레그램_시그널_알리미\engine\`)은 이 repo 밖. 거기 바뀌면 \\tsclient 로 별도 복사.
- **장중 업데이트 금지.** vm_update 가 실행중 파이썬 감지해 막음. 급하면 `-Force`(위험).

## 일회성 셋업 (최초 1회)
`tools/SETUP_GITHUB.md` 참고 — GitHub repo 생성 → PC remote+push → VM 을 git clone 으로 전환.
