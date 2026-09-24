"""레짐 전환 시 **방향이 맞으면 포지션을 유지**하는 규칙 — 측정. (2026-09-24)

**사용자 제안.** "횡보에서 롱을 들고 있다가 상승으로 전환되면 그대로 보유하고,
숏을 들고 있다가 하락으로 전환되면 그대로 숏을 보유하면 어떤가?"

측정 결과를 읽기 전에 알아야 할 사실 두 가지:

1. **롱 쪽은 이미 그렇게 동작한다.** 횡보 롱 보유 중 상승 전환이면 `decide_action` 의
   목표 포지션이 그대로 long 이라 `cand == current` → **HOLD + RESTAMP**(매매 없이
   규칙만 상승 레짐의 추세 규칙으로 갈아끼움)가 된다. 왕복 비용도 내지 않는다.
   그래서 이 스크립트의 변형 C는 "그 기능을 **끄면** 어떻게 되는가"를 재서
   이미 있는 기능의 값을 보여준다.

2. **숏 쪽은 없다.** 하락 레짐의 규칙(`DOWN_RULE`)은 **롱 전용**이라(short_ok=False)
   숏을 들고 하락으로 전환되면 목표가 사라져 CLOSE 된다. 이것은 실수가 아니라
   세 번 검증된 기각 결정이다(docs/STRATEGY.md 3장: 하락장 SOXS 추세추종 0.17배,
   승률 16%). 변형 B가 그 기각을 **이 형태로 다시** 검증한다.

변형:
  A 현재            — 그대로
  B 숏 유지          — 숏 보유 + 하락 전환 시 청산하지 않고 **추세 숏**(트레일링 25%)으로 보유
  C 롱 유지 끄기      — 롱 보유 + 상승 전환 시 팔고 다음날 시가에 되산다(왕복 0.2% 지불)
  D B + 원래 롱 유지   — 제안 전체(= A + B). B와 같지만 표에서 대조용으로 남긴다

⚠️ 실험 스크립트다. src/ 는 건드리지 않았다.

사용법:
    ./.venv/bin/python scripts/backtest_regime_carry.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from data_fetcher import fetch_history  # noqa: E402
from regime import DOWN, UP  # noqa: E402
from risk_manager import RiskConfig, topup_needed  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)


def run(signals: pd.DataFrame, legs: dict[str, pd.DataFrame], params: Params,
        risk: RiskConfig, *, carry_short: bool = False, carry_long: bool = True):
    """backtest.run_backtest 와 같은 구조. 두 스위치만 다르다.

      carry_short — 숏 보유 + 하락 전환 시 추세 숏으로 유지할 것인가 (제안의 새 부분)
      carry_long  — 롱 보유 + 상승 전환 시 유지할 것인가 (현재 동작. False면 왕복비용 지불)
    """
    dates = signals.index
    equity = risk.total_capital_usd
    injections: list = []
    pos: dict | None = None
    trades: list = []
    curve = np.empty(len(dates))
    expo = risk.exposure
    events: Counter = Counter()

    def close_position(exit_price, exit_date, reason):
        nonlocal equity, pos
        gross = (exit_price - pos["entry_price"]) / pos["entry_price"]
        equity *= 1 + expo * gross - 2 * risk.fee_rate * expo
        trades.append(dict(regime=pos["regime"], side=pos["side"], entry_date=pos["entry_date"],
                           exit_date=exit_date, exit_reason=reason, ret_pct=gross * 100))
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
            price = float(leg.loc[today, "Close"])
            pos["peak"] = max(pos["peak"], price)
            holding = Holding(side=pos["side"], regime=pos["regime"], kind=pos["kind"],
                              rule=pos["rule"], bars_held=i - pos["entry_i"],
                              peak_ratio=price / pos["peak"],
                              unrealized=(price - pos["entry_price"]) / pos["entry_price"])

        hist = signals.iloc[pos["entry_i"]:i + 1] if pos is not None else None
        d = decide_action(row, holding, hist)
        action, restamp = d["action"], d["restamp"]

        # --- 변형 B: 숏 보유 + 하락 전환 → 청산하지 않고 추세 숏으로 유지 -------------
        if (carry_short and pos is not None and pos["side"] == "short"
                and d["exit_reason"] == "regime_change" and regime == DOWN
                and action in ("CLOSE", "SWITCH")):
            events["숏 유지(하락 전환)"] += 1
            if carry_short == "keep":
                # **규칙을 그대로 둔다.** 레짐 라벨만 갈아끼워 regime_change 가 다시
                # 걸리지 않게 하고, 진입일(entry_i)·고점은 건드리지 않는다 →
                # 원래의 횡보 평균회귀 규칙(z=0 청산, 꺾임 청산)이 그대로 이어진다.
                pos.update(regime=DOWN)
            else:
                pos.update(regime=DOWN, kind="trend", rule=None, entry_i=i + 1, peak=0.0)
            curve[i] = equity * (1 + expo * (float(legs["short"].loc[today, "Close"])
                                             - pos["entry_price"]) / pos["entry_price"])
            continue

        # --- 변형 C: 롱 유지를 끈다 → 팔고 다음날 시가에 되산다 ----------------------
        if (not carry_long and pos is not None and pos["side"] == "long"
                and d["exit_reason"] == "regime_change" and regime == UP
                and action == "HOLD" and restamp):
            events["롱 왕복(상승 전환)"] += 1
            close_position(float(legs["long"].loc[next_day, "Open"]), next_day, "regime_change")
            action, restamp = "OPEN", False
            d = dict(d, target="long")

        if action in ("CLOSE", "SWITCH"):
            events[f"청산 {d['exit_reason']}"] += 1
            close_position(float(legs[pos["side"]].loc[next_day, "Open"]), next_day, d["exit_reason"])
        elif action == "HOLD" and restamp:
            events[f"RESTAMP {pos['side']}→{regime}"] += 1
            pos.update(regime=regime, kind=kind_for(regime), rule=mr_rule_for(regime),
                       entry_i=i + 1, peak=0.0)

        if action in ("OPEN", "SWITCH"):
            need = topup_needed(equity, risk)
            if need > 0:
                injections.append((next_day, need))
                equity += need
            side = d["target"]
            pos = dict(kind=kind_for(regime), side=side, entry_date=next_day, entry_i=i + 1,
                       entry_price=float(legs[side].loc[next_day, "Open"]), regime=regime,
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
    return pd.Series(curve, index=dates), trades, injections, events


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
    params, risk = Params(), RiskConfig(total_capital_usd=5000.0)
    sig_df = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = sig_df.index.intersection(legs["long"].index).intersection(legs["short"].index)
    sig_df, legs = sig_df.loc[common], {k: v.loc[common] for k, v in legs.items()}
    signals = build_signals(sig_df, params).dropna(subset=["z", "regime"])

    end = signals.index[-1]
    W = {
        "학습2.5": lambda ix: (ix >= end - pd.Timedelta(days=1826)) & (ix < end - pd.Timedelta(days=913)),
        "검증2.5": lambda ix: ix >= end - pd.Timedelta(days=913),
        "최근5년": lambda ix: ix >= end - pd.Timedelta(days=1826),
        "최근3년": lambda ix: ix >= end - pd.Timedelta(days=1096),
        "전체": lambda ix: ix >= ix[0],
    }
    variants = [
        ("A 현재 (롱 유지 O / 숏 유지 X)", dict(carry_short=False, carry_long=True)),
        ("B1 숏 유지 = 추세 숏(트레일 25%)", dict(carry_short="trend", carry_long=True)),
        ("B2 숏 유지 = 횡보 규칙 그대로", dict(carry_short="keep", carry_long=True)),
        ("C 롱 유지를 끄면 (참고)", dict(carry_short=False, carry_long=False)),
    ]

    print("=" * 100)
    print("레짐 전환 시 같은 방향 포지션 유지 — 배수(투입대비)")
    print("=" * 100)
    print(f"{'설정':34s}" + "".join(f"{w:>13s}" for w in W))
    print("-" * 100)
    detail, evs = {}, {}
    for label, kw in variants:
        line = f"{label:34s}"
        for wname, f in W.items():
            s = signals[f(signals.index)]
            l = {k: v.loc[s.index] for k, v in legs.items()}
            curve, trades, inj, ev = run(s, l, params, risk, **kw)
            m = metrics(curve, trades, risk, sum(a for _, a in inj))
            line += f"{m['mult']:>11.2f}배"
            if wname == "최근5년":
                detail[label], evs[label] = m, ev
        print(line)
    print("-" * 100)
    print("(최근 5년 상세)")
    for label, m in detail.items():
        print(f"  {label:34s} MDD {m['mdd']:>7.1f}%  Sharpe {m['sharpe']:>5.2f}  "
              f"거래 {m['n']:>3d}건  승률 {m['wr']:>5.1f}%")
    print("-" * 100)
    print("(최근 5년 — 전환 이벤트 발동 횟수. 몇 건인지가 신뢰도의 핵심이다)")
    for label, ev in evs.items():
        keys = [k for k in ev if k.startswith(("숏 유지", "롱 왕복", "RESTAMP"))]
        got = ", ".join(f"{k} {ev[k]}건" for k in sorted(keys)) or "없음"
        print(f"  {label:34s} {got}")
    print("=" * 100)


if __name__ == "__main__":
    main()
