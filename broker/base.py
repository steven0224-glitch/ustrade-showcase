"""브로커 추상화 — vnpy BaseGateway 패턴 발췌.

전략/체결 분리: 전략은 목표비중만, 브로커는 주문체결만.
토스 Open API 발급 시 TossBroker(BaseBroker) 만 구현하면 라이브 전환 완료.
무인 시스템틱용으로 단순화 (틱스트리밍 불필요, 조회/주문/취소만).
"""
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional

QTY_DECIMALS = 2   # 소수주 거래 최대 소수 자릿수 — 전 페르소나 정책(둘째 자리까지만 거래)


def floor_qty(qty: float, decimals: int = QTY_DECIMALS) -> float:
    """소수주 수량을 decimals 자리로 *절사*(내림). 매수·매도 거래수량을 정책 자릿수로 강제.
    반올림 아닌 내림: 매수는 예산·매도는 보유수량 초과 방지(올림하면 과매수/과매도).
    부동소수 표현오차(예: 3.45*100=344.999) 흡수 위해 미세 nudge 후 floor."""
    if not (qty > 0):
        return 0.0
    f = 10 ** decimals
    return math.floor(qty * f + 1e-9) / f


def fmt_qty(qty) -> str:
    """체결/보유 수량 표시 — 정수는 정수로, 소수주는 최대 2자리. `:.0f` 는 0.34→'0' 오표시(알림 신뢰성 훼손).
    청산·패닉·대시보드 알림 공용(소수주 정책). NaN/None 은 '0'."""
    try:
        q = float(qty or 0)
    except (TypeError, ValueError):
        return "0"
    if not math.isfinite(q):               # NaN·±inf (int(inf)=OverflowError 크래시 차단)
        return "0"
    return str(int(q)) if q == int(q) else f"{q:.2f}"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    GTC = "GTC"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class Quote:
    symbol: str
    last: float
    bid: float
    ask: float


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float


@dataclass
class AccountInfo:
    cash: float
    equity: float          # 총자산 = cash + 보유종목 평가액
    buying_power: float


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    tif: TimeInForce = TimeInForce.DAY
    ref_price: Optional[float] = None   # 사이징 시점 기준가 — 시장가 체결 슬리피지 측정용(review 자동튜닝). 거래엔 미사용.
    amount: Optional[float] = None      # 금액주문(orderAmount, $) — 설정 시 qty 대신 달러금액으로 주문(소수주 매수). qty XOR amount.
    reason: str = ""                    # 매매 사유(전략 근거) — 저널·대시보드 기록용. 브로커 체결엔 미사용.


@dataclass
class Order:
    order_id: str
    request: OrderRequest
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    message: str = ""


class BaseBroker(ABC):
    """모든 브로커 어댑터의 공통 인터페이스 (PaperBroker / TossBroker)."""
    name = "base"

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def get_account(self) -> AccountInfo: ...

    @abstractmethod
    def get_positions(self) -> list: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    def place_order(self, req: OrderRequest) -> Order: ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool: ...

    @abstractmethod
    def get_order(self, order_id: str) -> Order: ...

    # 편의 메서드 (공통 구현)
    def get_position(self, symbol: str) -> Optional[Position]:
        for p in self.get_positions():
            if p.symbol == symbol:
                return p
        return None
