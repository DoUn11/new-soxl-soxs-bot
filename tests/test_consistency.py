"""백테스트와 알리미가 **같은 판단**을 내리는지 날짜별로 대조한다.

이 테스트가 존재하는 이유: 예전에는 backtest.py와 alerter.py가 각자 판단 로직을 갖고 있어
같은 전략이 서로 다른 성과를 냈다(전체 기간 48배 vs 83배). 지금은 둘 다
strategy.decide_action 만 쓰지만, **Holding을 만드는 방식**이 다르면 다시 갈라진다
(보유일 계산, 고점 기준일, 진입 규칙 재설정 등). 그래서 행동 시퀀스를 직접 비교한다.

  - 백테스트 경로 : backtest.run_backtest 와 동일한 방식으로 Holding을 만든다(인덱스 기반).
  - 알리미 경로   : alerter.decide 와 동일한 방식으로 만든다(날짜 슬라이스 + journal 상태).

실행: ./.venv/bin/python tests/test_consistency.py
네트워크로 시세를 받으므로 몇 초 걸린다.
"""
from __future__ import annotations

import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from data_fetcher import fetch_history  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)

YEARS = 6          # 비교 구간. 길게 잡으면 느려진다.


def load():
    params = Params()
    sig = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = sig.index.intersection(legs["long"].index).intersection(legs["short"].index)
    sig, legs = sig.loc[common], {k: v.loc[common] for k, v in legs.items()}
    signals = build_signals(sig, params).dropna(subset=["z", "regime", "ma_short", "ma_slope"])
    signals = signals[signals.index >= signals.index[-1] - pd.DateOffset(years=YEARS)]
    return signals, {k: v.loc[signals.index] for k, v in legs.items()}, params


def backtest_actions(signals, legs):
    """backtest.run_backtest 와 같은 방식(정수 인덱스 상태)으로 행동 시퀀스를 만든다."""
    dates = signals.index
    pos = None
    out = []
    for i in range(len(dates) - 1):
        today, next_day = dates[i], dates[i + 1]
        row = signals.iloc[i]
        holding = None
        if pos is not None:
            price = legs[pos["side"]].loc[today, "Close"]
            pos["peak"] = max(pos["peak"], price)
            holding = Holding(side=pos["side"], regime=pos["regime"], kind=pos["kind"],
                              rule=pos["rule"], bars_held=i - pos["entry_i"],
                              peak_ratio=price / pos["peak"],
                              unrealized=(price - pos["entry_price"]) / pos["entry_price"])
        hist = signals.iloc[pos["entry_i"]:i + 1] if pos is not None else None
        d = decide_action(row, holding, hist)
        out.append((today, d["action"], d["target"], bool(d["restamp"])))

        if d["action"] in ("CLOSE", "SWITCH"):
            pos = None
        elif d["action"] == "HOLD" and d["restamp"]:
            pos.update(regime=row["regime"], kind=kind_for(row["regime"]),
                       rule=mr_rule_for(row["regime"]), entry_i=i + 1, peak=0.0)
        if d["action"] in ("OPEN", "SWITCH"):
            side = d["target"]
            pos = dict(kind=kind_for(row["regime"]), side=side, entry_i=i + 1,
                       entry_price=legs[side].loc[next_day, "Open"], regime=row["regime"],
                       rule=mr_rule_for(row["regime"]), peak=0.0)
    return out


def alerter_actions(signals, legs):
    """alerter.decide 와 같은 방식(journal 상태 + 날짜 슬라이스)으로 행동 시퀀스를 만든다."""
    dates = signals.index
    jpos = None          # journal.current_position() 이 돌려주는 것과 같은 모양
    out = []
    for i in range(len(dates) - 1):
        today, next_day = dates[i], dates[i + 1]
        row = signals.loc[today]
        holding = hist = None
        if jpos is not None:
            side = jpos["side"]
            price = float(legs[side]["Close"].loc[today])
            since = jpos["opened"]
            held = legs[side]["Close"].loc[since:today]
            peak = float(held.max()) if len(held) else price
            days = max(len(held) - 1, 0)
            hist = signals.loc[since:today]
            holding = Holding(side=side, regime=jpos["regime"], kind=kind_for(jpos["regime"]),
                              rule=mr_rule_for(jpos["regime"]), bars_held=days,
                              peak_ratio=price / peak if peak else 1.0,
                              unrealized=(price - jpos["entry_price"]) / jpos["entry_price"])
        d = decide_action(row, holding, hist)
        out.append((today, d["action"], d["target"], bool(d["restamp"])))

        if d["action"] in ("CLOSE", "SWITCH"):
            jpos = None
        elif d["action"] == "HOLD" and d["restamp"]:
            # 실거래에서는 RESTAMP 기록이 레짐과 기준일을 새로 잡는다.
            jpos.update(regime=row["regime"], opened=next_day)
        if d["action"] in ("OPEN", "SWITCH"):
            side = d["target"]
            jpos = dict(side=side, regime=row["regime"], opened=next_day,
                        entry_price=float(legs[side].loc[next_day, "Open"]))
    return out


def main() -> int:
    signals, legs, _ = load()
    a = backtest_actions(signals, legs)
    b = alerter_actions(signals, legs)
    print(f"비교 구간 {signals.index[0].date()} ~ {signals.index[-1].date()}  ({len(a)}거래일)")

    if len(a) != len(b):
        print(f"FAIL 길이 불일치: {len(a)} vs {len(b)}")
        return 1
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    counts = {}
    for _, act, _, _ in a:
        counts[act] = counts.get(act, 0) + 1
    print("행동 분포:", ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    if diffs:
        print(f"\nFAIL 두 경로가 {len(diffs)}일 다릅니다 (앞 10건):")
        for x, y in diffs[:10]:
            print(f"  {x[0].date()}  백테스트={x[1]}/{x[2]}/restamp={x[3]}  "
                  f"알리미={y[1]}/{y[2]}/restamp={y[3]}")
        return 1
    print("\nPASS 모든 거래일에서 백테스트와 알리미의 판단이 같습니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
