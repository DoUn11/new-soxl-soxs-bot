"""알리미 겸 실행기 — 전략 신호로 매수·매도를 실행하고 결과를 보고한다.

역할이 세 단계로 나뉜다.
  - 플래그 없음  : 오늘의 지시만 보여준다(읽기 전용). 점검·확인용.
  - `--execute`  : 주문 계획을 세우고 **안전장치 전부를 실제로 검사**한 뒤 드라이런한다.
  - `--live`     : 실제 주문을 낸다. 체결까지 확인해 journal에 남기고 보고한다.

⚠️ `--live` 가 없으면 주문이 나가지 않는다. 기본값을 실거래로 두지 않는 이유는, 실수로
   한 번 실행하는 것이 되돌릴 수 없는 주문이 되기 때문이다. 주문 안전장치는 executor.py,
   API 호출은 toss_api.py 가 담당한다.

전략 신호는 **일봉 종가**로 확정된다. 따라서 결정 시점은 하루에 한 번, 미국 장 마감(16:00 ET)
직후뿐이다. 프리장·본장·애프터장 중 아무 때나 신호가 뜨는 구조가 아니다.

**체결 시점 권고** (docs/STRATEGY.md 6장 실측):
  백테스트가 검증한 체결 시점은 **다음 거래일 정규장 시가**다. 즉시 행동을 원하시면
  마감 직후 애프터장이 가장 가깝지만, 그 구간은 스프레드가 넓어 백테스트 가정(편도 0.1%)을
  넘길 가능성이 크다. 실측한 체결 시점별 차이는 40%에 달했으므로, 당분간은
  **다음날 정규장 개장 직후(09:30~10:00 ET)** 체결을 권한다. 실제 체결가를 journal에 남겨
  가정이 맞는지 몇 달 뒤 대조할 것.

사용법:
    ./.venv/bin/python src/alerter.py                   # 오늘의 지시만 확인 (읽기 전용)
    ./.venv/bin/python src/alerter.py --execute         # 안전장치 검사 + 드라이런
    ./.venv/bin/python src/alerter.py --live --notify   # 실제 매매 + 텔레그램 보고
    ./.venv/bin/python src/alerter.py --slippage        # 신호가 대비 체결가 차이 리포트
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import executor  # noqa: E402
import journal  # noqa: E402
import regime as regime_mod  # noqa: E402
import notifier  # noqa: E402
import toss_api  # noqa: E402
from data_fetcher import fetch_history  # noqa: E402
from risk_manager import RiskConfig, position_size_usd  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)

BAR = "=" * 70


def extended_hours_price(ticker: str) -> tuple[float, str] | None:
    """프리/애프터장 최신 체결가 (참고용). 정규장 종가와 얼마나 벌어졌는지 보기 위함."""
    try:
        d = yf.Ticker(ticker).history(period="5d", interval="30m", prepost=True)
        if d.empty:
            return None
        d.index = d.index.tz_convert("America/New_York")
        last = d.iloc[-1]
        return float(last["Close"]), d.index[-1].strftime("%m-%d %H:%M ET")
    except Exception:
        return None


def decide(signals: pd.DataFrame, legs: dict, params: Params, pos: dict | None,
           quotes: dict[str, float] | None = None) -> dict:
    """마지막 거래일 종가 기준으로 오늘의 지시를 만든다.

    `quotes` 는 토스 실호가(있으면). **표시용 현재가에만 쓰고 판단에는 쓰지 않는다.**
    """
    today = signals.index[-1]
    row = signals.loc[today]
    regime = row["regime"]

    # 레짐 지속일
    streak = 1
    for k in range(len(signals) - 2, -1, -1):
        if signals["regime"].iloc[k] == regime:
            streak += 1
        else:
            break

    out = dict(date=today, regime=regime, z=float(row["z"]), close=float(row["close"]),
               streak=streak, action="NONE", ticker=None, reason="", detail="")

    # 표시 전용 장세 라벨. 매매 판단에는 절대 들어가지 않는다 — decide_action 은 위의
    # row["regime"] 만 본다. regime.describe 독스트링 참고.
    view = regime_mod.describe(signals["close"])
    if pd.notna(view.loc[today]):
        out["view"] = view.loc[today]
        out["view_chg"] = float(signals["close"].pct_change(20).loc[today])

    holding = hist = None
    if pos:
        side = "long" if pos["ticker"] == params.tickers["long"] else "short"
        price = float(legs[side]["Close"].loc[today])
        entry = pos["entry_price"]
        unrealized = (price - entry) / entry if entry > 0 else 0.0
        # 보유 중 최고가·보유일은 진입일(또는 마지막 RESTAMP일) 이후로 센다.
        # 진입일이 마지막 거래일 이후(장 마감 후 기록)일 수 있으므로 하한을 둔다.
        since = pd.Timestamp(pos["opened"][:10])
        # 체결은 장 마감 후에 기록되므로 진입일이 **마지막 봉보다 뒤**일 수 있다.
        # 그대로 두면 signals.loc[since:] 가 비어 σ 손절(stop_hit)과 반등 후 꺾임
        # (short_trend_broken)이 **조용히 꺼진다** — 백테스트에서는 entry_i 로 자르므로
        # 멀쩡해서 일치성 테스트로도 잡히지 않는다. 진입 근거가 된 봉은 마지막 봉이므로
        # 거기로 내린다.
        since = min(since, today)
        held = legs[side]["Close"].loc[since:]
        peak = float(held.max()) if len(held) else price
        days = max(len(held) - 1, 0)
        hist = signals.loc[since:]
        holding = Holding(side=side, regime=pos["regime"], kind=kind_for(pos["regime"]),
                          rule=mr_rule_for(pos["regime"]), bars_held=days,
                          peak_ratio=price / peak if peak else 1.0, unrealized=unrealized)
        out.update(unrealized=unrealized * 100, entry=entry, price=price,
                   peak=peak, days=days, ticker=pos["ticker"], price_src="yahoo")
        # **표시용만** 실호가로 덮어쓴다. 위의 holding/peak_ratio 는 Yahoo 시계열
        # 그대로 둔다 — 현재가만 다른 소스로 바꾸면 고점과 자가 달라진다.
        lp = (quotes or {}).get(pos["ticker"].upper())
        if lp:
            out.update(price=lp, unrealized=(lp - entry) / entry * 100 if entry > 0 else 0.0,
                       price_src="toss")

    # 판단은 strategy.decide_action 하나만 쓴다 — 백테스트와 같은 경로다.
    d = decide_action(row, holding, hist)
    out.update(action=d["action"], reason=d["reason"], target=d["target"],
               exit_reason=d["exit_reason"], restamp=d["restamp"])

    # 거래 종목의 기준가(신호일 종가). SOXX 종가와 혼동하면 원가·슬리피지가 어긋난다.
    ref_side = d["target"] or (holding.side if holding else None)
    if ref_side:
        tk = params.tickers[ref_side]
        # 저널의 signal_price 로 남아 슬리피지 계산의 기준이 된다. Yahoo 소급조정가로
        # 적으면 실제 체결가와 2% 어긋나 슬리피지가 허수가 되므로 실호가를 우선한다.
        out["ref_price"] = float((quotes or {}).get(tk.upper())
                                 or legs[ref_side]["Close"].loc[today])

    held_ticker = (pos or {}).get("ticker", "")
    if d["action"] == "HOLD":
        detail = (f"{held_ticker} 계속 보유 (규칙 갱신)" if d["restamp"]
                  else f"{held_ticker} 계속 보유")
        out.update(ticker=held_ticker, detail=detail)
    elif d["action"] == "CLOSE":
        out.update(ticker=held_ticker, detail=f"{held_ticker} 전량 매도")
    elif d["action"] == "OPEN":
        tk = params.tickers[d["target"]]
        out.update(ticker=tk, detail=f"{tk} 매수")
    elif d["action"] == "SWITCH":
        tk = params.tickers[d["target"]]
        out.update(ticker=tk, close_ticker=held_ticker,
                   detail=f"{held_ticker} 전량 매도 → {tk} 매수 (같은 장에서)")
    return out


def run_smoke(args: argparse.Namespace) -> None:
    """연결 점검용 단발 주문. **전략과 무관하고 journal에 포지션으로 기록하지 않는다.**

    API 경로(토큰 → 계좌 헤더 → 주문 → 체결 조회)가 실제로 도는지 최소 수량으로 확인하는
    용도다. 킬 스위치와 정규장 확인은 그대로 적용한다.

    ⚠️ 매수한 주식은 계좌에 남는다. 그 상태로는 전략의 신규 매수가 잔고 대조에서 막히므로
       (`이미 SOXS N주를 보유 중입니다`), 점검이 끝나면 --smoke-sell 로 정리할 것.
    """
    side = "BUY" if args.smoke_buy else "SELL"
    qty = args.smoke_buy or args.smoke_sell
    symbol = args.smoke_symbol.upper()
    live = bool(args.live)
    print(BAR)
    print(f" 연결 점검 주문  {symbol} {qty}주 시장가 {side}"
          f"   {'실거래' if live else '드라이런'}")
    print(BAR)

    if executor.HALT_FILE.exists():
        print(f" ⛔ 킬 스위치가 켜져 있습니다 ({executor.HALT_FILE}).")
        return
    creds = toss_api.Credentials.from_env()
    if creds is None:
        print(" ⛔ 자격증명이 없습니다 (TOSS_API_CLIENT_ID / SECRET / ACCOUNT_SEQ).")
        return
    client = toss_api.TossClient(creds)

    want = "regularMarket" if args.smoke_session == "regular" else "preMarket"
    try:
        cal = client._request("GET", "/api/v1/market-calendar/US") or {}
    except toss_api.TossError as e:
        print(f" ⛔ 장 운영 정보 조회 실패: {e}")
        return
    sess = (cal.get("today") or {}).get(want)
    if not sess:
        print(f" ⛔ 오늘 {want} 세션이 없습니다(휴장).")
        return
    start = toss_api.parse_session_time(sess.get("startTime"))
    end = toss_api.parse_session_time(sess.get("endTime"))
    now = pd.Timestamp.now(tz="UTC").to_pydatetime()
    if start and end and not (start <= now <= end):
        print(f" ⛔ {want} 시간이 아닙니다 ({start:%H:%M}~{end:%H:%M} KST 기준, "
              f"현재 {pd.Timestamp.now(tz='Asia/Seoul'):%H:%M} KST).")
        return
    print(f" 세션: {want}  ({start:%H:%M}~{end:%H:%M})")

    try:
        px = client.last_price(symbol)
        print(f" 현재가 ${px:,.4f}  →  예상 금액 약 ${px * qty:,.2f}")
        print(f" 보유 수량 {client.held_quantity(symbol):g}주 / "
              f"USD 매수가능 ${client.buying_power_usd():,.2f}")
    except toss_api.TossError as e:
        print(f" ⛔ 조회 실패: {e}")
        return

    # 프리장은 유동성이 얇아 시장가가 위험하다. 지정가로 상한/하한을 둔다.
    if args.smoke_session == "pre":
        limit = args.smoke_limit
        if limit is None:
            limit = px * (0.99 if side == "SELL" else 1.01)   # 1% 양보
        order_type, price = "LIMIT", limit
        print(f" 프리장이므로 지정가 ${price:,.2f} 로 냅니다 "
              f"(현재가 대비 {(price/px-1)*100:+.1f}%). 미체결분은 세션 종료 시 취소됩니다.")
    else:
        order_type, price = "MARKET", None

    if not live:
        print(f" 드라이런입니다. 실제로 보내려면 --live 를 붙이세요. "
              f"({order_type}{f' @ ${price:,.2f}' if price else ''})")
        return

    try:
        order = client.create_order(symbol=symbol, side=side, order_type=order_type,
                                    quantity=qty, price=price,
                                    client_order_id=f"smoke-{side}-{symbol}-{qty}")
        print(f" 주문 접수: orderId={order.get('orderId')}")
        final = executor.settle(client, order)
        q, avg, cost = executor.filled_summary(final)
        print(f" 상태 {final.get('status')}   체결 {q:g}주 @ ${avg:,.4f}"
              + (f"   비용 ${cost:,.2f}" if cost else ""))
        print(f" 보유 수량(체결 후) {client.held_quantity(symbol):g}주")
        # 점검 주문도 저널에 남긴다(action=SMOKE). 포지션 복원에서는 제외되므로
        # 전략 상태를 흔들지 않고, 대시보드 주문내역에는 보인다.
        journal.append(journal.Entry(
            timestamp=journal.now(), action="SMOKE", ticker=symbol, regime="-", z=0.0,
            signal_price=round(px, 4), fill_price=round(avg, 4), shares=q,
            equity_after=0.0,
            note=f"smoke {side} {order_type} id={final.get('orderId') or '-'} "
                 f"status={final.get('status')} cost={cost:.2f}"))
        if side == "BUY":
            print(f"\n ⚠️ 이 {qty}주는 계좌에 남습니다. 전략의 신규 매수를 막으므로 정리하세요:")
            print(f"    ./.venv/bin/python src/alerter.py --smoke-sell {qty} --live")
    except (toss_api.TossError, ValueError) as e:
        print(f" ⛔ 주문 실패: {e}")
    print(BAR)


def live_prices(params: Params) -> dict[str, float]:
    """토스 실시간 호가 {티커: 가격}. 못 받으면 빈 dict — 호출자가 Yahoo로 물러난다.

    **왜 토스인가**: Yahoo의 SOXL/SOXS 값은 소급조정된 시계열이라 실제 호가와 2% 가까이
    어긋난다(2026-09-23 실측: SOXL +1.97%, SOXS -1.95%). 3배 ETF에서 그만큼 틀리면
    화면과 텔레그램의 평가손익이 통째로 어긋난다. 실제로 사고팔 수 있는 값은 토스 쪽이다.

    ⚠️ **표시·기준가 전용이다. 신호 계산에는 절대 쓰지 마라.** 레짐과 z는 Yahoo 일봉으로
    계산하고(토스에는 과거 일봉 API가 없다), 백테스트 수치도 전부 그 위에 서 있다.
    고점(트레일링)과 `Holding` 에 넘기는 값도 Yahoo 시계열 그대로 둔다 — 현재가만 다른
    소스로 바꾸면 고점과 자가 달라져 판정이 어긋나고, tests/test_consistency.py 가
    보장하는 "백테스트=알리미" 관계도 깨진다.

    심볼 3개를 한 번의 호출로 받는다. 여기는 매매 프로세스 자신이라 토큰을 정당하게
    쓰는 쪽이므로, 콘솔이 쓰는 config/TRADING 락은 확인하지 않는다.
    """
    try:
        creds = toss_api.Credentials.from_env()
        if creds is None:
            return {}
        syms = list(params.tickers.values())
        rows = toss_api.TossClient(creds)._request(
            "GET", "/api/v1/prices", params={"symbols": ",".join(syms)}) or []
        out = {}
        for r in rows:
            v = float(r.get("lastPrice") or 0)
            if v > 0:
                out[str(r.get("symbol", "")).upper()] = v
        return out
    except Exception:
        return {}


def resolve_equity(params: Params, risk: RiskConfig) -> tuple[float, str]:
    """사이징에 쓸 자산과 그 출처. **증권사 실제 잔고를 우선한다.** (2026-09-23)

    예전에는 journal의 `equity_after` 만 읽었는데, `executor.record()` 가 받은 값을
    그대로 다시 적을 뿐이라 **자산이 영원히 고정**됐다. 그래서 이익이 나도 다음 주문이
    커지지 않아(복리가 안 됨) 백테스트와 실거래가 갈라졌고, 반대로 손실이 나면 저널
    자산이 실제보다 높아 preflight의 `주문 금액 > 매수 가능` 에 **조용히 막혔다.**

    이제 매 실행마다 증권사에 묻는다 — USD 예수금 + 전략 종목(SOXL/SOXS) 평가금액.
    수수료·슬리피지·배당이 전부 반영된 값이라 따로 계산할 필요가 없다. 조회가 안 되면
    (자격증명 없음, IP 미등록 등) journal 값으로 물러난다. 읽기 전용 실행에서도
    자격증명이 없으면 그냥 journal을 쓴다.

    돌려주는 출처 문자열은 화면에 그대로 찍어 **어느 값을 썼는지 숨기지 않는다.**
    """
    fallback = journal.current_equity(risk.total_capital_usd)
    creds = toss_api.Credentials.from_env()
    if creds is None:
        return fallback, "journal (증권사 자격증명 없음)"
    try:
        live = toss_api.TossClient(creds).strategy_equity_usd(
            [params.tickers["long"], params.tickers["short"]])
    except Exception as e:                      # 조회 실패가 알리미를 멈추게 하지 않는다
        return fallback, f"journal (증권사 조회 실패: {type(e).__name__})"
    if live <= 0:
        return fallback, "journal (증권사가 0을 돌려줌)"
    return live, "증권사 실제 잔고"


def run_execution(d: dict, pos: dict | None, equity: float, risk: RiskConfig,
                  args: argparse.Namespace) -> None:
    """오늘의 지시를 주문으로 실행하고 결과를 보고한다.

    순서: 계획 → 안전장치 전수 검사 → (live면) 주문 → 체결 확인 → journal 기록 → 보고.
    안전장치가 하나라도 걸리면 주문하지 않고 이유를 전부 보여준다.
    """
    live = bool(args.live)
    print("-" * 70)
    print(" 실거래 실행" if live else " 드라이런 (주문 나가지 않음)")

    # 같은 방향 재진입 — 팔고 되사면 왕복 비용만 나가므로 매매하지 않고 규칙만 갱신한다.
    if d["action"] == "HOLD" and d.get("restamp"):
        print(f"   {d['reason']}")
        if live:
            executor.record_restamp(d, pos, equity)
            print("   주문 없음. RESTAMP 로 기록해 다음 실행이 새 레짐 규칙을 쓰게 했습니다.")
        else:
            print("   주문 없음. --live 면 RESTAMP 기록만 남깁니다.")
        return

    creds = toss_api.Credentials.from_env()
    if creds is None:
        print("   ⛔ 토스증권 자격증명이 없습니다.")
        print("      config/.env 에 TOSS_API_CLIENT_ID / TOSS_API_CLIENT_SECRET /")
        print("      TOSS_API_ACCOUNT_SEQ 를 넣으세요 (accountSeq는 --toss-accounts 로 확인).")
        return

    # 사이징은 risk_manager 가 정한다(자본 하한 포함). executor는 금액만 받는다.
    plans = executor.build_plans(d, pos, position_size_usd(equity, risk))
    if not plans:
        print(f"   매매 지시가 없어({d['action']}) 주문하지 않습니다.")
        return

    client = toss_api.TossClient(creds)
    fixed = executor.reconcile_pending(client)   # 미체결로 남은 기록 자가 복구
    for f in fixed:
        print(f"   🔧 체결 사후확인: {f}")
    if fixed:
        pos = journal.current_position()          # 복구됐으면 포지션을 다시 읽는다
    guards = executor.Guards(max_order_usd=args.max_order, max_late_min=args.max_late)
    sold: set[str] = set()
    last_order: dict = {}
    last_filled = None
    blocked_why: list[str] = []
    done: list[str] = []

    for n, plan in enumerate(plans, 1):
        tag = f"[{n}/{len(plans)}]"
        # SWITCH의 매수 다리는 직전 매도 대금을 쓴다 — 실제 매수 가능 금액에 맞춘다.
        if plan.action == "OPEN" and sold:
            plan, why = executor.fund_buy_plan(client, plan, guards)
            if why:
                print(f"   {tag} ⛔ {why}")
                blocked_why.append(why)
                break
        print(f"   {tag} 계획: {plan.describe()}")
        print(f"        멱등키: {plan.client_order_id}")
        warns: list[str] = []
        blocks = executor.preflight(client, plan, guards, pos,
                                    ignore_session=args.ignore_session,
                                    sold=frozenset(sold), warn_out=warns)
        for w in warns:
            print(f"   {tag} ⚠️ {w}")
        if blocks:
            print(f"   {tag} ⛔ 주문하지 않았습니다:")
            for b in blocks:
                print(f"        · {b}")
            blocked_why += blocks
            break
        print(f"   {tag} ✅ 안전장치 통과")

        try:
            order = executor.execute(client, plan, live=live)
        except (toss_api.TossError, ValueError) as e:
            print(f"   {tag} ⛔ 주문 실패: {e}")
            blocked_why.append(str(e))
            break

        if live:
            order = executor.settle(client, order)
            filled = executor.filled_summary(order)
            print(f"   {tag} 주문 {order.get('orderId', '?')}  상태 {order.get('status')}")
            if filled[0] > 0:
                print(f"        체결 {filled[0]:.4f}주 @ ${filled[1]:.4f}"
                      + (f"  비용 ${filled[2]:,.2f}" if filled[2] else ""))
            else:
                print("        아직 체결 확인이 되지 않았습니다. 계좌에서 확인하세요.")
            executor.record(plan, d, order, equity, live=True)
            last_order, last_filled = order, filled
        else:
            print(f"   {tag} {order.get('plan')}")
            last_order = order
        done.append(plan.describe())
        if plan.side == "SELL":
            sold.add(plan.symbol)

    if live and done:
        print("   journal/trades.csv 에 기록했습니다.")
    if not live:
        print("   실제 주문은 나가지 않았습니다. 실행하려면 --live 를 붙이세요.")
    if blocked_why and done:
        print("   ⚠️ 일부만 실행됐습니다. 계좌와 journal을 반드시 대조하세요.")

    if args.notify:
        summary = " / ".join(done) if done else " / ".join(p.describe() for p in plans)
        notifier.send(notifier.format_execution(d, summary, blocked_why,
                                                last_order, live, last_filled))


def main() -> None:
    ap = argparse.ArgumentParser(description="SOXL/SOXS 알리미 겸 실행기")
    ap.add_argument("--record", action="store_true", help="오늘 지시를 journal에 기록")
    ap.add_argument("--fill", type=float, help="미체결 신호의 실제 체결가 입력")
    ap.add_argument("--shares", type=float, help="--fill 과 함께: 실제 체결 수량")
    ap.add_argument("--skip", action="store_true", help="신호를 받았지만 매매하지 않았음으로 처리")
    ap.add_argument("--date", help="--fill/--skip 대상 날짜 (기본: 가장 오래된 미체결)")
    ap.add_argument("--slippage", action="store_true", help="신호가 대비 체결가 리포트")
    ap.add_argument("--notify", action="store_true", help="텔레그램으로 알림 발송")
    ap.add_argument("--brief", action="store_true",
                    help="개장 직전 요약만 텔레그램으로 발송 (읽기 전용, 주문 없음)")
    ap.add_argument("--notify-test", action="store_true", help="텔레그램 설정 점검 + 테스트 발송")
    ap.add_argument("--chat-id", action="store_true", help="봇에게 메시지를 보낸 뒤 실행하면 chat id를 찾아줌")
    ap.add_argument("--toss-accounts", action="store_true",
                    help="토스증권 계좌 목록을 조회해 accountSeq 를 알려줍니다 (최초 설정용)")
    ap.add_argument("--execute", action="store_true",
                    help="주문 계획 + 안전장치 검사까지 수행 (드라이런, 주문은 나가지 않음)")
    ap.add_argument("--live", action="store_true",
                    help="실제 주문 실행. 되돌릴 수 없으니 --execute 로 먼저 확인하세요")
    ap.add_argument("--ignore-session", action="store_true",
                    help="정규장 시각을 해석하지 못할 때만 사용. 접수 시간 검사를 건너뜁니다")
    ap.add_argument("--max-order", type=float, default=20000.0, help="1회 주문 금액 상한(USD)")
    ap.add_argument("--min-capital", type=float, default=None,
                    help="자본 하한(USD). 자산이 이 아래면 채워 넣어 매수. 0이면 하한 없음")
    ap.add_argument("--smoke-buy", type=int, metavar="N",
                    help="연결 점검용: SOXS N주를 시장가 매수 (전략과 무관, --live 필요)")
    ap.add_argument("--smoke-sell", type=int, metavar="N",
                    help="연결 점검용: SOXS N주를 시장가 매도 (--smoke-buy 정리용)")
    ap.add_argument("--smoke-symbol", default="SOXS", help="--smoke-* 대상 종목")
    ap.add_argument("--smoke-session", choices=["regular", "pre"], default="regular",
                    help="--smoke-* 를 어느 세션에서 낼지. pre는 유동성이 얇으니 지정가를 씁니다")
    ap.add_argument("--smoke-limit", type=float,
                    help="--smoke-* 지정가. 생략하면 프리장은 현재가 기준 1%% 양보로 자동 계산")
    ap.add_argument("--max-late", type=int, default=120,
                    help="개장 후 이 분이 지나면 주문하지 않습니다 (기본 120분). "
                         "실측상 개장 직후가 최적이라 늦으면 건너뛰는 편이 낫습니다")
    ap.add_argument("--capital", type=float, default=5000.0)
    ap.add_argument("--exposure", type=float, default=None)
    args = ap.parse_args()

    if args.chat_id:
        found = notifier.find_chat_id()
        if not found:
            print("chat id를 찾지 못했습니다. 텔레그램에서 봇에게 아무 메시지나 보낸 뒤 다시 실행하세요.")
            print("(TELEGRAM_BOT_TOKEN 이 config/.env 에 설정돼 있어야 합니다)")
        else:
            for f in found:
                print(f"   chat_id = {f['chat_id']}   ({f['name']})")
            print("\n위 값을 config/.env 의 TELEGRAM_CHAT_ID 에 넣으세요.")
        return

    if args.smoke_buy or args.smoke_sell:
        run_smoke(args)
        return

    if args.toss_accounts:
        # 계좌 목록 조회는 accountSeq가 필요 없다.
        creds = toss_api.Credentials.from_env(require_account=False)
        if creds is None:
            print("config/.env 에 TOSS_API_CLIENT_ID 와 TOSS_API_CLIENT_SECRET 을 먼저 넣으세요.")
            return
        try:
            for a in toss_api.TossClient(creds).accounts():
                print(f"   accountSeq = {a.get('accountSeq')}   "
                      f"계좌번호 {a.get('accountNo')}   유형 {a.get('accountType')}")
            print("\n위 accountSeq 를 config/.env 의 TOSS_API_ACCOUNT_SEQ 에 넣으세요.")
        except toss_api.TossError as e:
            print(f"조회 실패: {e}")
        return

    if args.notify_test:
        cred = notifier.credentials()
        if cred is None:
            print("설정 없음: config/.env 에 TELEGRAM_BOT_TOKEN 과 TELEGRAM_CHAT_ID 를 넣으세요.")
            print("예시는 config/.env.example 참고.")
            return
        ok = notifier.send("✅ <b>알리미 연결 테스트</b>\nSOXL/SOXS 봇이 정상 연결됐습니다.")
        print("테스트 메시지를 보냈습니다." if ok else "발송 실패 — 토큰·chat id를 확인하세요.")
        return

    if args.slippage:
        rep = journal.slippage_report()
        if not rep:
            print("체결 기록이 없습니다. --record 로 신호를 남기고 --fill 로 체결가를 입력하세요.")
            return
        df = pd.DataFrame(rep)
        print(df.to_string(index=False))
        print(f"\n평균 슬리피지 {df.diff_pct.abs().mean():.3f}%  (백테스트 가정 편도 0.100%)")
        return

    if args.fill is not None or args.skip:
        t = journal.record_fill(args.fill or 0, args.date or "", args.shares, skipped=args.skip)
        if t is None:
            print("미체결 신호를 찾지 못했습니다. --record 로 먼저 신호를 남기세요.")
        elif args.skip:
            print(f"{t['timestamp'][:16]} {t['action']} {t['ticker']} → 미실행으로 처리했습니다.")
        else:
            sp = float(t["signal_price"] or 0)
            print(f"기록 완료: {t['timestamp'][:16]} {t['ticker']} "
                  f"신호가 ${sp:.2f} → 체결가 ${args.fill:.2f} "
                  f"(슬리피지 {(args.fill/sp-1)*100:+.3f}%)" if sp else "기록 완료")
        return

    params = Params()
    risk = RiskConfig(total_capital_usd=args.capital,
                      **({"exposure": args.exposure} if args.exposure is not None else {}),
                      **({"min_capital_usd": args.min_capital} if args.min_capital is not None else {}))

    sig_df = fetch_history(params.tickers["signal"], "max")
    legs = {"long": fetch_history(params.tickers["long"], "max"),
            "short": fetch_history(params.tickers["short"], "max")}
    common = sig_df.index.intersection(legs["long"].index).intersection(legs["short"].index)
    sig_df, legs = sig_df.loc[common], {k: v.loc[common] for k, v in legs.items()}
    signals = build_signals(sig_df, params).dropna(subset=["z", "regime"])
    legs = {k: v.loc[signals.index] for k, v in legs.items()}

    pos = journal.current_position()
    quotes = live_prices(params)        # 토스 실호가 (표시·기준가 전용, 실패 시 빈 dict)
    d = decide(signals, legs, params, pos, quotes)
    equity, equity_src = resolve_equity(params, risk)

    def px_for(ticker: str) -> float:
        """표시용 현재가. 토스 우선, 없으면 Yahoo 일봉 종가."""
        v = quotes.get((ticker or "").upper())
        if v:
            return float(v)
        side = "long" if ticker == params.tickers["long"] else "short"
        return float(legs[side]["Close"].iloc[-1])

    et_now = pd.Timestamp.now(tz="America/New_York")
    print(BAR)
    print(f" SOXL/SOXS 알리미   기준 거래일 {d['date'].date()}   (실행 {journal.now()})")
    print(f" 미국 동부 현재 {et_now:%Y-%m-%d %H:%M} ET"
          f"  — 신호는 완성된 일봉만 씁니다(미완성 당일 봉 제외)")
    print(BAR)

    pend = journal.pending_fills()
    if pend:
        print(" ⚠️ 체결 확인이 필요한 지시가 있습니다")
        for r in pend:
            print(f"    {r['timestamp'][:16]}  {r['action']} {r['ticker']}  "
                  f"신호가 ${float(r['signal_price']):.2f}  예정 {float(r['shares'] or 0):.2f}주")
        print("    실제로 매매했다면 : --fill <체결가> [--shares <수량>]")
        print("    매매하지 않았다면 : --skip")
        print("-" * 70)
    print(f" SOXX ${d['close']:.2f}   z = {d['z']:+.2f}   레짐 = {d['regime']} ({d['streak']}거래일째)")
    if d.get("view"):
        # 표시 전용. 매매는 위의 '레짐'으로만 한다(regime.describe 독스트링 참고).
        print(f" 장세 참고 = {d['view']}  (20일 {d['view_chg']:+.1%}, 매매에는 쓰지 않음)")
    print("-" * 70)
    if pos:
        tag = "" if pos.get("filled") else "  ⚠️ 체결가 미입력(신호가로 임시 계산)"
        print(f" 보유: {pos['ticker']} {pos['shares']:.2f}주 @ ${pos['entry_price']:.2f} "
              f"({pos['opened'][:10]} 진입){tag}")
        src = "토스 실호가" if d.get("price_src") == "toss" else "Yahoo 종가"
        print(f"       현재 ${d['price']:.2f}  평가 {d['unrealized']:+.2f}%  ({src})  "
              f"보유 {d['days']}거래일")
    else:
        print(f" 보유: 없음 (현금 ${equity:,.0f})")
    print(f" 사이징 기준 자산 ${equity:,.2f}  — 출처: {equity_src}")
    print("-" * 70)

    icon = {"OPEN": "▶ 매수", "CLOSE": "■ 매도", "SWITCH": "⇄ 교체",
            "HOLD": "· 보유", "NONE": "· 대기"}[d["action"]]
    print(f" 오늘의 지시:  {icon}   {d['detail'] or '아무것도 하지 않음'}")
    print(f"   사유: {d['reason']}")
    if d["action"] == "OPEN":
        amount = position_size_usd(equity, risk)
        px = px_for(d["ticker"])
        print(f"   금액: ${amount:,.0f} (자산 {risk.exposure*100:.0f}%)  "
              f"기준가 ${px:.2f} → 약 {amount/px:.2f}주")
        ext = extended_hours_price(d["ticker"])
        if ext:
            print(f"   참고: 연장시간 최종가 ${ext[0]:.2f} ({ext[1]}, 종가 대비 {(ext[0]/px-1)*100:+.2f}%)")
    print("-" * 70)
    print(" 체결 권고: 다음 거래일 정규장 개장 직후 (09:30~10:00 ET)")
    print("   백테스트가 검증한 체결 시점입니다. 애프터장은 스프레드가 넓어 가정이 깨질 수 있습니다.")
    print(BAR)

    if args.execute or args.live:
        run_execution(d, pos, equity, risk, args)
        print(BAR)
        return

    if args.brief:
        # 개장 직전 요약. **주문은 하지 않는다** — 이 경로는 읽기 전용이다.
        msg = notifier.format_brief(d, pos, equity, position_size_usd(equity, risk))
        pend = journal.pending_fills()
        if pend:
            msg += f"\n⚠️ 미체결 {len(pend)}건"
        print("\n" + msg.replace("<b>", "").replace("</b>", ""))
        print("\n 텔레그램 발송: " + ("성공" if notifier.send(msg) else "실패(--notify-test 로 확인)"))
        return

    if args.notify:
        ref = px_for(d["ticker"] or "")
        ext = extended_hours_price(d["ticker"]) if d["action"] == "OPEN" else None
        msg = notifier.format_alert(d, pos, equity, position_size_usd(equity, risk), ref, ext)
        pend = journal.pending_fills()
        if pend:
            msg += f"\n\n⚠️ 미체결 확인 필요 {len(pend)}건"
        print("\n 텔레그램 발송: " + ("성공" if notifier.send(msg) else "실패(설정 확인: --notify-test)"))

    if args.record:
        if d["action"] in ("OPEN", "CLOSE"):
            px = px_for(d["ticker"])
            shares = position_size_usd(equity, risk) / px if d["action"] == "OPEN" else (pos or {}).get("shares", 0)
            journal.append(journal.Entry(
                timestamp=journal.now(), action=d["action"], ticker=d["ticker"] or (pos or {}).get("ticker", ""),
                regime=d["regime"], z=round(d["z"], 3), signal_price=round(px, 4),
                fill_price=0, shares=round(shares, 4), equity_after=round(equity, 2),
                note=d["reason"]))
            print(f"\n journal/trades.csv 에 기록했습니다. 체결 후:")
            print(f"   ./.venv/bin/python src/alerter.py --fill <실제체결가>")
        else:
            print("\n 매매 지시가 없어 기록하지 않았습니다.")


if __name__ == "__main__":
    main()
