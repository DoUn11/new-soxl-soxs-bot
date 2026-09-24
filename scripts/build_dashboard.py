"""대시보드용 데이터 수집 — results/dashboard.json 을 만든다.

대시보드는 **읽기 전용**이다. 이 스크립트도 조회만 하고 주문을 내지 않는다.

모으는 것:
  1. 상태   — 현재 레짐, SOXX 가격·z, 레짐 지속일, 오늘의 지시, 장 운영 시간, 최근 레짐 추이
  2. 주문   — 토스 Open API 의 실제 주문 내역 (SOXL/SOXS 만) + journal 기록
  3. 성과   — 실거래 실현손익, 그리고 참고용 백테스트 기대치

토스 자격증명이 없거나 조회가 실패하면 그 부분만 비우고 나머지는 채운다
(대시보드가 자격증명 없이도 열리게 하려는 것).

실행:
    ./.venv/bin/python scripts/build_dashboard.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import journal  # noqa: E402
import toss_api  # noqa: E402
from backtest import run_backtest  # noqa: E402
from data_fetcher import fetch_history  # noqa: E402
from risk_manager import RiskConfig, position_size_usd  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for, SIDEWAYS_RULE, DOWN_RULE)

OUT = ROOT / "results" / "dashboard.json"
TEMPLATE = ROOT / "dashboard" / "template.html"
PAGE = ROOT / "results" / "dashboard.html"
BOT_SYMBOLS = ("SOXL", "SOXS")


def after_tax_once(mult: float, capital: float = 5000.0) -> float:
    """**1회 실현** 기준 세후. 매수보유 벤치마크에 쓴다(끝에 한 번 팔므로).

    해외주식 양도소득세 22%, 기본공제 250만원(≈$1,800) 반영.
    """
    gain = capital * mult - capital
    if gain <= 0:
        return mult
    return (capital + gain - max(0.0, gain - 1800) * 0.22) / capital


def after_tax_annual(curve: pd.Series, injections=(), capital: float = 5000.0) -> float:
    """**매년 실현** 기준 세후 배수. 전략에 쓴다.

    이 전략은 연 20회 안팎 회전하므로 이익이 매년 실현된다. 끝에 한 번 파는 매수보유와
    세금 구조가 다르고, 그 차이가 성과 비교를 뒤집을 만큼 크다(docs/STRATEGY.md).

    **추가입금은 이익이 아니다.** 그 해 자산 증가분에서 입금액을 빼고 과세한다. 빼지
    않으면 새로 넣은 돈에 세금을 매겨 세후가 세전보다 커진다. 돌려주는 값은
    `세후 최종 자산 ÷ 총 투입(최초 원금 + 누적 입금)` 이다.
    """
    inj = pd.Series(dtype=float)
    if len(injections):
        inj = pd.Series([a for _, a in injections],
                        index=pd.DatetimeIndex([d for d, _ in injections]))
    y = curve.resample("YE").last()
    y = pd.concat([pd.Series([capital], index=[curve.index[0]]), y])
    eq = capital
    total_in = capital
    for (d0, a), (d1, b) in zip(zip(y.index[:-1], y.values[:-1]),
                                zip(y.index[1:], y.values[1:])):
        added = float(inj[(inj.index > d0) & (inj.index <= d1)].sum()) if len(inj) else 0.0
        total_in += added
        gain = b / a * eq - eq - added          # 입금분은 이익에서 제외
        eq += added + (gain - max(0.0, gain - 1800) * 0.22 if gain > 0 else gain)
    return eq / total_in


def collect_strategy() -> dict:
    params = Params()
    risk = RiskConfig()
    sig_df = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = sig_df.index.intersection(legs["long"].index).intersection(legs["short"].index)
    sig_df = sig_df.loc[common]
    legs = {k: v.loc[common] for k, v in legs.items()}
    signals = build_signals(sig_df, params).dropna(subset=["z", "regime"])
    legs = {k: v.loc[signals.index] for k, v in legs.items()}

    today = signals.index[-1]
    row = signals.loc[today]
    streak = 1
    for k in range(len(signals) - 2, -1, -1):
        if signals["regime"].iloc[k] == row["regime"]:
            streak += 1
        else:
            break

    pos = journal.current_position()
    equity = journal.current_equity(risk.total_capital_usd)
    holding = hist = None
    pos_out = None
    if pos:
        side = "long" if pos["ticker"] == params.tickers["long"] else "short"
        price = float(legs[side]["Close"].loc[today])
        entry = float(pos["entry_price"])
        since = pd.Timestamp(pos["opened"][:10])
        held = legs[side]["Close"].loc[since:]
        peak = float(held.max()) if len(held) else price
        days = max(len(held) - 1, 0)
        hist = signals.loc[since:]
        holding = Holding(side=side, regime=pos["regime"], kind=kind_for(pos["regime"]),
                          rule=mr_rule_for(pos["regime"]), bars_held=days,
                          peak_ratio=price / peak if peak else 1.0,
                          unrealized=(price - entry) / entry if entry else 0.0)
        pos_out = dict(ticker=pos["ticker"], shares=float(pos["shares"]), entry=entry,
                       price=price, opened=pos["opened"][:10], days=days,
                       unrealized_pct=(price / entry - 1) * 100 if entry else 0.0,
                       unrealized_usd=(price - entry) * float(pos["shares"]),
                       filled=bool(pos.get("filled")), regime=pos["regime"])

    d = decide_action(row, holding, hist)
    target = d["target"]
    plan_amount = position_size_usd(equity, risk) if d["action"] in ("OPEN", "SWITCH") else None

    # 최근 90거래일 레짐·z 추이 (차트용)
    tail = signals.tail(90)
    trend = [dict(date=str(i.date()), close=round(float(r["close"]), 2),
                  z=round(float(r["z"]), 2), regime=r["regime"])
             for i, r in tail.iterrows()]

    # 표시 전용 장세 라벨 — 매매에는 들어가지 않는다 (regime.describe 독스트링 참고)
    import regime as regime_mod
    _view = regime_mod.describe(signals["close"])
    view_label = _view.loc[today] if pd.notna(_view.loc[today]) else None
    view_chg = round(float(signals["close"].pct_change(20).loc[today]) * 100, 1)

    # 레짐 분포 (전체 기간)
    dist = {k: int(v) for k, v in signals["regime"].value_counts().items()}

    return dict(
        params=dict(bb_period=params.bb_period, regime_ma=params.regime_ma,
                    regime_slope_days=params.regime_slope_days,
                    regime_price_days=params.regime_price_days, trend_ma=params.trend_ma,
                    sideways_entry=SIDEWAYS_RULE.entry_z, sideways_exit=SIDEWAYS_RULE.exit_z,
                    down_entry=DOWN_RULE.entry_z,
                    exposure=risk.exposure, min_capital=risk.min_capital_usd),
        signal=dict(date=str(today.date()), close=round(float(row["close"]), 2),
                    z=round(float(row["z"]), 2), regime=row["regime"], streak=streak,
                    view=view_label, view_chg=view_chg,
                    lt_trend=(row["lt_trend"] if pd.notna(row["lt_trend"]) else None),
                    lt_chg=(round(float(row["lt_chg"]) * 100, 1)
                            if pd.notna(row["lt_chg"]) else None),
                    action=d["action"], reason=d["reason"],
                    ticker=(params.tickers[target] if target else None),
                    amount=plan_amount, equity=equity),
        position=pos_out, trend=trend, regime_dist=dist,
        signals=signals, legs=legs, params_obj=params, risk=risk)


def collect_backtest(signals, legs, params, risk) -> list[dict]:
    last = signals.index[-1]
    out = []
    for name, kw in [("최근 3년", dict(years=3)), ("AI 국면 2023~", dict(since="2023-01-01")),
                     ("최근 5년", dict(years=5)), ("전체", dict(years=0))]:
        s = signals
        if kw.get("since"):
            s = s[s.index >= pd.Timestamp(kw["since"])]
        elif kw.get("years"):
            s = s[s.index >= last - pd.DateOffset(years=kw["years"])]
        l = {k: v.loc[s.index] for k, v in legs.items()}
        curve, trades, injections = run_backtest(s, l, params, risk)
        injected = sum(a for _, a in injections)
        invested = risk.total_capital_usd + injected
        mult = curve.iloc[-1] / invested
        df = pd.DataFrame([t.__dict__ for t in trades])
        dl = curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
        bhl = l["long"]["Close"].iloc[-1] / l["long"]["Open"].iloc[0]
        out.append(dict(name=name, years=round((curve.index[-1] - curve.index[0]).days / 365.25, 1),
                        mult=round(float(mult), 2),
                        mdd=round(float((curve / curve.cummax() - 1).min() * 100), 1),
                        sharpe=round(float(dl.mean() / dl.std() * np.sqrt(252)), 2),
                        trades=len(df),
                        winrate=round(float((df.ret_pct > 0).mean() * 100), 1) if len(df) else 0.0,
                        after_tax=round(float(after_tax_annual(curve, injections)), 2),
                        n_injections=len(injections),
                        injected=round(float(injected), 2),
                        soxl=round(float(bhl), 2),
                        soxl_tax=round(after_tax_once(float(bhl)), 2)))
    return out


def collect_broker() -> dict:
    """증권사 조회 — **봇이 낸 주문만**. 실패하면 error만 채워 돌려준다.

    ⚠️ 토스 주문 목록 API는 `clientOrderId` 를 돌려주지 않는다. 그래서 증권사 내역만으로는
    봇 주문과 사람이 직접 낸 주문을 구분할 수 없다(이 계좌에는 수동 주문이 100건 가까이
    있다). 따라서 **저널(`journal.bot_orders()`)을 기준으로 삼고**, 저장된 orderId로
    증권사 상세를 덧붙인다. 저널에 없는 주문은 봇이 낸 것이 아니므로 보여주지 않는다.
    """
    creds = toss_api.Credentials.from_env()
    bot = journal.bot_orders()
    if creds is None:
        return dict(ok=False, error="자격증명 미설정 (TOSS_API_CLIENT_ID / SECRET / ACCOUNT_SEQ)",
                    orders=[dict(r, broker=None) for r in bot])
    cl = toss_api.TossClient(creds)
    out: dict = dict(ok=True)
    try:
        out["buying_power_usd"] = round(cl.buying_power_usd(), 2)
        out["holdings"] = [
            dict(symbol=sym, quantity=cl.held_quantity(sym), sellable=cl.sellable_quantity(sym))
            for sym in BOT_SYMBOLS]
        out["prices"] = {sym: round(cl.last_price(sym), 4) for sym in ("SOXX", "SOXL", "SOXS")}
        cal = cl._request("GET", "/api/v1/market-calendar/US") or {}
        today = cal.get("today") or {}
        out["sessions"] = {k: today.get(k) for k in ("preMarket", "regularMarket", "afterMarket")}
    except toss_api.TossError as e:
        out["ok"] = False
        out["error"] = f"{e}"

    orders = []
    for r in bot:
        item = dict(
            timestamp=r.get("timestamp"), action=r.get("action"), kind=r.get("kind"),
            ticker=r.get("ticker"), regime=r.get("regime"),
            z=float(r.get("z") or 0), signal_price=float(r.get("signal_price") or 0),
            fill_price=float(r.get("fill_price") or 0), shares=float(r.get("shares") or 0),
            note=r.get("note") or "", order_id=r.get("order_id") or "", broker=None)
        if item["order_id"] and out.get("ok"):
            try:
                o = cl.get_order(item["order_id"])
                ex = o.get("execution") or {}
                item["broker"] = dict(
                    status=o.get("status"), side=o.get("side"), orderType=o.get("orderType"),
                    quantity=o.get("quantity"), price=o.get("price"),
                    orderAmount=o.get("orderAmount"), orderedAt=o.get("orderedAt"),
                    filledQuantity=ex.get("filledQuantity"),
                    averageFilledPrice=ex.get("averageFilledPrice"),
                    filledAmount=ex.get("filledAmount"),
                    commission=ex.get("commission"), tax=ex.get("tax"),
                    filledAt=ex.get("filledAt"))
            except toss_api.TossError as e:
                item["broker_error"] = f"{e}"
        # 슬리피지 (신호가 대비 체결가)
        if item["signal_price"] and item["fill_price"]:
            item["slippage_pct"] = round((item["fill_price"] / item["signal_price"] - 1) * 100, 3)
        orders.append(item)
    orders.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    out["orders"] = orders
    return out


def collect_live_performance() -> dict:
    """journal 기준 실거래 성과. 아직 없으면 빈 상태로 돌려준다."""
    rows = journal.read_all()
    entries = [r for r in rows if r.get("action") in ("OPEN", "CLOSE", "SWITCH")]
    closed, realized = [], 0.0
    open_row = None
    for r in rows:
        a = r.get("action")
        if a == "OPEN":
            open_row = r
        elif a == "CLOSE" and open_row is not None:
            ep = float(open_row.get("fill_price") or open_row.get("signal_price") or 0)
            xp = float(r.get("fill_price") or r.get("signal_price") or 0)
            sh = float(open_row.get("shares") or 0)
            if ep > 0:
                pnl = (xp - ep) * sh
                realized += pnl
                closed.append(dict(ticker=open_row.get("ticker"), opened=open_row.get("timestamp")[:10],
                                   closed=r.get("timestamp")[:10], entry=ep, exit=xp, shares=sh,
                                   pnl=round(pnl, 2), ret_pct=round((xp / ep - 1) * 100, 2)))
            open_row = None
    wins = [c for c in closed if c["pnl"] > 0]
    return dict(journal_rows=len(rows), signals=len(entries), closed=closed,
                realized_pnl=round(realized, 2), n_closed=len(closed),
                winrate=round(len(wins) / len(closed) * 100, 1) if closed else None,
                skipped=len([r for r in rows if r.get("action") == "SKIPPED"]))


def main() -> None:
    st = collect_strategy()
    signals, legs = st.pop("signals"), st.pop("legs")
    params, risk = st.pop("params_obj"), st.pop("risk")
    data = dict(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        generated_et=pd.Timestamp.now(tz="America/New_York").strftime("%Y-%m-%d %H:%M ET"),
        **st,
        backtest=collect_backtest(signals, legs, params, risk),
        broker=collect_broker(),
        live=collect_live_performance(),
    )
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"저장: {OUT}")

    # 템플릿에 데이터를 주입해 완성 페이지를 만든다.
    if TEMPLATE.exists():
        blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        # 인라인 <script> 안에서 </script> 나 <!-- 가 파서를 끊지 않게 막는다.
        blob = blob.replace("<", "\\u003c").replace(">", "\\u003e")
        PAGE.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", blob),
                        encoding="utf-8")
        print(f"저장: {PAGE}  ({PAGE.stat().st_size/1024:.0f} KB)")
    else:
        print(f"경고: 템플릿이 없습니다 ({TEMPLATE})")
    print(f"  레짐 {data['signal']['regime']}  z={data['signal']['z']:+.2f}  "
          f"지시 {data['signal']['action']}")
    print(f"  주문내역 {len(data['broker'].get('orders') or [])}건  "
          f"백테스트 {len(data['backtest'])}구간  "
          f"브로커 조회 {'성공' if data['broker'].get('ok') else data['broker'].get('error')}")


if __name__ == "__main__":
    main()
