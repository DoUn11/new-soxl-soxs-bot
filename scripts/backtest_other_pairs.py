"""다른 종목 쌍에 **같은 규칙 그대로** 적용 — 진짜 out-of-sample 검증. (2026-09-24)

**왜 이게 중요한가.** 지금까지의 수치는 전부 SOXX 위에서 나왔다. 레짐 분류기(3.1),
횡보 진입 z(6.4), 120일 거부권(4.6) 모두 SOXX 데이터를 보고 골랐다. CLAUDE.md가 말하는
"손대지 않은 구간"이 필요한 이유다. **같은 규칙을 건드리지 않고 다른 자산에 적용하는 것**이
그 검증이다 — 파라미터를 하나도 바꾸지 않으므로 결과가 좋으면 규칙이 일반적이라는 뜻이고,
무너지면 SOXX에 과적합됐다는 뜻이다.

⚠️ **여기서 파라미터를 종목마다 다시 맞추면 검증이 아니라 과적합이 된다.** 절대 하지 말 것.

⚠️ 결과가 좋아도 **자본 분산은 별개 문제**다. 반도체·나스닥·S&P는 서로 강하게 상관돼
있어 두 쌍을 동시에 돌려도 분산이 되지 않는다 — 같은 베팅을 두 배로 하는 것에 가깝다.

사용법:
    ./.venv/bin/python scripts/backtest_other_pairs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from data_fetcher import fetch_history  # noqa: E402
from risk_manager import RiskConfig  # noqa: E402
from strategy import Params, build_signals  # noqa: E402
from backtest import run_backtest  # noqa: E402

# (신호, 롱, 숏, 설명) — 전부 3배 레버리지 롱/인버스 쌍이다.
PAIRS = [
    ("SOXX", "SOXL", "SOXS", "반도체 (현재 운용 중)"),
    ("QQQ",  "TQQQ", "SQQQ", "나스닥100"),
    ("SPY",  "SPXL", "SPXS", "S&P500"),
    ("IWM",  "TNA",  "TZA",  "소형주 러셀2000"),
    ("XLF",  "FAS",  "FAZ",  "금융"),
    ("XBI",  "LABU", "LABD", "바이오텍"),
    ("TSLA", "TSLL", "TSLQ", "테슬라 (사용자가 직접 매매 중)"),
    ("NVDA", "NVDL", "NVD",  "엔비디아"),
]


def evaluate(signal: str, long: str, short: str, params: Params, risk: RiskConfig) -> dict | None:
    try:
        sig = fetch_history(signal, "max")
        legs = {"long": fetch_history(long, "max"), "short": fetch_history(short, "max")}
    except Exception as e:
        return dict(error=f"데이터 조회 실패: {type(e).__name__}")
    if sig.empty or legs["long"].empty or legs["short"].empty:
        return dict(error="데이터 없음")

    common = sig.index.intersection(legs["long"].index).intersection(legs["short"].index)
    if len(common) < 400:
        return dict(error=f"공통 데이터 {len(common)}일뿐 (최소 400일 필요)")
    sig, legs = sig.loc[common], {k: v.loc[common] for k, v in legs.items()}
    signals = build_signals(sig, params).dropna(subset=["z", "regime"])
    if len(signals) < 300:
        return dict(error="지표 워밍업 후 데이터 부족")

    end = signals.index[-1]
    out = dict(start=signals.index[0].date(), end=end.date(), years=len(signals) / 252)
    for name, days in (("학습2.5", None), ("검증2.5", 913), ("최근5년", 1826)):
        if name == "학습2.5":
            m = (signals.index >= end - pd.Timedelta(days=1826)) & (signals.index < end - pd.Timedelta(days=913))
        else:
            m = signals.index >= end - pd.Timedelta(days=days)
        s = signals[m]
        if len(s) < 60:
            out[name] = None
            continue
        l = {k: v.loc[s.index] for k, v in legs.items()}
        curve, trades, inj = run_backtest(s, l, params, risk)
        invested = risk.total_capital_usd + sum(a for _, a in inj)
        out[name] = curve.iloc[-1] / invested
        if name == "최근5년":
            out["mdd"] = (curve / curve.cummax() - 1).min() * 100
            out["n"] = len(trades)
            r = np.array([t.ret_pct for t in trades]) if trades else np.array([0.0])
            out["wr"] = (r > 0).mean() * 100
            # 상위 2건을 빼면 얼마나 남는가 — 소수 거래 의존도
            out["ex2"] = float(np.prod(1 + np.sort(r)[:-2] / 100)) if len(r) > 2 else float("nan")
            # 매수보유 벤치마크 (반드시 함께 보고한다 — CLAUDE.md 원칙)
            lg = legs["long"].loc[s.index, "Close"]
            out["bh"] = float(lg.iloc[-1] / lg.iloc[0])
    return out


def main() -> None:
    params, risk = Params(), RiskConfig(total_capital_usd=5000.0)
    print("=" * 108)
    print("같은 규칙, 다른 종목 — 파라미터는 하나도 바꾸지 않았습니다 (진짜 out-of-sample)")
    print("=" * 108)
    print(f"{'쌍':22s}{'학습2.5':>10s}{'검증2.5':>10s}{'최근5년':>10s}{'상위2건제외':>12s}"
          f"{'MDD':>9s}{'거래':>7s}{'승률':>7s}{'롱매수보유':>11s}")
    print("-" * 108)
    for signal, long, short, desc in PAIRS:
        r = evaluate(signal, long, short, params, risk)
        label = f"{long}/{short}"
        if r is None or "error" in r:
            print(f"{label:22s}  ⚠️ {r.get('error') if r else '실패'}")
            continue
        def f(k, suf="배"):
            v = r.get(k)
            return f"{v:>8.2f}{suf}" if isinstance(v, float) and not np.isnan(v) else f"{'—':>10s}"
        print(f"{label:22s}{f('학습2.5')}{f('검증2.5')}{f('최근5년')}{f('ex2'):>12s}"
              f"{r.get('mdd', float('nan')):>8.1f}%{r.get('n', 0):>6d}건{r.get('wr', 0):>6.0f}%"
              f"{r.get('bh', float('nan')):>10.2f}배")
    print("-" * 108)
    print("※ '상위2건제외' = 가장 좋았던 거래 2건을 뺀 누적. 이 값이 1배 근처면")
    print("   성과가 소수 거래에 의존한다는 뜻이라 신뢰하기 어렵습니다.")
    print("※ 파라미터를 종목마다 다시 맞추면 검증이 아니라 과적합입니다. 하지 마세요.")
    print("=" * 108)


if __name__ == "__main__":
    main()
