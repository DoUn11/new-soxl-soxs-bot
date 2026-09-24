"""신호·체결 시점 실험 — 오버나이트 갭을 없앨 수 있는가. (2026-09-24)

**동기.** 현재 구조는 "신호는 당일 **종가**, 체결은 다음 거래일 **시가**"다. 그 사이의
오버나이트 갭이 순수 슬리피지로 들어간다. 손절이 실패한 원인도 이것이었다(목표 -10%인데
실제 실현은 평균 -16.71%). 사용자 제안: **체결을 종가에 하거나, 신호를 시가 기준으로
하면 갭이 사라지지 않는가?**

측정하는 세 가지:

  A. close→next_open  (현재)  신호 = 당일 종가, 체결 = 다음날 시가
     → 갭이 통째로 슬리피지. 지금 쓰는 방식.

  B. open→same_open   (제안)  신호 = 당일 **시가** 기준, 체결 = **같은 날 시가**
     → 신호와 체결이 같은 가격. 갭이 사라진다. 룩어헤드 없음(시가를 보고 판단해 시가에
       체결한다고 가정). 실제로는 관측→주문에 몇 분 걸리므로 약간 낙관적이지만,
       A도 "공식 시가에 체결"을 가정하므로 **같은 낙관도**로 비교된다.

  C. close→same_close (참고) 신호 = 당일 종가, 체결 = **같은 날 종가**
     → **실현 불가능하다.** 종가를 알아야 신호가 나오는데 그 종가에 체결할 수는 없다.
       게다가 토스는 정규장 종료 1시간 전까지만 주문을 받는다(MOC 자체가 불가).
       갭이 얼마나 비싼지 보여주는 **상한선**으로만 쓴다.

평가는 CLAUDE.md 표준(최근 5년 = 앞 2.5년 학습 / 뒤 2.5년 검증)을 따른다.

⚠️ 실험 스크립트다. src/ 는 건드리지 않았다.

사용법:
    ./.venv/bin/python scripts/backtest_timing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from data_fetcher import fetch_history  # noqa: E402
from risk_manager import RiskConfig, topup_needed  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)


def run(signals: pd.DataFrame, legs: dict[str, pd.DataFrame], params: Params,
        risk: RiskConfig, *, fill: str) -> tuple[pd.Series, list, list]:
    """`fill` 로 체결 시점을 고른다.

      "next_open"  — 신호 다음 거래일 시가 (현재 방식)
      "same_open"  — 신호 당일 시가
      "same_close" — 신호 당일 종가 (참고용, 실현 불가)
    """
    dates = signals.index
    equity = risk.total_capital_usd
    injections: list = []
    pos: dict | None = None
    trades: list = []
    curve = np.empty(len(dates))
    expo = risk.exposure

    def px(side: str, i: int) -> float:
        leg = legs[side]
        if fill == "next_open":
            return float(leg.loc[dates[min(i + 1, len(dates) - 1)], "Open"])
        if fill == "same_open":
            return float(leg.loc[dates[i], "Open"])
        return float(leg.loc[dates[i], "Close"])

    def close_position(price, when, reason):
        nonlocal equity, pos
        gross = (price - pos["entry_price"]) / pos["entry_price"]
        equity *= 1 + expo * gross - 2 * risk.fee_rate * expo
        trades.append(dict(regime=pos["regime"], side=pos["side"],
                           entry_date=pos["entry_date"], exit_date=when,
                           entry_price=pos["entry_price"], exit_price=price,
                           exit_reason=reason, ret_pct=gross * 100))
        pos = None

    for i in range(len(dates) - 1):
        today = dates[i]
        row = signals.iloc[i]
        regime = row["regime"]
        if pd.isna(row["z"]) or pd.isna(regime):
            curve[i] = equity
            continue

        holding = None
        if pos is not None:
            leg = legs[pos["side"]]
            price = float(leg.loc[today, "Close"])
            pos["peak"] = max(pos["peak"], price)
            holding = Holding(side=pos["side"], regime=pos["regime"], kind=pos["kind"],
                              rule=pos["rule"], bars_held=i - pos["entry_i"],
                              peak_ratio=price / pos["peak"],
                              unrealized=(price - pos["entry_price"]) / pos["entry_price"])

        hist = signals.iloc[pos["entry_i"]:i + 1] if pos is not None else None
        d = decide_action(row, holding, hist)
        # 체결 봉의 인덱스 — next_open 만 다음 봉이고 나머지는 당일이다.
        fill_i = i + 1 if fill == "next_open" else i

        if d["action"] in ("CLOSE", "SWITCH"):
            close_position(px(pos["side"], i), dates[fill_i], d["exit_reason"])
        elif d["action"] == "HOLD" and d["restamp"]:
            pos.update(regime=regime, kind=kind_for(regime), rule=mr_rule_for(regime),
                       entry_i=fill_i, peak=0.0)

        if d["action"] in ("OPEN", "SWITCH"):
            need = topup_needed(equity, risk)
            if need > 0:
                injections.append((dates[fill_i], need))
                equity += need
            side = d["target"]
            pos = dict(kind=kind_for(regime), side=side, entry_date=dates[fill_i],
                       entry_i=fill_i, entry_price=px(side, i), regime=regime,
                       rule=mr_rule_for(regime), peak=0.0)

        if pos is None:
            curve[i] = equity
        else:
            leg = legs[pos["side"]]
            mtm = (float(leg.loc[today, "Close"]) - pos["entry_price"]) / pos["entry_price"]
            curve[i] = equity * (1 + expo * mtm)

    if pos is not None:
        close_position(float(legs[pos["side"]].loc[dates[-1], "Close"]), dates[-1], "open_at_end")
        curve[-1] = equity
    else:
        curve[-1] = curve[-2] if len(curve) > 1 else equity
    return pd.Series(curve, index=dates), trades, injections


def metrics(curve, trades, risk, injected):
    invested = risk.total_capital_usd + injected
    mult = curve.iloc[-1] / invested
    mdd = (curve / curve.cummax() - 1).min() * 100
    daily = curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0
    n = len(trades)
    wr = (sum(1 for t in trades if t["ret_pct"] > 0) / n * 100) if n else 0.0
    return dict(mult=mult, mdd=mdd, sharpe=sharpe, n=n, wr=wr)


def main() -> None:
    params = Params()
    risk = RiskConfig(total_capital_usd=5000.0)
    sig_df = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = sig_df.index.intersection(legs["long"].index).intersection(legs["short"].index)
    sig_df, legs = sig_df.loc[common], {k: v.loc[common] for k, v in legs.items()}

    # A/C 는 종가 기반 신호, B 는 **시가 기반 신호**다. 시가 시계열로 지표를 다시 만든다.
    sig_close = build_signals(sig_df, params).dropna(subset=["z", "regime"])
    open_df = sig_df.copy()
    open_df["Close"] = open_df["Open"]          # 시가를 '그 시점의 가격'으로 삼는다
    sig_open = build_signals(open_df, params).dropna(subset=["z", "regime"])

    end = sig_close.index[-1]
    W = {
        "학습2.5": lambda ix: (ix >= end - pd.Timedelta(days=1826)) & (ix < end - pd.Timedelta(days=913)),
        "검증2.5": lambda ix: ix >= end - pd.Timedelta(days=913),
        "최근5년": lambda ix: ix >= end - pd.Timedelta(days=1826),
        "최근3년": lambda ix: ix >= end - pd.Timedelta(days=1096),
        "전체": lambda ix: ix >= ix[0],
    }
    variants = [
        ("A 현재 (종가신호 → 다음날 시가)", sig_close, "next_open"),
        ("B 제안 (시가신호 → 당일 시가)", sig_open, "same_open"),
        ("C 참고 (종가신호 → 당일 종가) ※실현불가", sig_close, "same_close"),
    ]

    print("=" * 104)
    print("신호·체결 시점 실험 — 배수(투입대비)")
    print("=" * 104)
    print(f"{'설정':42s}" + "".join(f"{w:>12s}" for w in W))
    print("-" * 104)
    detail = {}
    for label, sigs, fill in variants:
        line = f"{label:42s}"
        for wname, f in W.items():
            s = sigs[f(sigs.index)]
            l = {k: v.loc[s.index] for k, v in legs.items()}
            curve, trades, inj = run(s, l, params, risk, fill=fill)
            m = metrics(curve, trades, risk, sum(a for _, a in inj))
            line += f"{m['mult']:>10.2f}배"
            if wname == "최근5년":
                detail[label] = m
        print(line)
    print("-" * 104)
    print("(최근 5년 기준 상세)")
    for label, m in detail.items():
        print(f"  {label:42s} MDD {m['mdd']:>7.1f}%  Sharpe {m['sharpe']:>5.2f}  "
              f"거래 {m['n']:>3d}건  승률 {m['wr']:>5.1f}%")
    print("=" * 104)


if __name__ == "__main__":
    main()
