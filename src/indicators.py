"""기술적 지표 계산 함수 (볼린저밴드, ADX).

docs/STRATEGY.md 2.1~2.2 전략 정의에 사용되는 지표를 계산한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    """볼린저밴드를 계산해 중심선/상단/하단/밴드폭을 반환한다.

    밴드폭(bandwidth)은 중심선 대비 상단-하단 거리의 비율로,
    값이 작을수록 변동성이 수축된(=횡보 가능성이 높은) 구간이다.
    """
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    bandwidth = (upper - lower) / mid
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_bandwidth": bandwidth,
        }
    )


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ADX(Average Directional Index)를 Wilder 방식으로 계산한다.

    값이 낮을수록(관례적으로 20~25 미만) 추세가 약한 횡보장으로 간주한다.
    """
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder 지수평활 (alpha = 1/period)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=high.index).ewm(alpha=1 / period, adjust=False).mean() / atr

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.ewm(alpha=1 / period, adjust=False).mean()
