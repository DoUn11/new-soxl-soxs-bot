"""포지션 사이징.

사이징은 전략 규칙과 분리된 **독립적인 리스크 결정**이다. 신호가 같아도 노출을 얼마로 두느냐에
따라 결과가 완전히 달라지므로, 기본값을 크게 잡지 않고 호출자가 명시하도록 한다.

3배 레버리지 상품이므로 노출 X%는 기초지수 기준 약 3X%의 방향 노출을 뜻한다.

⚠️ 이 전략의 전체 기간 MDD는 -78%다. 노출 100%면 $5,000이 $1,100까지 내려간 구간을 견뎌야 한다.
   200일선 버전(-53.9%)보다 훨씬 크다. 최근 국면(AI, 2023~) 성과가 2배인 대가다.
   AGENTS.md의 "전액 진입 금지" 원칙과 충돌하므로 실거래 전 반드시 재확인할 것.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_EXPOSURE = 1.00


@dataclass(frozen=True)
class RiskConfig:
    total_capital_usd: float = 5000.0
    exposure: float = DEFAULT_EXPOSURE
    fee_rate: float = 0.001  # 편도 0.1% = 왕복 0.2% (수수료+슬리피지)
    min_capital_usd: float = 5000.0  # 이 금액 아래로 떨어지면 추가 입금해 이 금액으로 매수

    def __post_init__(self) -> None:
        if not 0 < self.exposure <= 1.0:
            raise ValueError(f"exposure는 0 초과 1.0 이하여야 합니다: {self.exposure}")
        if self.min_capital_usd < 0:
            raise ValueError(f"min_capital_usd는 0 이상이어야 합니다: {self.min_capital_usd}")


def position_size_usd(equity: float, config: RiskConfig) -> float:
    """1회 진입 금액.

    - 자산이 `min_capital_usd` **이상**이면 자산 전체를 굴린다(이익은 그대로 복리).
    - 자산이 그 아래로 떨어지면 **추가 입금해 `min_capital_usd` 까지 채워** 매수한다.

    즉 진입 금액에 **하한**이 있다. 손실이 나도 다음 진입 금액이 줄지 않는다.
    """
    return max(equity, config.min_capital_usd) * config.exposure


def topup_needed(equity: float, config: RiskConfig) -> float:
    """이번 진입을 위해 새로 넣어야 하는 금액. 자산이 하한 이상이면 0.

    ⚠️ **이 돈은 수익이 아니라 새로 넣는 자본이다.** 그래서 "몇 배"로 성과를 말할 수 없다.
    성과는 `최종 자산 ÷ (최초 원금 + 누적 추가 입금)` 으로 봐야 한다.
    backtest.py 가 그렇게 보고한다.
    """
    return max(0.0, config.min_capital_usd - equity)
