"""백테스트 — 규칙은 strategy.py, 레짐은 regime.py, 사이징은 risk_manager.py.

체결 규칙: 신호는 당일 **종가**로 확정, 체결은 **다음 거래일 시가**로 가정 (look-ahead 방지).
비용     : 진입·청산 각각 편도 0.1% (수수료+슬리피지 합산 가정)
마지막 날 보유 중인 포지션은 종가로 강제 청산해 거래 통계에 포함한다.

⚠️ 이 파일은 과거 데이터 시뮬레이션만 한다. 실주문은 alerter.py --live 가 담당한다.
판단은 strategy.decide_action 하나만 쓰므로 여기서 나온 수치가 실거래 경로의 기대값이다
(tests/test_consistency.py 가 두 경로의 일치를 확인한다).

사용법:
    ./.venv/bin/python src/backtest.py                    # 최근 3년 (기본값)
    ./.venv/bin/python src/backtest.py --years 5          # 최근 5년
    ./.venv/bin/python src/backtest.py --since 2023-01-01 # AI 국면
    ./.venv/bin/python src/backtest.py --years 0          # 전체 기간
    ./.venv/bin/python src/backtest.py --exposure 0.5 --csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from data_fetcher import fetch_history  # noqa: E402
from risk_manager import RiskConfig, topup_needed  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


@dataclass
class Trade:
    regime: str
    side: str
    ticker: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    exit_reason: str
    ret_pct: float


def run_backtest(signals: pd.DataFrame, legs: dict[str, pd.DataFrame],
                 params: Params, risk: RiskConfig
                 ) -> tuple[pd.Series, list[Trade], list[tuple]]:
    """(자산곡선, 거래목록, 추가입금 내역) 을 돌려준다.

    추가입금 내역은 `[(날짜, 금액), ...]` 이다. **시점이 필요한 이유**: 세후 수익률을
    계산할 때 그 해의 자산 증가분에서 입금액을 빼야 한다. 빼지 않으면 새로 넣은 돈을
    이익으로 보고 과세해, 세후가 세전보다 커지는 엉뚱한 결과가 나온다.

    **자본 하한 규칙**: 진입 시점 자산이 `risk.min_capital_usd` 아래면 그만큼 새로
    입금해 채워 넣고 매수한다(risk_manager.position_size_usd 참고). 그래서 최종 자산을
    최초 원금으로 나눈 "배수"는 성과가 아니다. 반드시 **누적 추가입금을 더한 총 투입**
    으로 나눠야 한다. summarize() 가 그렇게 보고한다.
    """
    dates = signals.index
    equity = risk.total_capital_usd
    injections: list[tuple] = []
    pos: dict | None = None
    trades: list[Trade] = []
    curve = np.empty(len(dates))
    expo = risk.exposure

    def close_position(exit_price, exit_date, reason):
        nonlocal equity, pos
        gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
        equity *= 1 + expo * gross - 2 * risk.fee_rate * expo
        trades.append(Trade(regime=pos["regime"], side=pos["side"], ticker=params.tickers[pos["side"]],
                            entry_date=pos["entry_date"], exit_date=exit_date,
                            entry_price=pos["entry_price"], exit_price=exit_price,
                            exit_reason=reason, ret_pct=gross * 100))
        pos = None

    for i in range(len(dates) - 1):
        today, next_day = dates[i], dates[i + 1]
        row = signals.iloc[i]
        regime = row["regime"]
        if pd.isna(row["z"]) or pd.isna(regime):
            curve[i] = equity
            continue

        holding = None
        if pos is not None:
            leg = legs[pos["side"]]
            price = leg.loc[today, "Close"]
            pos["peak"] = max(pos["peak"], price)
            holding = Holding(
                side=pos["side"], regime=pos["regime"], kind=pos["kind"], rule=pos["rule"],
                bars_held=i - pos["entry_i"], peak_ratio=price / pos["peak"],
                unrealized=(price - pos["entry_price"]) / pos["entry_price"])

        # 판단은 strategy.decide_action 하나만 쓴다 — 알리미와 같은 경로다.
        hist = signals.iloc[pos["entry_i"]:i + 1] if pos is not None else None
        d = decide_action(row, holding, hist)

        if d["action"] in ("CLOSE", "SWITCH"):
            close_position(legs[pos["side"]].loc[next_day, "Open"], next_day, d["exit_reason"])
        elif d["action"] == "HOLD" and d["restamp"]:
            # 같은 방향 재진입 — 매매하지 않고 규칙만 갈아끼운다. 실제 체결이 없으므로
            # entry_price(원가)는 그대로 두고 레짐·규칙·보유일·고점만 재설정한다.
            pos.update(regime=regime, kind=kind_for(regime), rule=mr_rule_for(regime),
                       entry_i=i + 1, peak=0.0)

        if d["action"] in ("OPEN", "SWITCH"):
            # 자본 하한: 자산이 하한 아래면 새로 넣어 채운다.
            need = topup_needed(equity, risk)
            if need > 0:
                injections.append((next_day, need))
                equity += need
            side = d["target"]
            # peak=0 으로 시작해 **진입 봉(next_day)의 종가부터** 고점을 센다.
            # 알리미는 journal의 진입일 이후 종가로 고점을 잡으므로, 신호 봉 종가를
            # 초깃값으로 쓰면 두 경로의 트레일링 판정이 어긋난다.
            pos = dict(kind=kind_for(regime), side=side, entry_date=next_day, entry_i=i + 1,
                       entry_price=legs[side].loc[next_day, "Open"], regime=regime,
                       rule=mr_rule_for(regime), peak=0.0)

        if pos is None:
            curve[i] = equity
        else:
            leg = legs[pos["side"]]
            mtm = (leg.loc[today, "Close"] - pos["entry_price"]) / pos["entry_price"]
            curve[i] = equity * (1 + expo * mtm)

    if pos is not None:
        last = dates[-1]
        close_position(legs[pos["side"]].loc[last, "Close"], last, "open_at_end")
        curve[-1] = equity
    else:
        curve[-1] = curve[-2] if len(curve) > 1 else equity
    return pd.Series(curve, index=dates), trades, injections


def summarize(curve: pd.Series, trades: list[Trade], params: Params, risk: RiskConfig,
              injected: float = 0.0) -> pd.DataFrame:
    df = pd.DataFrame([t.__dict__ for t in trades])
    years = (curve.index[-1] - curve.index[0]).days / 365.25
    invested = risk.total_capital_usd + injected     # 총 투입 = 최초 원금 + 추가 입금
    mult = curve.iloc[-1] / invested                 # 성과는 **총 투입** 대비로 본다
    mdd = (curve / curve.cummax() - 1).min() * 100
    daily = curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0

    print("=" * 76)
    print(f"기간 {curve.index[0].date()} ~ {curve.index[-1].date()} ({years:.1f}년) | "
          f"원금 ${risk.total_capital_usd:,.0f} | 노출 {risk.exposure*100:.0f}% | 왕복비용 {risk.fee_rate*200:.1f}%")
    print(f"레짐 {params.regime_ma}일선+{params.regime_slope_days}일 기울기"
          f"+가격 {params.regime_price_days}일 | "
          "상승=SOXL 추세 / 횡보=양방향 회귀 / 하락=SOXL 롱 회귀")
    print("-" * 76)
    print(f"최종 자산      : ${curve.iloc[-1]:,.0f}")
    if injected > 0:
        print(f"총 투입        : ${invested:,.0f}  "
              f"(최초 ${risk.total_capital_usd:,.0f} + 추가입금 ${injected:,.0f})")
        print(f"순손익         : ${curve.iloc[-1] - invested:+,.0f}")
        print(f"투입대비       : {mult:.2f}배   ← 추가입금을 포함한 성과")
    else:
        print(f"투입대비       : {mult:.2f}배  (추가입금 없음)")
    print(f"CAGR           : {(mult ** (1 / years) - 1) * 100:+.2f}%" if mult > 0 else "CAGR           : n/a")
    print(f"최대 낙폭(MDD) : {mdd:.2f}%")
    print(f"Sharpe         : {sharpe:.2f}")
    if df.empty:
        print("체결된 거래가 없습니다."); print("=" * 76); return df
    print(f"총 거래        : {len(df)}건 (승률 {(df.ret_pct > 0).mean()*100:.1f}%)")
    print("-" * 76)
    print("레짐·방향별 (거래 수익률 %):")
    print(df.groupby(["regime", "side"]).agg(
        건수=("ret_pct", "size"), 승률=("ret_pct", lambda s: (s > 0).mean() * 100),
        평균=("ret_pct", "mean"), 최고=("ret_pct", "max"), 최악=("ret_pct", "min")).to_string())
    print("-" * 76)
    print("청산 사유별:")
    print(df.groupby(["regime", "exit_reason"]).agg(
        건수=("ret_pct", "size"), 평균=("ret_pct", "mean")).to_string())
    print("=" * 76)
    return df


def main() -> None:
    parser = argparse.ArgumentParser(description="SOXL/SOXS 빠른 레짐 전략 백테스트 (실주문 없음)")
    parser.add_argument("--exposure", type=float, default=None)
    parser.add_argument("--capital", type=float, default=5000.0)
    parser.add_argument("--fee", type=float, default=0.001, help="편도 비용 (기본 0.1%%)")
    parser.add_argument("--years", type=float, default=3.0, help="평가할 최근 기간(년). 0이면 전체")
    parser.add_argument("--since", default=None, help="이 날짜부터 평가 (--years보다 우선)")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    params = Params()
    risk = RiskConfig(total_capital_usd=args.capital, fee_rate=args.fee,
                      **({"exposure": args.exposure} if args.exposure is not None else {}))

    # 지표 워밍업에 평가 구간 이전 데이터가 필요하므로 항상 전체를 받는다.
    signal_df = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = signal_df.index.intersection(legs["long"].index).intersection(legs["short"].index)
    signal_df, legs = signal_df.loc[common], {k: v.loc[common] for k, v in legs.items()}

    signals = build_signals(signal_df, params).dropna(subset=["z", "regime"])
    if args.since:
        signals = signals[signals.index >= pd.Timestamp(args.since)]
    elif args.years and args.years > 0:
        signals = signals[signals.index >= signals.index[-1] - pd.DateOffset(years=args.years)]
    legs = {k: v.loc[signals.index] for k, v in legs.items()}

    curve, trades, injections = run_backtest(signals, legs, params, risk)
    df = summarize(curve, trades, params, risk, sum(a for _, a in injections))
    if args.csv and not df.empty:
        RESULTS_DIR.mkdir(exist_ok=True)
        out = RESULTS_DIR / "trades.csv"
        df.sort_values("entry_date").to_csv(out, index=False)
        print(f"\n거래 로그 저장: {out}")


if __name__ == "__main__":
    main()
