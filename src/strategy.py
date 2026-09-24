"""전략 규칙 — 레짐마다 다른 메커니즘.

  상승: SOXL 추세 추종 (레짐 진입 시 매수, 레짐 이탈 또는 트레일링 25%에 청산)
  횡보: 박스권 양방향 평균회귀 (하단→SOXL, 상단→SOXS, 중심선 부근에서 청산)
  하락: SOXL 롱 평균회귀 (반등 매수. SOXS는 쓰지 않는다)

**하락장에 SOXS를 쓰지 않는 이유** (docs/STRATEGY.md 3장, 세 번 검증됨):
  - SOXS 추세추종   → 전체 기간 0.17배 (79건, 승률 16%)
  - SOXS 숏 회귀    → 59배
  - 거래 안 함(현금) → 57배
  - **SOXL 롱 회귀 → 244배**  ← 채택
  하락 구간은 평균 12일로 짧고 반등이 섞여 있으며, 변동성이 상승장의 1.5배라 3배 상품의
  디케이가 극심하다. 하락에 베팅하는 것보다 하락 중의 반등을 짧게 먹는 쪽이 낫다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from regime import DOWN, SIDEWAYS, UP


@dataclass(frozen=True)
class TrendRule:
    """상승 레짐. 고정 익절 없이 추세가 끝날 때까지 보유한다."""

    trail: float = 0.25   # 보유 중 고점 대비 이만큼 되돌리면 청산


@dataclass(frozen=True)
class MeanReversionRule:
    """밴드 이탈 평균회귀. 횡보·하락 레짐에서 쓴다."""

    long_ok: bool
    short_ok: bool
    entry_z: float        # |z| >= 이 값이면 밴드 이탈로 보고 진입
    exit_z: float | None  # 되돌림 목표. None이면 z 목표 없이 꺾임으로만 청산. 부호는 아래 참고
    take_profit: float | None   # None이면 고정 익절 없이 밴드 도달로만 익절
    stop_loss: float | None     # None이면 고정 손절 없이 단기 추세 이탈로만 손절
    time_stop: int | None       # None이면 보유일 제한 없음
    use_trend_break: bool = False   # True면 단기 추세 이탈을 손절로 쓴다
    stop_sigma: float | None = None # 진입일 σ의 몇 배만큼 역방향으로 가면 손절인가
                                    # (신호 종목 SOXX 가격 기준. 진입 시점에 고정된다)

    # exit_z 부호 규칙 (long 기준으로 읽는다):
    #   exit_z = -1.0 → 진입한 쪽 근처(z=-1.0)에서 청산. 이동폭 0.5σ
    #   exit_z =  0.0 → 중심선에서 청산. 이동폭 1.5σ
    #   exit_z = +1.0 → **반대편 밴드**에서 청산. 이동폭 2.5σ
    # short은 부호를 뒤집어 대칭으로 적용한다(-exit_z).


UP_RULE = TrendRule(trail=0.25)
# 횡보: 박스 하단→SOXL, 상단→SOXS.
# 익절 = **중심선 복귀(z=0)**, 손절 = 레짐 이탈 또는 반등 후 꺾임.
#
# 익절 목표를 중심선으로 둔 근거(docs/STRATEGY.md 6.4 파라미터 탐색):
#   반대편 밴드(±1.5, ±1.0, ±0.8)는 횡보 레짐(평균 12거래일) 안에 거의 도달하지 못해
#   실측 발동이 0~2건이었다. 중심선은 **실제로 발동한다**(최근 3년 횡보 16건 중 6건).
#   그리고 학습구간(1.55→2.00배)과 검증구간(37.74→43.25배)이 **둘 다** 개선된
#   유일한 선택지다. 한쪽만 좋아지는 값은 과최적화로 보고 채택하지 않았다.
#
# **손절은 쓰지 않는다 (2026-09-23 최종).** 하루 동안 고정 10% → 0.85σ → 제거 순으로
# 갔다. 계기는 추세 전환 초입에서 평균회귀 숏이 물린 사고였다(2026-09-22 SOXS 진입
# 직후 -5.3%). 하지만 **평가 구간을 최근 5년으로 바꾸자 어느 손절도 개선이 아니었다**:
#   손절 없음  → 학습2.5년 1.61 / 검증2.5년 20.80 / 최근5년 29.65배
#   고정 10%   → 1.42 / 21.59 / 27.36배
#   0.85σ      → 1.18 / 20.80 / 21.62배   (0.5~1.7σ 어느 값도 개선 없음)
# 5년간 σ 손절 발동이 3건뿐이라 통계적으로 구분되는 차이는 아니지만, 개선 근거가 없는
# 규칙을 남겨둘 이유도 없다. 고정 10%를 정당화했던 수치는 이제 쓰지 않는 11년 학습
# 구간에서 나온 것이었다.
#
# 청산은 레짐 이탈 / 중심선(z=0) 복귀 / 반등 후 꺾임 셋으로 한다. 레짐 전환이 평균
# 12거래일로 빨라 대부분의 청산이 손절선에 닿기 전에 나온다.
# `stop_loss`(평가손익 %)와 `stop_sigma`(진입일 σ 배수) 기능은 지우지 않고 남겨 뒀다 —
# stop_hit() 독스트링에 설계 근거가 있으니 다시 켜고 싶으면 값만 넣으면 된다.
# docs/STRATEGY.md 4.5 참고.
SIDEWAYS_RULE = MeanReversionRule(True, True, entry_z=1.7, exit_z=0.0,
                                  take_profit=None, stop_loss=None, time_stop=None,
                                  use_trend_break=True, stop_sigma=None)
# 하락: 롱만(반등 매수). 익절 = 레짐 이탈 / z가 0(중심선) 복귀 / 반등 추세 이탈.
# 횡보와 마찬가지로 고정 %는 익절·손절 모두 쓰지 않는다. "반등 추세 이탈"은 횡보의
# 단기 추세 이탈과 같은 판정이다 — 단기선을 회복했다가 다시 잃으면 반등이 끝난 것으로 본다.
DOWN_RULE = MeanReversionRule(True, False, entry_z=2.0, exit_z=0.0,
                              take_profit=None, stop_loss=None, time_stop=5,
                              use_trend_break=True)


@dataclass(frozen=True)
class Params:
    bb_period: int = 14        # 볼린저 기간. z와 하락 레짐 진입·청산 기준도 이 값을 쓴다
    bb_std: float = 2.0
    trend_ma: int = 5          # 단기 추세선. 횡보 손절(추세 이탈) 판정에 쓴다
    regime_ma: int = 50
    regime_slope_days: int = 20
    regime_price_days: int = 20   # 가격 자체의 추세 확인 창 (regime.classify 참고)
    lt_days: int = 120            # 장기 추세 거부권의 관측 창 (lt_veto 참고)
    lt_band_sigma: float = 0.5    # 거부권 데드밴드 = 이 배수 × 120일 수익률의 표준편차
    lt_min_periods: int = 252     # 표준편차를 신뢰할 최소 표본. 그전에는 거부권이 없다
    tickers: dict = field(default_factory=lambda: {"signal": "SOXX", "long": "SOXL", "short": "SOXS"})


def build_signals(ohlc: pd.DataFrame, params: Params) -> pd.DataFrame:
    """SOXX 일봉으로 레짐과 z-score를 계산한다. z=+2는 밴드 상단, z=-2는 하단."""
    from regime import classify

    close = ohlc["Close"]
    mid = close.rolling(params.bb_period).mean()
    sigma = close.rolling(params.bb_period).std()
    frame = pd.DataFrame({"close": close})
    frame["z"] = (close - mid) / sigma
    frame["sigma"] = sigma                                      # σ 기준 손절가 계산용
    frame["ma_short"] = close.rolling(params.trend_ma).mean()   # 단기 추세 판정용
    frame["ma_slope"] = frame["ma_short"].diff()                # 반등/꺾임 방향
    frame["regime"] = classify(close, params.regime_ma, params.regime_slope_days,
                               params.regime_price_days)

    # 장기 추세 — 거부권 판정용. 밴드는 **그 시점까지의 과거만** 보고 정한다.
    chg = close.pct_change(params.lt_days)
    band = params.lt_band_sigma * chg.expanding(min_periods=params.lt_min_periods).std()
    frame["lt_chg"] = chg
    frame["lt_trend"] = None
    frame.loc[chg > band, "lt_trend"] = UP
    frame.loc[chg < -band, "lt_trend"] = DOWN
    frame.loc[chg.between(-band, band), "lt_trend"] = SIDEWAYS
    return frame


def mr_rule_for(regime: str) -> MeanReversionRule | None:
    if regime == SIDEWAYS:
        return SIDEWAYS_RULE
    if regime == DOWN:
        return DOWN_RULE
    return None


def mr_entry_side(row: pd.Series, rule: MeanReversionRule) -> str | None:
    if rule.long_ok and row["z"] <= -rule.entry_z:
        return "long"
    if rule.short_ok and row["z"] >= rule.entry_z:
        return "short"
    return None


def short_trend_broken(side: str, hist: pd.DataFrame) -> bool:
    """**반등 후 꺾임** 판정. hist는 진입일부터 오늘까지의 signals 구간.

    반등 = 단기선(5일)의 기울기가 포지션에 유리한 방향으로 돌아선 구간
           (롱이면 오름세, 숏이면 내림세)
    꺾임 = 그 뒤 기울기가 다시 반대로 돌아선 시점 → 반등이 끝난 것으로 보고 청산

    밴드 이탈 진입은 정의상 단기 추세를 거스르므로(하단 매수 시 단기선은 아직 내림세),
    **반등이 한 번 확인된 뒤에만** 무장(arm)한다. 무장 없이 판정하면 진입 당일 전부
    청산된다. 하루짜리 반전이 아니라 이동평균 기울기를 쓰는 이유는 종가 한 번 밀린
    것을 "추세가 꺾였다"로 읽지 않기 위해서다.

    백테스트와 알리미가 같은 함수를 쓰도록 상태 대신 구간을 받는다.
    """
    if len(hist) < 2:
        return False
    rebounding = (hist["ma_slope"] > 0) if side == "long" else (hist["ma_slope"] < 0)
    return bool(not rebounding.iloc[-1] and rebounding.iloc[:-1].any())


def stop_hit(side: str, row: pd.Series, rule: MeanReversionRule,
             hist: pd.DataFrame | None) -> bool:
    """σ 기준 손절 — **진입일에 고정된 가격**에 닿았는가.

    익절은 진입 z(±1.7)에서 중심선(0)까지 1.7σ를 먹는 구조다. 손절을 그 절반인
    0.85σ로 두면 손익비가 1:2가 된다. 사용자 지시(2026-09-23)다.

    왜 %가 아니라 σ인가: 고정 %(이전의 10%)는 변동성이 낮은 구간에서 너무 멀고 높은
    구간에서 너무 가깝다. σ는 진입 근거와 같은 자로 잰다 — 밴드 이탈로 들어갔으면
    밴드의 폭으로 손절을 잰다.

    왜 **진입일의** σ로 고정하는가: σ는 매일 변하므로 그때그때 다시 계산하면 손절선이
    움직인다. 변동성이 커지면 손절선이 멀어져 손실이 커지고, 줄어들면 멀쩡한 포지션이
    잘린다. 진입 시점에 한 번 정해 두는 편이 규칙이 분명하다.

    기준 가격은 **SOXX(신호 종목)**다. SOXL/SOXS의 평가손익이 아니다 — 레버리지 배수와
    일간 재조정 때문에 3배 ETF의 손익률은 SOXX의 움직임과 정확히 비례하지 않는다.
    """
    if rule.stop_sigma is None or hist is None or len(hist) == 0:
        return False
    entry = hist.iloc[0]
    if pd.isna(entry.get("sigma")) or pd.isna(entry.get("close")):
        return False
    offset = rule.stop_sigma * float(entry["sigma"])
    base = float(entry["close"])
    return (row["close"] <= base - offset) if side == "long" else (row["close"] >= base + offset)


def mr_exit_reason(side: str, unrealized: float, days_held: int,
                   row: pd.Series, rule: MeanReversionRule,
                   hist: pd.DataFrame | None = None) -> str | None:
    """청산 판단. 규칙은 **진입 당시 레짐**의 것을 끝까지 쓴다.

    hist를 주면 단기 추세 이탈(use_trend_break)을 판정한다. 주지 않으면 건너뛴다.
    """
    if rule.exit_z is not None:
        reverted = (row["z"] >= rule.exit_z) if side == "long" else (row["z"] <= -rule.exit_z)
        if reverted:
            return "band_revert"
    if rule.take_profit is not None and unrealized >= rule.take_profit:
        return "take_profit"
    if rule.use_trend_break and hist is not None and short_trend_broken(side, hist):
        return "trend_break"
    if rule.stop_loss is not None and unrealized <= -rule.stop_loss:
        return "stop_loss"
    if stop_hit(side, row, rule, hist):
        return "stop_loss"
    if rule.time_stop is not None and days_held >= rule.time_stop:
        return "time_stop"
    return None


def trend_exit_reason(peak_ratio: float, rule: TrendRule = UP_RULE) -> str | None:
    """상승 레짐 포지션. 레짐 이탈은 호출자가 먼저 판정한다.

    peak_ratio: 현재가 / 보유 중 최고가.
    ⚠️ 이 트레일링은 실제로 거의 발동하지 않는다. 레짐(50일선)이 먼저 바뀌기 때문이다.
       트레일 폭을 10~40%로 바꿔도 결과가 12.77~13.92배로 평평했다. 안전장치로만 남겨 둔다.
    """
    if peak_ratio <= 1 - rule.trail:
        return "trailing_stop"
    return None


# ---------------------------------------------------------------------------
# 통합 판단 — 백테스트와 알리미가 **이 아래만** 쓴다.
#
# 이 블록이 생긴 이유: 예전에는 backtest.py와 alerter.py가 각자 판단 로직을 갖고 있었고,
# 그 둘이 달라서 같은 전략이 서로 다른 성과를 냈다(전체 기간 48배 vs 83배). 백테스트는
# 청산한 당일 같은 가격에 재진입할 수 있었지만 알리미는 하루에 한 지시만 낼 수 있었다.
#
# 그래서 "청산 후 진입"이 아니라 **목표 포지션**으로 바꿨다. 매 거래일 목표 포지션을 정하고
# 현재 포지션과의 차이만 행동으로 만든다:
#
#   목표 == 현재      → HOLD    (거래 없음)
#   목표 없음/현재 있음 → CLOSE   (전량 매도)
#   목표 있음/현재 없음 → OPEN    (신규 매수)
#   목표 != 현재      → SWITCH  (같은 장에서 매도 후 매수)
#
# 이 모델의 두 가지 이점:
#   1. 같은 방향으로 재진입하는 경우가 거래가 아니라 HOLD가 된다. 예전에는 팔고 같은 가격에
#      되사면서 왕복 0.2%를 냈다(전체 기간 106회, 누적 약 19% 손실). 이제는 규칙만 갱신한다.
#   2. 방향이 바뀌는 경우(SWITCH)는 같은 장에서 두 주문으로 실행 가능하므로, 알리미가
#      백테스트와 똑같이 행동할 수 있다. 하루 늦게 재진입하는 일이 없어진다.
# ---------------------------------------------------------------------------

ACTIONS = ("HOLD", "OPEN", "CLOSE", "SWITCH", "NONE")


@dataclass(frozen=True)
class Holding:
    """보유 포지션의 **규칙 판정용** 상태. 수량·금액은 여기 없다(회계는 호출자 담당)."""

    side: str                  # "long"(SOXL) | "short"(SOXS)
    regime: str                # 진입 당시 레짐. 청산 규칙은 이 레짐의 것을 끝까지 쓴다
    kind: str                  # "trend"(상승) | "mr"(횡보·하락)
    rule: MeanReversionRule | None
    bars_held: int             # 진입 후 경과 거래일 (타임스톱용)
    peak_ratio: float = 1.0    # 현재가 / 보유 중 고점 (트레일링용)
    unrealized: float = 0.0    # 평가손익률. 현재 규칙엔 고정 %가 없어 쓰이지 않는다


def exit_reason_for(holding: Holding, row: pd.Series,
                    hist: pd.DataFrame | None = None) -> str | None:
    """보유 포지션의 청산 사유. 없으면 None."""
    if row["regime"] != holding.regime:
        return "regime_change"      # 진입 근거가 사라졌다
    if holding.kind == "trend":
        return trend_exit_reason(holding.peak_ratio)
    if holding.rule is None:
        return None
    return mr_exit_reason(holding.side, holding.unrealized, holding.bars_held,
                          row, holding.rule, hist)


def lt_veto(side: str, row: pd.Series) -> bool:
    """**장기 추세 거부권** — 장기 추세를 거스르는 평균회귀 진입을 막는가.

    2026-09-23 채택 → 같은 날 제거 → **같은 날 재채택**. 제거했던 이유는 새 백테스트
    근거가 아니라 사용자 재량이었고, 재채택도 마찬가지로 사용자 지시다(성능 수치는
    바뀐 게 없다 — 아래 표 그대로다). docs/STRATEGY.md 4.6에 세 번의 전환을 모두
    기록해 뒀다.

    횡보·하락 레짐의 진입은 "밴드를 벗어났으니 되돌아온다"는 가정이다. 그 가정이 깨지는
    곳이 **추세 전환 구간**이다. 2026-09-22에 그걸 실제로 겪었다 — SOXX가 4거래일 만에
    +12.5% 오르는데 50일선 기울기가 아직 음수라 레짐이 "횡보"로 남았고, z=+2.36을
    되돌림 신호로 읽어 SOXS를 샀다. 높은 z가 되돌림이 아니라 **돌파**였다.

    그래서 장기(120일) 추세와 반대 방향의 평균회귀 진입을 거부한다. **상승 레짐의 추세
    진입에는 적용하지 않는다** — 그쪽은 애초에 추세를 따르는 규칙이라 거부할 이유가 없다.

    왜 120일인가 (docs/STRATEGY.md 6.6):
      60~200일이 전부 검증 구간을 개선하는 넓은 고원이다. 150일이 봉우리지만(검증
      32.47배) 같은 검증 구간에 20여 설정을 이미 돌린 뒤라 봉우리는 믿지 않는다. 전체
      16년까지 보면 좋은 구간이 60~150일로 좁혀지고, 120일이 그 안쪽이면서 전체 기간
      성적이 가장 안정적이다. 250일은 무너진다(최근5년 17.51배).

    왜 표준편차 데드밴드인가: 고정 %를 쓰면 값을 하나 더 고르는 일이 된다. 밴드 정의를
    바꿔봐도 결과가 거의 같았다 — 후행 확장 0.5σ / 후행 롤링 2년 0.5σ / 고정 ±8~12%가
    최근 5년 36.79~43.69배로 모여 있다. 그중 **미래를 보지 않으면서** 값을 따로 고르지
    않아도 되는 후행 확장을 택했다.

    ⚠️ 개선의 출처가 09-22 사고와 다르다. 150일로 한쪽씩 떼어보면 검증 구간에서 **숏
    차단은 기여가 0**이었고(20.80배 = 거부권 없음과 동일) 이익은 대부분 "장기 하락에서
    롱 금지"에서 나왔다. 사고 재발 방지와 수익 개선은 같은 스위치가 아니다.

    ⚠️ **켜고 끄는 결정은 백테스트가 아니라 사용자 판단으로 갈렸다.** 다섯 구간(학습·검증·
    최근5년·최근3년·전체) 전부가 거부권이 있을 때 더 높다(예: 최근5년 29.65→42.59배).
    그 수치는 채택·제거·재채택 어느 시점에도 바뀌지 않았다 — "어떤 진입을 자동으로
    막을지"에 대한 판단이 왔다 갔다 한 것이지, 성능이 왔다 갔다 한 게 아니다.
    """
    lt = row.get("lt_trend")
    if lt is None or (isinstance(lt, float) and pd.isna(lt)):
        return False
    return (side == "short" and lt == UP) or (side == "long" and lt == DOWN)


def raw_entry_side(row: pd.Series) -> str | None:
    """거부권을 적용하기 **전**의 진입 방향. 사유 설명에만 쓴다."""
    regime = row["regime"]
    if regime == UP:
        return "long"
    rule = mr_rule_for(regime)
    return mr_entry_side(row, rule) if rule else None


def entry_side_for(row: pd.Series) -> str | None:
    """이 거래일에 새로 잡을 방향. 없으면 None."""
    side = raw_entry_side(row)
    if side is None or row["regime"] == UP:
        return side
    return None if lt_veto(side, row) else side


def entry_reason_for(row: pd.Series, side: str) -> str:
    if row["regime"] == UP:
        return "상승 레짐 추세 진입"
    return f"{row['regime']} 레짐 밴드 이탈 (z={row['z']:+.2f})"


def kind_for(regime: str) -> str:
    return "trend" if regime == UP else "mr"


def decide_action(row: pd.Series, holding: Holding | None,
                  hist: pd.DataFrame | None = None) -> dict:
    """한 거래일의 행동을 정한다. **백테스트와 알리미의 유일한 판단 경로.**

    돌려주는 키:
      action  — HOLD | OPEN | CLOSE | SWITCH | NONE
      target  — 목표 방향("long"/"short") 또는 None
      reason  — 사람이 읽을 사유
      exit_reason — 청산이 걸렸다면 그 사유 (HOLD로 끝나도 채워질 수 있다)
      restamp — True면 매매 없이 포지션의 레짐·규칙·보유일·고점을 갱신해야 한다
    """
    exit_why = exit_reason_for(holding, row, hist) if holding else None

    if holding is not None and not exit_why:
        return dict(action="HOLD", target=holding.side, reason="조건 미충족",
                    exit_reason=None, restamp=False)

    cand = entry_side_for(row)
    current = holding.side if holding else None

    if current is None:
        if cand is None:
            blocked = raw_entry_side(row)
            if blocked is not None and lt_veto(blocked, row):
                return dict(action="NONE", target=None,
                            reason=(f"장기 추세 거부권 — {row['lt_trend']} 추세"
                                    f"(120일 {row['lt_chg']:+.1%})를 거스르는 진입 차단"),
                            exit_reason=None, restamp=False)
            rule = mr_rule_for(row["regime"])
            need = f"필요 |z|≥{rule.entry_z}" if rule else "상승 레짐 대기"
            return dict(action="NONE", target=None,
                        reason=f"진입 조건 미충족 (현재 z={row['z']:+.2f}, {need})",
                        exit_reason=None, restamp=False)
        return dict(action="OPEN", target=cand, reason=entry_reason_for(row, cand),
                    exit_reason=None, restamp=False)

    # 여기부터는 보유 중 + 청산 조건 충족.
    if cand == current:
        # 같은 방향을 다시 잡을 상황이다. 팔고 되사면 비용만 나가므로 거래하지 않고
        # 규칙만 갱신한다(레짐·보유일·고점 재설정).
        return dict(action="HOLD", target=current,
                    reason=f"{exit_why} 발생했으나 같은 방향 재진입 조건 — 매매 없이 규칙 갱신",
                    exit_reason=exit_why, restamp=True)
    if cand is None:
        return dict(action="CLOSE", target=None, reason=exit_why,
                    exit_reason=exit_why, restamp=False)
    return dict(action="SWITCH", target=cand,
                reason=f"{exit_why} → {entry_reason_for(row, cand)}",
                exit_reason=exit_why, restamp=False)
