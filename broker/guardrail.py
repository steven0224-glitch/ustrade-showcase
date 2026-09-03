"""킬스위치 — 무인 거래 안전 가드레일 (Vibe-Trading operator-halt 패턴 발췌).

거래 제출 전 체크. 상태는 state/killswitch.json 에 영속 (스케줄 실행 간 유지).
트립 시 halt 플래그 → 수동 reset 전까지 거래 거부 (일일손실은 날짜 바뀌면 자동 리셋).

무인은 버그 = 손실. 이 층이 마지막 방어선.
"""
import json
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .base import Side
from paths import STATE_DIR        # 동기화 폴더 밖 (OneDrive 손상 방지)

KILL_FILE = STATE_DIR / "HALT"          # 이 파일 만들면 즉시 전면 정지
STATE_FILE = STATE_DIR / "killswitch.json"
LOCK_FILE = STATE_DIR / "run.lock"      # 동시 실행 방지 (더블트레이드 차단)
_LOCK_STALE_SEC = 3600                  # 이 시간 지난 락은 좀비 후보 — 단 보유 pid 가 살아있으면 회수 안 함
_LOCK_HARD_SEC = 6 * 3600              # 이 시간 넘으면 pid 생존과 무관하게 회수(pid 재사용 대비 — 6h 넘는 실행 없음)
_LOCK_HEARTBEAT_SEC = _LOCK_STALE_SEC // 4   # 락 보유 중 이 주기로 mtime 갱신 → 살아있는 장기실행이 좀비로 오인·탈취되는 것 방지


def _pid_alive(pid: int) -> bool:
    """프로세스 생존 여부 best-effort. 불확실하면 True(살아있다 가정 → 락 회수 안 함, 안전측)."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259
            ERROR_ACCESS_DENIED = 5
            k = ctypes.windll.kernel32
            # 명시적 시그니처 — 64비트 윈도우서 HANDLE(void*)을 기본 c_int 로 잘라 오판하는 것 방지.
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k.GetExitCodeProcess.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return k.GetLastError() == ERROR_ACCESS_DENIED   # 권한거부=존재, 그외=없음
            try:
                code = wintypes.DWORD()
                if k.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)        # POSIX: 신호 0 = 존재확인(종료 안 함)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True            # 판정 불가 → 살아있다고 가정(회수 보류 = 더블트레이드보다 안전)


@dataclass
class GuardConfig:
    max_daily_loss: float = 0.05        # 당일 시작자산(직전일 종가) 대비 손실 한도
    max_total_drawdown: float = 0.20    # 고점(HWM) 대비 누적 손실 한도 — 일일한도 밑도는 다일 그라인드다운 차단(GUARD-1)
    max_position_weight: float = 0.40   # 단일종목 최대 비중 (분산 강제)
    max_gross: float = 1.05             # 총노출 상한
    max_consecutive_errors: int = 3     # 이 수 *이상* 누적 시 정지 (>=N; 허용=N-1, 3=3번째 실행서 정지)
    error_window: int = 6               # 에러 카운트 롤링 윈도우 (최근 N회 실행 — flapping 차단)
    max_order_notional: float = 1_000_000.0  # 단일주문 명목 상한 절대치 (fat-finger)
    order_notional_buffer: float = 1.5  # 주문 명목 = max_position_weight·자산·이 버퍼 이하 (GUARD-2, 2배 사이징버그 트립)


class HaltError(Exception):
    """거래 정지 — 호출측은 주문 제출하지 말 것."""


class LockBusy(Exception):
    """다른 실행이 락 보유 중 — 중복 실행 거부."""


class RunLock:
    """프로세스 간 배타 락 (load→체결→mark_traded 임계구역 보호).

    O_EXCL 로 생성 — 이미 있으면 다른 실행 중. 좀비 락(_LOCK_STALE_SEC 경과)은 탈취.
    동시 cron/수동 실행이 멱등락(already_traded)을 read-modify-write 레이스로
    우회해 더블트레이드하는 것을 차단 (파일 원자성만으론 부족).
    """
    def __init__(self, path: Path = None, steal_dead_after: int = 1800):
        self.path = path or LOCK_FILE   # 호출 시점 해석 (테스트서 LOCK_FILE 패치 가능)
        # 죽은 pid 락 회수까지 대기(초). 기본 1800=2*_LOCK_HEARTBEAT_SEC(기존 동작 그대로 유지) —
        # 호출측이 짧게 줄여도 안전: Windows 는 보유자가 fd 를 연 채면 rename steal 이 sharing
        # violation 으로 실패하므로(__enter__ 의 os.rename), *살아있는* 락은 값과 무관히 오탈취 불가
        # — 짧은 값의 위험은 구조적으로 차단되고, 죽은 락만 더 빨리 회수된다.
        self.steal_dead_after = steal_dead_after
        self._fd = None
        self._stop = None    # 하트비트 정지 신호
        self._hb = None      # 하트비트 스레드

    def _zombie_state(self, path: Path):
        """(회수가능?, age, holder) — 좀비락 판정. 판정 불가는 보수적으로 '살아있음'(회수 안 함).

        pid 죽음 확인 시 steal_dead_after 경과하면 회수(기본 30min≈2*heartbeat) — 크래시한 진입(run_live)이
        청산(run_exit)을 _LOCK_STALE_SEC(1h) 동안 막아 리스크관리 공백 나던 것 단축(진입·청산 공유락 유지가
        슬리브 동시쓰기 레이스 방지엔 옳음 — 분리 대신 회수속도 개선). pid 살아있거나 판정불가(접근거부)면
        hard 6h 까지 보류 — pid 재사용 오회수 방지(안전측).
        """
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        try:
            holder = int((path.read_text() or "-1").strip() or "-1")
        except (OSError, ValueError):
            holder = -1
        dead = not _pid_alive(holder)
        return ((dead and age > self.steal_dead_after) or age > _LOCK_HARD_SEC), age, holder

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._open()
        except FileExistsError:
            # 좀비 락 회수 시도 (크래시로 미해제). 단 보유 프로세스가 아직 살아있으면(정상적
            # 1h+ 장기 실행) 회수 금지 — 살아있는 락을 탈취하면 동시 실행 더블트레이드.
            steal, age, holder = self._zombie_state(self.path)
            if not steal:
                raise LockBusy(f"다른 실행 진행 중 (락 {self.path.name}, pid {holder}, {age:.0f}s)")
            # 회수는 ①rename 으로 원자화하고 ②떼어낸 파일의 좀비 여부를 재확인한다.
            # 종전 unlink→_open 2단계는 '판정한 파일'과 '지우는 파일'이 같다는 보장이 없었다 —
            # 판정 후 다른 실행이 회수·재생성한 *살아있는* 락을 지우고 O_EXCL 에도 성공해
            # 양쪽이 동시 보유(더블트레이드)할 수 있다. rename 은 소스가 사라졌으면 실패하므로
            # 파일을 실제로 떼어낸 프로세스가 정확히 하나이고, 떼어낸 뒤엔 배타 소유라 재판정이
            # 신뢰할 수 있다(신선하면 되돌려놓고 거부 = 판정·회수 원자성 확보).
            # ※ 스틸 토큰 파일 방식은 토큰 보유자가 회수 도중 크래시하면 이후 회수가 영구 봉쇄돼
            #   (크래시 복구 불능) 채택하지 않았다.
            victim = self.path.with_name(
                f"{self.path.name}.stale.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}")
            try:
                os.rename(str(self.path), str(victim))
            except OSError:
                # 경합 패배(다른 실행이 먼저 떼어감) 또는 보유자가 파일을 연 채 생존(Windows 공유위반)
                raise LockBusy(f"좀비락 동시 회수 경합 (락 {self.path.name})") from None
            if not self._zombie_state(victim)[0]:
                try:
                    os.rename(str(victim), str(self.path))   # 신선한 락이었다 → 원상복구(best-effort)
                except OSError:
                    pass
                raise LockBusy(f"좀비락 회수 중 새 락 감지 — 회수 취소 (락 {self.path.name})")
            try:
                os.unlink(victim)
            except OSError:
                pass
            try:
                self._open()
            except FileExistsError:
                # 회수 직후 제3의 실행이 새 락을 선점 → 거부(크래시 아님)
                raise LockBusy(f"좀비락 회수 후 재선점됨 (락 {self.path.name})") from None
        self._start_heartbeat()   # 락 확보 후 — 보유 중 mtime 갱신해 탈취 방지
        return self

    def _open(self):
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self._fd, str(os.getpid()).encode())

    def _touch(self):
        """락 mtime 갱신 — 살아있는 장기 실행이 stale/hard 임계 넘겨 좀비로 오인·탈취되는 것 방지."""
        try:
            os.utime(self.path, None)
        except OSError:
            pass

    def _heartbeat(self):
        # _stop 신호(=락 해제) 들어올 때까지 주기적으로 mtime 갱신. wait() 가 True 반환=정지.
        while not self._stop.wait(_LOCK_HEARTBEAT_SEC):
            self._touch()

    def _start_heartbeat(self):
        self._stop = threading.Event()
        self._hb = threading.Thread(target=self._heartbeat, name="runlock-hb", daemon=True)
        self._hb.start()

    def __exit__(self, *exc):
        if self._stop is not None:
            self._stop.set()            # 하트비트 정지 먼저 — unlink 후 갱신 시도 방지
        if self._hb is not None:
            self._hb.join(timeout=2)
        if self._fd is not None:
            os.close(self._fd)
            try:
                self.path.unlink()
            except OSError:
                pass
        return False


class KillSwitch:
    def __init__(self, config: Optional[GuardConfig] = None, today: str = "", namespace: str = ""):
        self.cfg = config or GuardConfig()
        self.today = today   # 'YYYY-MM-DD' (호출측서 주입 — 결정론)
        self.run_equity = None   # 이번 실행 자산 스냅샷 (roll_day 가 설정 — 명목캡 비례화용, 비영속)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # 브로커별 상태 분리(namespace) — paper($100k)·toss($100) 가 같은 baseline·hwm 파일을
        # 공유해 스케일 혼입으로 false-halt(−99.97% 오트립) 나던 것 차단. 미지정 시 기존 단일 파일.
        self._namespace = namespace
        self._state_file = STATE_FILE if not namespace else STATE_DIR / f"killswitch.{namespace}.json"
        self.state = self._load()

    @staticmethod
    def _default_state() -> dict:
        return {"halted": False, "reason": "", "halt_kind": "", "day": "",
                "day_start_equity": None, "errors": 0, "last_traded_day": "",
                "recent": [], "last_equity": None, "hwm": None}

    @property
    def _halt_marker(self) -> Path:
        """보조 정지 마커 — '상태파일이 인메모리 정지를 못 따라감'을 다음 런에 전달하는 유일 용도.

        trip 의 _save 가 실패했을 때만 생긴다. _save 가 한 번이라도 성공하면(=상태파일이 진실을
        담으면) 자동 제거되므로 자동해제·reset 경로에 별도 처리가 필요 없다.
        """
        return self._state_file.with_suffix(".halt")

    def _load(self) -> dict:
        st = self._load_state_file()
        # 마커 존재 = 직전 런이 정지를 걸었으나 영속화에 실패함 → fail-closed 로 정지 승계.
        # 사유/kind 를 복원해 자동해제(daily_loss)·청산허용 분류가 정상 trip 과 동일하게 동작.
        if self._halt_marker.exists():
            try:
                m = json.loads(self._halt_marker.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                m = {}
            if not isinstance(m, dict):
                m = {}
            st = {**st, "halted": True,
                  "reason": m.get("reason") or "정지 영속화 실패 마커 — 수동 확인 후 reset 필요",
                  "halt_kind": m.get("halt_kind", "")}
        return st

    def _load_state_file(self) -> dict:
        default = self._default_state()
        if self._state_file.exists():
            try:
                loaded = json.loads(self._state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError) as e:
                # 상태파일 손상 = fail-closed (무인은 의심스러우면 정지, 자동 진행 금지)
                return {**default, "halted": True,
                        "reason": f"상태파일 손상 — 수동 확인 후 reset 필요 ({type(e).__name__})"}
            if not isinstance(loaded, dict):   # 유효 JSON이나 dict 아님(배열/스칼라) → {**d,**loaded} TypeError 前 fail-closed
                return {**default, "halted": True,
                        "reason": "상태파일 비정상 구조(dict 아님) — 수동 확인 후 reset 필요"}
            return {**default, **loaded}   # 누락 키 보강 (구버전 호환)
        return default

    def _save(self) -> bool:
        # 원자적 쓰기 — tmp 작성 후 교체 (크래시/동기화 중 부분쓰기 손상 방지).
        # fsync 로 교체 前 디스크 반영 보장(전원차단 시 빈/잘린 파일 방지), tmp 는 pid 별로
        # 분리(동일 tmp 경로를 두 프로세스가 동시 쓰는 경합 방지).
        tmp = self._state_file.with_suffix(f".{os.getpid()}.tmp")
        # per-pid tmp 는 크래시 시 자가치유 안 됨(매 실행 새 pid) → 5분 넘은 옛 tmp 정리.
        # 진행중 쓰기(나/동시 writer)는 mtime 신선 → 건드리지 않음.
        try:
            for p in self._state_file.parent.glob(f"{self._state_file.stem}.*.tmp"):
                if p != tmp and (time.time() - p.stat().st_mtime) > 300:
                    p.unlink()
        except OSError:
            pass
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(json.dumps(self.state, indent=2, ensure_ascii=False))
            fp.flush()
            os.fsync(fp.fileno())
        from paths import atomic_replace
        if not atomic_replace(str(tmp), str(self._state_file)):   # Windows 동시읽기 PermissionError 재시도(crash 방지)
            print(f"[killswitch] 상태 교체 실패(다음 호출 재시도) {self._state_file}", file=sys.stderr)
            return False   # 호출측 판단용 — 정지(trip)는 이 실패를 무음 처리하면 안 된다
        try:
            self._halt_marker.unlink()   # 상태파일이 최신 = 보조 마커 불필요(자동 회수)
        except OSError:
            pass
        return True

    # 손실성 자동정지 — 보호 청산(위험축소 매도)은 계속 돌아야 하는 정지 종류.
    # 손절·추세이탈 청산이 손실한도의 '본래 목적'이므로 손실로 인한 정지가 청산을 막으면 안 된다.
    # 손실성 정지 + 에러누적 정지는 청산을 막지 않는다 — 에러누적은 대개 일시적 브로커/네트워크 장애라,
    # 장애 회복 중 가격 하락 시 보호 손절(위험축소 SELL)까지 막으면 무방비 손실. 진입은 is_halted 로 계속
    # 차단(구조적 이상 신호). 수동 HALT·데이터무결성(bad_*)·바운드는 여전히 청산도 fail-closed.
    _EXIT_OK_HALTS = ("daily_loss", "total_drawdown", "error")

    # --- 정지 상태 ---
    def is_halted(self):
        if KILL_FILE.exists():
            return True, "수동 HALT 파일 존재 (state/HALT)"
        return bool(self.state.get("halted")), self.state.get("reason", "")

    def exit_blocked(self):
        """위험축소 청산(SELL)까지 막아야 하는 '하드' 정지인지 (blocked, reason).

        손실성 자동정지(daily_loss/total_drawdown)는 보호 청산을 막지 않는다(False) — 손절이
        손실한도의 본래 목적. 수동 HALT 파일·데이터무결성(bad_*)·에러누적·바운드 정지는 구조적
        이상/사람 개입 신호이므로 청산도 fail-closed 차단(True). 롱온리라 SELL=위험축소 항상 성립.
        """
        if KILL_FILE.exists():
            return True, "수동 HALT 파일 존재 (state/HALT)"
        if self.state.get("halted") and self.state.get("halt_kind") not in self._EXIT_OK_HALTS:
            return True, self.state.get("reason", "")
        return False, ""

    def trip(self, reason: str, kind: str = ""):
        self.state["halted"] = True
        self.state["reason"] = reason
        self.state["halt_kind"] = kind   # 구조화 사유 (문자열 파싱 대신 자동해제 판정용)
        # 정지 영속화 실패는 무음이면 안 된다 — 인메모리 halted 는 이번 런만 보호하고, 다음 런은
        # 옛 상태(=거래 허용)를 읽어 그대로 거래한다(정지 미영속 = 무방비 진입/더블트레이드).
        # ① 보조 마커로 다음 런까지 정지를 전파하고 ② 예외를 올려 이번 런을 error 로 종결시킨다.
        # 성공 경로의 계약(호출측이 곧바로 raise HaltError)은 그대로.
        try:
            saved, err = self._save(), ""
        except OSError as e:
            saved, err = False, f" ({type(e).__name__}: {e})"
        if saved:
            return
        try:
            self._halt_marker.write_text(
                json.dumps({"halted": True, "reason": reason, "halt_kind": kind},
                           ensure_ascii=False), encoding="utf-8")
            marker = "보조 마커 기록"
        except OSError as e:
            marker = f"보조 마커도 실패({type(e).__name__})"
        raise OSError(f"[killswitch] 정지 영속화 실패 — {marker}. 정지 사유: {reason}{err}")

    def reset(self):
        # 누적DD 정지 해제 시에만 고점(hwm) 재seed → 안 하면 equity 가 여전히 고점-20% 밑이라
        # 리셋 직후 즉시 재트립(복구 불가, JSON 수동편집 강요). 다른 정지 해제는 hwm 보존
        # (일반 reset 이 누적DD 가드를 조용히 무력화하지 않게).
        if self.state.get("halt_kind") == "total_drawdown":
            self.state["hwm"] = None
            self.state["day_start_equity"] = None   # hwm 만 비우면 day baseline 이 옛 고점스케일이라 즉시 재트립 → 함께 재seed
        self.state["halted"] = False
        self.state["reason"] = ""
        self.state["halt_kind"] = ""
        self.state["errors"] = 0
        self.state["recent"] = []        # 에러 윈도우도 비움 — 리셋 직후 즉시 재트립 방지
        self._save()
        # 전역 HALT(STATE_DIR/HALT)는 모든 namespace 가 공유하는 수동 정지선 — paper_<persona> reset 이
        # 이를 무성 삭제하면 toss 실거래 정지까지 해제되는 Critical. paper* namespace reset 은 전역 HALT 를
        # 건드리지 않는다(전역/실거래 reset·dashboard api_resume 의 명시 unlink 만 해제).
        if not str(self._namespace or "").startswith("paper") and KILL_FILE.exists():
            KILL_FILE.unlink()

    # --- 일일 경계 ---
    def _breaches_drawdown(self, equity: float) -> bool:
        """HWM 대비 누적DD 한도 초과인가 — 스케일급변 재seed 가 '진짜 대손실'을 지우는 것을 막는 판별자.

        HWM 은 영속·단조이고 up-jump(과대보고)에서 오염되지 않으므로, 직전 실행값(prior/baseline)과
        달리 단발 브로커 오독에 흔들리지 않는 유일한 기준선이다. HWM 미설정이면 판단근거 없음(False).
        """
        hwm = self.state.get("hwm")
        return bool(hwm and hwm > 0 and equity / hwm - 1.0 < -self.cfg.max_total_drawdown)

    def roll_day(self, equity: float):
        """날짜 바뀌면 당일 baseline(직전일 마지막 자산) 기록 + 일일손실 정지 자동해제.

        baseline = 직전 실행 자산 → 일일손실이 'first-touch 시점'이 아니라 day-over-day
        기준으로 측정됨 (M4). 첫 실행이라 직전값 없으면 현재 자산.
        ※에러 윈도우(recent)는 날짜와 무관하게 '최근 N회 실행' 기준으로 유지 — 매일 1회씩
          실패하는 feed 도 누적돼 트립되도록 (일일 리셋하면 cron 모드서 영원히 안 트립).
        """
        self._require_finite_equity(equity)   # NaN/inf 자산 → fail-closed (오염 state 영속·가드우회 차단)
        self.run_equity = equity   # 명목캡 비례화용 이번 실행 자산 (비영속)
        self._scale_jump_this_run = False   # 이번 실행이 스케일급변(브로커 오독)인지 — check_total_drawdown 이 참조
        prior = self.state.get("last_equity")
        # 스케일 급변(직전 대비 >5배 또는 <1/5)이면 baseline·hwm 을 현재로 재seed — 잔여 혼입·외부변경·
        # cash_cap 대폭변경 시 옛 스케일 기준 손실판정이 무의미해지는 것 차단(정상 일중변동은 5배 미만).
        if prior is not None and prior > 0 and (equity / prior > 5.0 or equity / prior < 0.2):
            # 스케일 급변 시 day baseline·last 만 재seed. hwm(누적DD 영속·단조증가 불변식)은 하향 금지 —
            # 단발 브로커 오독이 진짜 고점을 영구 소실시켜 GUARD-1 을 무력화하던 것 차단(max 보존).
            self._scale_jump_this_run = True   # check_total_drawdown 이 이 실행서 HWM 을 올리지 않게 하는 신호
            _hwm = self.state.get("hwm")
            # HWM 방향 인식(check_daily_loss down-jump 재seed 와 대칭):
            #  · down-jump(equity<prior*0.2) → **HWM 대비로도 누적DD 한도를 넘으면 재seed 금지**.
            #    무조건 재seed 하던 종전 코드는 −85% 대손실을 baseline·HWM 양쪽에서 지워
            #    daily_loss·total_drawdown 를 모두 통과시켰다(−20% 트립인데 −85% 무트립 = 비단조).
            #    HWM 은 up-jump 에서 보존되므로 '직전 과대보고 → 정상복귀' 왕복은 여기서 손실 0 으로
            #    판정돼 그대로 흡수된다(정당 출금·cash_cap 축소·transient 과소보고 방어 유지).
            #    한도를 넘는 급락은 재seed 하지 않고 아래 check_total_drawdown 이 트립한다(fail-closed).
            #  · up-jump(과대보고 의심) → HWM 보존(인플레된 단발 오독이 고점 영구오염하는 것 차단).
            down = equity < prior * 0.2
            new_hwm = equity if (_hwm is None or (down and not self._breaches_drawdown(equity))) else _hwm
            self.state.update(day=self.today, day_start_equity=equity, last_equity=equity, hwm=new_hwm)
            if self.state.get("halt_kind") == "daily_loss":
                self.state.update(halted=False, reason="", halt_kind="")
            self._save()
            return
        if self.state.get("day") != self.today:
            base = prior if prior is not None else equity
            self.state.update(day=self.today, day_start_equity=base)
            # 일일손실로 인한 정지만 새 날에 자동 해제 (수동 HALT/바운드 정지는 유지)
            if self.state.get("halt_kind") == "daily_loss":
                self.state.update(halted=False, reason="", halt_kind="")
        self.state["last_equity"] = equity   # 매 실행 최신 자산 → 다음날 baseline 원천
        self._save()

    def resume_if_new_day(self):
        """새 거래일이면 '일일손실' 정지만 자동 해제 (equity 불필요 → is_halted 確認 前 호출).

        roll_day 도 같은 해제를 하지만 roll_day 는 equity(브로커 조회)가 필요해
        _run_once_locked 의 is_halted 조기반환 뒤에야 호출됨 → 일일손실 정지가 새 날에도
        영구화되던 버그 방지. 수동 HALT·바운드/손상 정지는 건드리지 않음(수동 reset 필요).
        """
        if (self.state.get("day") != self.today
                and self.state.get("halt_kind") == "daily_loss"):
            # 정지 해제 시 stale day_start_equity 도 무효화 → 다음 check_daily_loss 가 당일 baseline 재seed.
            # roll_day 미호출 경로(run_exit)에서 옛 baseline 으로 손실판정이 어긋나는 것 차단.
            self.state.update(halted=False, reason="", halt_kind="", day_start_equity=None)
            self._save()

    # --- 체크 (위반 시 trip + HaltError) ---
    def _require_finite_equity(self, equity: float):
        # NaN/inf 자산은 모든 손실비교(dd < -limit)를 False 로 통과시켜 가드를 무력화 →
        # check_targets 의 NaN비중 차단과 동일하게 fail-closed. (데이터/브로커 결함 신호)
        # equity<=0 도 fail-closed — 토스 get_account 가 키 결측 시 0.0 coerce 하면
        # check_total_drawdown 의 hwm<=0 통과 분기로 손실가드가 우회되던 것 차단(라이브 자산은 항상 >0).
        if not math.isfinite(equity) or equity <= 0:
            self.trip(f"비정상 자산값({equity}) — 데이터/브로커 결함, 손실판정 불가",
                      kind="bad_equity")
            raise HaltError(self.state["reason"])

    def check_daily_loss(self, equity: float):
        self._require_finite_equity(equity)
        base = self.state.get("day_start_equity")
        if base is None:
            # baseline 미설정(최초 실행 or same-day reset 후 None) — 현재 자산으로 first-touch seed.
            # 안 하면 동일 거래일 reset 후 잔여 시간 일일손실 가드가 무방비(다음 roll_day 까지 공백).
            self.state["day_start_equity"] = equity
            self._save()
            return 0.0
        if base <= 0:                        # 비정상 자산(브로커 0/음수 반환 등) — fail-closed
            self.trip(f"비정상 baseline 자산 {base} — 손실판정 불가", kind="bad_baseline")
            raise HaltError(self.state["reason"])
        # baseline 스케일 오염 방어(defense-in-depth) — base 가 현재 자산과 >5배/<1/5 어긋나면
        # 손실이 아니라 옛 스케일(예: paper $100k 잔재) baseline 이다. roll_day 의 same-day 경로는
        # day_start 를 안 고쳐, day_start 만 옛 스케일이고 last_equity·equity 는 옳은 스케일이면
        # 여기서 -99.97% false daily_loss 트립이 났다. 트립 대신 현재로 재seed (roll_day 와 동일 임계).
        # ※ 단 down 쪽 재seed 는 HWM 대비로도 누적DD 한도를 넘으면 하지 않는다 — 그건 baseline 오염이
        #   아니라 진짜 대손실이고, 재seed 하면 손실이 클수록 안 잡히는 비단조가 된다(roll_day 와 동일 판별).
        ratio = equity / base
        if ratio > 5.0 or (ratio < 0.2 and not self._breaches_drawdown(equity)):
            self.state["day_start_equity"] = base = equity
            if self.state.get("halt_kind") == "daily_loss":
                self.state.update(halted=False, reason="", halt_kind="")
            self._save()
            return 0.0
        dd = equity / base - 1.0
        if dd < -self.cfg.max_daily_loss:
            self.trip(f"일일손실 한도 초과: {dd:.2%} < -{self.cfg.max_daily_loss:.0%}",
                      kind="daily_loss")
            raise HaltError(self.state["reason"])
        return dd

    def check_total_drawdown(self, equity: float):
        """고점(HWM) 대비 누적 드로다운 한도 — 일일한도를 밑도는 다일 그라인드다운 차단(GUARD-1).

        HWM 는 영속·단조증가. 트립은 daily_loss 와 달리 새 날에도 자동해제 안 됨(중대 — 수동 확인).
        """
        self._require_finite_equity(equity)   # NaN 이 max()로 HWM 영속 오염되는 것 차단
        hwm = self.state.get("hwm")
        # HWM 단조증가 — 단 스케일급변(브로커 오독 의심 >5x/<0.2x) 실행에선 HWM 을 올리지 않는다.
        # 인플레된 단발 오독이 고점을 영구 오염시켜 이후 정상 자산을 과민 트립시키는 것 차단(다음 실행 확인).
        scale_jump = getattr(self, "_scale_jump_this_run", False)
        if hwm is None:
            hwm = equity
        elif not scale_jump:
            hwm = max(hwm, equity)
        self.state["hwm"] = hwm
        self._save()
        if hwm <= 0:
            return 0.0
        dd = equity / hwm - 1.0
        # 단발 브로커 오독(정산지연·부분조회로 자산 과소/과대보고)은 roll_day 가 scale-jump 로 흡수:
        # down-jump 은 HWM 을 현재로 재seed(→여기 dd≈0), up-jump 은 dd>0 → 어느 쪽도 여기서 트립 안 남.
        # 따라서 별도 디바운스 없이, *정상 실행*의 한도 위반(진짜 다일 그라인드다운)은 즉시 트립(지연 없음).
        if dd < -self.cfg.max_total_drawdown:
            self.trip(f"누적 드로다운 한도 초과: {dd:.2%} < -{self.cfg.max_total_drawdown:.0%} "
                      f"(고점 {hwm:,.0f} → {equity:,.0f})", kind="total_drawdown")
            raise HaltError(self.state["reason"])
        return dd

    def check_targets(self, weights: dict):
        viol = []
        for t, w in weights.items():
            if not math.isfinite(w):                 # NaN/inf 비중 — 가드 우회 차단
                viol.append(f"{t} 비중 비정상값({w})")
            elif w > self.cfg.max_position_weight + 1e-9:
                viol.append(f"{t} 비중 {w:.0%} > 한도 {self.cfg.max_position_weight:.0%}")
        gross = sum(weights.values())
        if gross > self.cfg.max_gross + 1e-9:
            viol.append(f"총노출 {gross:.0%} > 한도 {self.cfg.max_gross:.0%}")
        if viol:
            self.trip("포지션 바운드 위반: " + "; ".join(viol), kind="position_bound")
            raise HaltError(self.state["reason"])

    def check_order_notional(self, notional: float, symbol: str):
        # 절대 fat-finger 캡 + 자산 비례 캡(작은 계좌선 2배 사이징버그도 트립). run_equity 미설정 시 절대캡만.
        limit = self.cfg.max_order_notional
        if self.run_equity and self.run_equity > 0:
            limit = min(limit, self.cfg.max_position_weight * self.run_equity
                        * self.cfg.order_notional_buffer)
        if notional > limit:
            self.trip(f"주문 명목 초과(fat-finger?): {symbol} {notional:,.0f} > {limit:,.0f}",
                      kind="order_notional")
            raise HaltError(self.state["reason"])

    def _push_outcome(self, is_error: int):
        recent = list(self.state.get("recent", []))
        recent.append(is_error)
        recent = recent[-self.cfg.error_window:]   # 최근 N회만 유지
        self.state["recent"] = recent
        self.state["errors"] = sum(recent)         # 윈도우 내 에러 수
        self._save()

    def record_error(self, msg: str = ""):
        """에러 1회 기록. 최근 윈도우 내 에러가 한도 이상이면 정지.

        '연속'이 아니라 롤링 윈도우 — 성공/실패 교차(flapping)도 누적돼 트립됨.
        """
        self._push_outcome(1)
        if self.state["errors"] >= self.cfg.max_consecutive_errors:
            self.trip(f"최근 {len(self.state['recent'])}회 중 에러 "
                      f"{self.state['errors']}회: {msg}", kind="error")   # kind='error' → 진입 차단·청산 허용(_EXIT_OK_HALTS)
            raise HaltError(self.state["reason"])

    def record_success(self):
        """성공 1회 기록 (윈도우에 추가 — 카운트를 0으로 리셋하지 않음)."""
        self._push_outcome(0)

    # --- 멱등성 (당일 1회 거래 락) ---
    def already_traded(self) -> bool:
        """오늘 이미 리밸런스를 실행했는지. cron 재시도·크래시 재실행 중복매매 방지."""
        return self.state.get("last_traded_day") == self.today

    def mark_traded(self):
        self.state["last_traded_day"] = self.today
        self._save()


class GuardedBroker:
    """브로커 경계 가드 — 모든 place_order 가 킬스위치를 통과하도록 강제.

    가드를 caller 관례가 아니라 체결 경계에서 강제. Executor.rebalance 직접 호출 등
    어떤 경로로 들어와도 우회 불가. 주문마다 HALT 재확인 → 루프 중간 정지도 즉시 반영.
    fail-closed: 정지/명목초과면 HaltError.
    """
    def __init__(self, inner, killswitch: "KillSwitch"):
        self._inner = inner
        self._ks = killswitch

    def place_order(self, req):
        halted, reason = self._ks.is_halted()
        if halted:
            # 손실성 자동정지(daily_loss/total_drawdown) 중에도 '위험 축소' 매도(청산)는 허용 —
            # 손절이 손실한도의 본래 목적. 매수(위험증가)와 그 외 정지(수동 HALT·데이터·에러·바운드)는
            # 차단(fail-closed). 롱온리 시스템이라 SELL 은 항상 보유 축소.
            exit_blk, _ = self._ks.exit_blocked()
            if req.side != Side.SELL or exit_blk:
                raise HaltError(reason)
        amt = getattr(req, "amount", None)
        if amt is not None:
            # 금액주문(소수주 매수) — 명목 = 주문금액 자체(가격 불필요). 시세결측이 매수를 막지 않게
            # quote 결측 reject 를 건너뜀(가용성↑). fat-finger·자산비례 캡은 금액으로 그대로 적용.
            if req.side == Side.BUY:
                self._ks.check_order_notional(float(amt), req.symbol)
            return self._inner.place_order(req)
        try:
            price = self._inner.get_quote(req.symbol).last
        except Exception:
            # 시세 조회 불가(영속 보유가 당일 패널서 빠짐 등). 매도(청산)는 위험축소라 시세없어도 inner 위임
            # → PaperBroker 평단 폴백 청산에 도달(책 동결 방지). 매수는 사이징·명목 판정 불가라 재raise.
            if req.side == Side.BUY:
                raise
            return self._inner.place_order(req)
        bad = price is None or price != price or price <= 0   # None/NaN/0/음수
        if bad:
            # 매수는 사이징·명목 판정 불가 → 거부. 매도(청산)는 위험 축소 방향이라 허용.
            if req.side == Side.BUY:
                raise ValueError(f"{req.symbol} 비정상 시세 {price} — 매수 거부")
        elif req.side == Side.BUY:
            # fat-finger 명목캡(GUARD-2)은 매수(과매수) 방향 가드 — 위험축소 SELL(청산)이 캡에 걸려
            # 차단·영구정지되던 논리역전 방지. SELL 수량은 보유분에 bound(슬리브·토스)라 과매도 제한적.
            self._ks.check_order_notional(req.qty * price, req.symbol)
        return self._inner.place_order(req)

    def __getattr__(self, name):
        # _inner/_ks 설정 전 접근(deepcopy/unpickle) 시 무한재귀 방지
        if name in ("_inner", "_ks"):
            raise AttributeError(name)
        # place_order 외 모든 호출(get_account/get_positions/get_quote/connect 등)은 위임
        return getattr(self._inner, name)
