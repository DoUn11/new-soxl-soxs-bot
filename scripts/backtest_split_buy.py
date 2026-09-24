"""분할매수(split-buy) 실험 — 진입을 한 번에 전량이 아니라 여러 번에 나눠 사면 나아지는가.

**실험 스크립트다.** src/backtest.py, executor.py, journal.py 는 건드리지 않았다.
방향·청산 판단은 strategy.decide_action 을 그대로 쓴다 — 이 스크립트는 그 판단이 내려진
뒤 "이번 진입을 몇 번에 나눠, 무엇을 트리거로 채우는가"라는 **사이징 레이어**만 얹는다.
그래서 진입/청산 로직을 복제하지 않는다(CLAUDE.md 원칙 — 판단은 decide_action 한 곳에만).

## 3가지 분할 방식

  z_pyramid    첫 tranche는 기존과 같은 entry_z에서 채운다. 이후 |z|가 z_step(0.5)씩 더
               벌어질 때마다 다음 tranche를 채운다(평균회귀 레짐에서만 의미가 있다 — z를
               쓰지 않는 상승 추세 진입은 이 방식에서 통째로 1회 채움으로 처리한다).
  time_split   tranche i는 최초 체결일로부터 거래일 i일 뒤 시가에 조건 없이 채운다.
               (그 사이 청산 조건이 먼저 뜨면 남은 tranche는 채우지 않는다.)
  adverse_move 평균 단가 대비 보유 종목(SOXL/SOXS) 가격이 adverse_step(4%)씩 더 밀릴
               때마다 다음 tranche를 채운다 — 흔히 말하는 "물타기".

tranche 비중은 세 방식 모두 (0.4, 0.3, 0.3)으로 통일해 비교 가능하게 했다.

## 3가지 적용 범위

  sideways_only  횡보 레짐 진입에만 분할 적용 (거래 표본이 가장 많다)
  mr_all         횡보+하락(평균회귀 레짐 전체)에 적용
  all            상승 추세 진입까지 포함 — 상승은 z가 없으므로 time_split/adverse_move만
                 실질적으로 작동한다(z_pyramid는 상승에서 1회 채움과 동일).

비용 모델: 채운 비중(filled_weight)에 비례해 왕복 수수료를 낸다 — tranche를 다 못 채우고
청산하면 그만큼 수수료도 덜 낸다(실제로 그만큼만 주문을 냈을 것이므로).

평가는 CLAUDE.md 표준(2026-09-23)을 따른다 — **최근 5년을 앞 2.5년(학습)/뒤 2.5년(검증)
으로 나눠 양쪽을 본다.** 참고용으로 최근 3년(CLI 기본)·AI 국면(2023~)·전체 기간도 함께
싣는다.

사용법:
    ./.venv/bin/python scripts/backtest_split_buy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from data_fetcher import fetch_history  # noqa: E402
from regime import DOWN, SIDEWAYS, UP  # noqa: E402
from risk_manager import RiskConfig, topup_needed  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)

METHODS = ("none", "z_pyramid", "time_split", "adverse_move")
SCOPES = ("sideways_only", "mr_all", "all")
WEIGHTS = (0.4, 0.3, 0.3)
Z_STEP = 0.5
ADVERSE_STEP = 0.04


def _scope_regimes(scope: str) -> set[str]:
    return {"sideways_only": {SIDEWAYS}, "mr_all": {SIDEWAYS, DOWN}, "all": {SIDEWAYS, DOWN, UP}}[scope]


def _init_position(regime, side, kind, rule, fill_price, fill_i, method, scope) -> dict:
    """새 포지션(진입 1번째 tranche)을 만든다. tranches: [(price, weight)]."""
    use_split = method != "none" and regime in _scope_regimes(scope)
    if not use_split or (method == "z_pyramid" and kind == "trend"):
        weights = (1.0,)
    else:
        weights = WEIGHTS
    return dict(kind=kind, side=side, regime=regime, rule=rule,
                entry_i=fill_i, peak=0.0,
                tranches=[(fill_price, weights[0])], weights=weights,
                filled_weight=weights[0], next_idx=1)


def _avg_price(pos: dict) -> float:
    return sum(p * w for p, w in pos["tranches"]) / pos["filled_weight"]


def _tranche_due(pos: dict, method: str, row: pd.Series, leg_close: float, i: int) -> bool:
    """오늘(종가 기준 row/leg_close, iloc i) 다음 tranche 조건이 찼는가."""
    if pos["next_idx"] >= len(pos["weights"]):
        return False
    if method == "z_pyramid":
        rule = pos["rule"]
        if rule is None:
            return False
        level = rule.entry_z + Z_STEP * pos["next_idx"]
        z = row["z"]
        return (z <= -level) if pos["side"] == "long" else (z >= level)
    if method == "time_split":
        return i == pos["entry_i"] + pos["next_idx"] - 1
    if method == "adverse_move":
        avg = _avg_price(pos)
        move = (leg_close - avg) / avg
        threshold = ADVERSE_STEP * pos["next_idx"]
        return (move <= -threshold) if pos["side"] == "long" else (move >= threshold)
    return False


def simulate(signals: pd.DataFrame, legs: dict[str, pd.DataFrame], params: Params,
            risk: RiskConfig, method: str, scope: str) -> tuple[pd.Series, list[dict], list[tuple]]:
    dates = signals.index
    equity = risk.total_capital_usd
    injections: list[tuple] = []
    pos: dict | None = None
    trades: list[dict] = []
    curve = np.empty(len(dates))
    expo = risk.exposure

    def close_position(exit_price, exit_date, reason):
        nonlocal equity, pos
        avg = _avg_price(pos)
        fw = pos["filled_weight"]
        gross = (exit_price - avg) / avg
        equity *= 1 + expo * fw * gross - 2 * risk.fee_rate * expo * fw
        trades.append(dict(regime=pos["regime"], side=pos["side"],
                           ticker=params.tickers[pos["side"]],
                           entry_date=dates[pos["entry_i"]], exit_date=exit_date,
                           avg_entry=avg, exit_price=exit_price, exit_reason=reason,
                           n_tranches=len(pos["tranches"]), filled_weight=fw,
                           ret_pct=gross * 100))
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
            avg = _avg_price(pos)
            holding = Holding(side=pos["side"], regime=pos["regime"], kind=pos["kind"],
                              rule=pos["rule"], bars_held=i - pos["entry_i"],
                              peak_ratio=price / pos["peak"], unrealized=(price - avg) / avg)

        hist = signals.iloc[pos["entry_i"]:i + 1] if pos is not None else None
        d = decide_action(row, holding, hist)

        if d["action"] in ("CLOSE", "SWITCH"):
            close_position(legs[pos["side"]].loc[next_day, "Open"], next_day, d["exit_reason"])
        elif d["action"] == "HOLD" and d["restamp"]:
            pos.update(regime=regime, kind=kind_for(regime), rule=mr_rule_for(regime),
                       entry_i=i + 1, peak=0.0)

        if d["action"] in ("OPEN", "SWITCH"):
            need = topup_needed(equity, risk)
            if need > 0:
                injections.append((next_day, need))
                equity += need
            side = d["target"]
            pos = _init_position(regime, side, kind_for(regime), mr_rule_for(regime),
                                 legs[side].loc[next_day, "Open"], i + 1, method, scope)
        elif pos is not None and pos["side"] == (holding.side if holding else None):
            # 보유 중 — 다음 tranche가 찼는지 오늘 종가 기준으로 확인, 내일 시가에 채운다.
            leg_close = legs[pos["side"]].loc[today, "Close"]
            if _tranche_due(pos, method, row, leg_close, i):
                fill_price = legs[pos["side"]].loc[next_day, "Open"]
                w = pos["weights"][pos["next_idx"]]
                pos["tranches"].append((fill_price, w))
                pos["filled_weight"] += w
                pos["next_idx"] += 1

        if pos is None:
            curve[i] = equity
        else:
            leg = legs[pos["side"]]
            avg = _avg_price(pos)
            mtm = (leg.loc[today, "Close"] - avg) / avg
            curve[i] = equity * (1 + expo * pos["filled_weight"] * mtm)

    if pos is not None:
        last = dates[-1]
        close_position(legs[pos["side"]].loc[last, "Close"], last, "open_at_end")
        curve[-1] = equity
    else:
        curve[-1] = curve[-2] if len(curve) > 1 else equity
    return pd.Series(curve, index=dates), trades, injections


def metrics(curve: pd.Series, trades: list[dict], risk: RiskConfig, injected: float) -> dict:
    invested = risk.total_capital_usd + injected
    mult = curve.iloc[-1] / invested
    mdd = (curve / curve.cummax() - 1).min() * 100
    daily = curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else 0.0
    n = len(trades)
    winrate = (sum(1 for t in trades if t["ret_pct"] > 0) / n * 100) if n else 0.0
    avg_tranches = (sum(t["n_tranches"] for t in trades) / n) if n else 0.0
    return dict(mult=mult, mdd=mdd, sharpe=sharpe, n=n, winrate=winrate, avg_tranches=avg_tranches)


def load_data(params: Params):
    signal_df = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = signal_df.index.intersection(legs["long"].index).intersection(legs["short"].index)
    signal_df, legs = signal_df.loc[common], {k: v.loc[common] for k, v in legs.items()}
    signals = build_signals(signal_df, params).dropna(subset=["z", "regime"])
    legs = {k: v.loc[signals.index] for k, v in legs.items()}
    return signals, legs


def window(signals, legs, since=None, years=None):
    s = signals
    if since:
        s = s[s.index >= pd.Timestamp(since)]
    elif years:
        s = s[s.index >= s.index[-1] - pd.DateOffset(years=years)]
    l = {k: v.loc[s.index] for k, v in legs.items()}
    return s, l


def run_one(signals, legs, params, risk, method, scope) -> dict:
    curve, trades, injections = simulate(signals, legs, params, risk, method, scope)
    return metrics(curve, trades, risk, sum(a for _, a in injections))


def main() -> None:
    params = Params()
    risk = RiskConfig(total_capital_usd=5000.0)
    signals, legs = load_data(params)

    full_start, full_end = signals.index[0], signals.index[-1]
    mid5 = signals.index[-1] - pd.Timedelta(days=round(2.5 * 365.25))
    start5 = signals.index[-1] - pd.Timedelta(days=round(5 * 365.25))

    windows = {
        "학습(최근5년 앞 2.5년)": (signals[(signals.index >= start5) & (signals.index < mid5)],),
        "검증(최근5년 뒤 2.5년)": (signals[signals.index >= mid5],),
        "최근5년 전체": (signals[signals.index >= start5],),
        "최근3년(CLI기본)": (signals[signals.index >= signals.index[-1] - pd.DateOffset(years=3)],),
        "AI국면(2023~)": (signals[signals.index >= pd.Timestamp("2023-01-01")],),
        f"전체 {(full_end-full_start).days/365.25:.1f}년": (signals,),
    }
    win_legs = {name: {k: v.loc[s.index] for k, v in legs.items()} for name, (s,) in windows.items()}

    rows = []
    for scope in SCOPES:
        for method in METHODS:
            if method == "none" and scope != SCOPES[0]:
                continue  # baseline(분할 없음)은 scope 무관하게 한 번만
            label = "분할없음(기존)" if method == "none" else f"{method} / {scope}"
            r = {"조합": label}
            for wname, (s,) in windows.items():
                m = run_one(s, win_legs[wname], params, risk, method, scope)
                r[wname] = m
            rows.append(r)

    win_names = list(windows.keys())
    print("=" * 150)
    print("분할매수 실험 — 배수(투입대비). 학습/검증 두 창이 채택 여부 판단 기준, 나머지는 참고용")
    print("=" * 150)
    col_w = 18
    header = f"{'조합':28s}" + "".join(f"{w:>{col_w}s}" for w in win_names)
    print(header)
    print("-" * len(header))
    for r in rows:
        line = f"{r['조합']:28s}"
        for wname in win_names:
            m = r[wname]
            line += f"{m['mult']:>{col_w-1}.2f}배"
        print(line)
    print("-" * len(header))
    print("(MDD / Sharpe / 거래수 / 승률 / 평균tranche수 — 최근5년 전체 기준)")
    print("-" * len(header))
    for r in rows:
        m = r["최근5년 전체"]
        print(f"{r['조합']:28s} MDD {m['mdd']:>7.1f}%  Sharpe {m['sharpe']:>5.2f}  "
              f"거래 {m['n']:>4d}건  승률 {m['winrate']:>5.1f}%  평균tranche {m['avg_tranches']:.2f}")
    print("=" * 120)


if __name__ == "__main__":
    main()
