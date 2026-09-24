"""시장 데이터 수집 (docs/STRATEGY.md 3장의 data_fetcher 모듈).

현재는 yfinance만 사용한다. 토스증권 Open API 연동은 미구현(6장 TODO).
이 모듈은 데이터 수집만 담당하며 지표·신호·주문 로직을 포함하지 않는다.

⚠️ 레버리지 ETF 데이터 주의사항
- SOXL/SOXS는 리버스 스플릿(액면병합)이 잦다. Yahoo/yfinance는 과거 가격을 항상 "현재 주식수 기준"으로
  소급 조정해 제공하므로(auto_adjust와 무관하게 스플릿은 항상 반영), 로그에 찍히는 절대가격은 그날 실제
  호가가 아니다. 같은 조정 기준선 위에서 계산한 구간 수익률(%, $)은 정확하다.
- 스플릿 경계에서 조정 오류로 보이는 값이 섞인다. 2026-05-26 SOXS 일간수익률은 -94.58%로 기록되지만
  같은 날 SOXX는 +6.10%여서 이론값(-18.29%)과 전혀 맞지 않는다. 16.5년(4,158거래일) 중 이런 날은 4일이며,
  분석 결과를 뒤집을 정도는 아니었으나(docs 8장) SOXS 단독 전략을 백테스트할 때는 반드시 이상치를 점검할 것.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf


MARKET_TZ = "America/New_York"
REGULAR_CLOSE_MIN = 16 * 60      # 16:00 ET
SETTLE_BUFFER_MIN = 5            # 마감 직후 데이터가 확정될 시간


def drop_incomplete_bar(df: pd.DataFrame, now_et: pd.Timestamp | None = None) -> pd.DataFrame:
    """**미완성 당일 봉을 잘라낸다.**

    전략은 "일봉 종가"로 판단하는데, 미국 정규장이 열려 있는 동안 yfinance를 호출하면
    당일 봉이 진행 중인 상태로 섞여 들어온다(Close = 현재가). 그대로 쓰면 종가가 아닌
    장중 가격으로 레짐과 z를 계산하게 되고, 백테스트가 검증한 것과 다른 입력이 된다.

    이게 실전에서 반드시 문제가 되는 이유: executor는 **정규장 중**(09:30~15:00 ET)에만
    주문을 접수한다. 즉 주문을 내는 바로 그 시간이 당일 봉이 미완성인 시간이다.

    판정: 마지막 봉의 날짜가 **미국 동부 기준 오늘**이고 아직 16:05 ET가 지나지 않았다면
    그 봉을 버린다. 마감 후에는 완성된 봉이므로 그대로 쓴다.
    """
    if df.empty:
        return df
    now = now_et if now_et is not None else pd.Timestamp.now(tz=MARKET_TZ)
    minutes = now.hour * 60 + now.minute
    if df.index[-1].date() == now.date() and minutes < REGULAR_CLOSE_MIN + SETTLE_BUFFER_MIN:
        return df.iloc[:-1]
    return df


def fetch_history(ticker: str, period: str, drop_partial: bool = True) -> pd.DataFrame:
    """일봉 OHLC를 가져온다 (auto_adjust=True 고정, 타임존 제거).

    `drop_partial=True`(기본)면 미완성 당일 봉을 버린다. drop_incomplete_bar 참고.
    """
    df = yf.Ticker(ticker).history(period=period, interval="1d", auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"{ticker} 데이터를 받아오지 못했습니다.")
    df.index = df.index.tz_localize(None)
    if drop_partial:
        df = drop_incomplete_bar(df)
    if df.empty:
        raise RuntimeError(f"{ticker} 완성된 일봉이 없습니다.")
    return df


def fetch_4h(ticker: str) -> pd.DataFrame:
    """yfinance 1시간봉(약 3년치)으로 4시간봉을 만든다.

    미국 정규장(09:30~16:00 ET)을 09:30~13:30(1시간봉 4개) / 13:30~16:00(3개) 두 봉으로 묶는다.
    반환: 시간순 DataFrame(date, slot, Open/High/Low/Close).
    """
    h = yf.Ticker(ticker).history(period="730d", interval="1h", auto_adjust=True)
    if h.empty:
        raise RuntimeError(f"{ticker} 시간봉 데이터를 받아오지 못했습니다.")
    h.index = h.index.tz_convert("America/New_York").tz_localize(None)
    h["date"] = h.index.normalize()
    h["slot"] = (h.index.hour * 60 + h.index.minute >= 13 * 60 + 30).astype(int)
    grouped = h.groupby(["date", "slot"]).agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"), Close=("Close", "last")
    )
    return grouped.reset_index()


def fetch_closes(tickers: list[str], period: str = "max") -> pd.DataFrame:
    """여러 종목의 종가를 공통 거래일로 맞춰 한 프레임으로 반환한다 (페어 분석용)."""
    frames = {t: fetch_history(t, period)["Close"] for t in tickers}
    return pd.DataFrame(frames).dropna()
