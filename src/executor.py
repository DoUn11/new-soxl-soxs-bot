"""주문 실행 — 전략 판단(alerter.decide)을 실제 주문으로 바꾼다.

**설계 원칙: 기본은 드라이런이다.** 실제 주문은 `live=True` 를 명시해야만 나간다.
전략 판단은 여기 없다(alerter/strategy 담당). 여기는 사이징·안전장치·주문 전송만 한다.

안전장치 (모두 통과해야 주문이 나간다):
  1. 킬 스위치   — `config/HALT` 파일이 있으면 어떤 주문도 내지 않는다.
  2. 정규장 확인 — 미국 정규장 시작 ~ 종료 1시간 전 구간에서만 주문한다.
                   백테스트가 검증한 체결 시점이고, 금액 주문·소수점 수량 주문이
                   접수되는 구간이기도 하다. 세션 시각을 해석할 수 없으면 **차단**한다.
  3. 포지션 대조 — journal이 믿는 포지션과 증권사 실제 보유가 다르면 중단한다.
                   (수동 매매, 부분 체결, 기록 누락을 자동으로 덮어쓰지 않기 위해)
  4. 금액 상한   — 1회 주문 금액 상한과 하루 주문 건수 상한.
  5. 멱등성      — clientOrderId 를 (신호일, 액션, 종목)으로 고정한다. 같은 날 재실행해도
                   서버가 같은 주문을 재반환하고(10분), journal 기록으로 그 이후도 막는다.

⚠️ 실API 검증은 **일부만** 됐다 (2026-09-22). 조회 전부와 수량 기반 LIMIT 주문·체결
   확인은 통과했지만, **전략 매수가 쓰는 `orderAmount`(금액 주문) 경로와 SWITCH의
   매도→매수 연속 자금은 아직 검증되지 않았다.** 첫 실거래는 금액을 제한해서 시작할 것
   (`--capital 500 --min-capital 0`). toss_api.py 독스트링에 항목별 현황이 있다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import journal
import toss_api
from toss_api import TossClient, TossError

HALT_FILE = Path(__file__).resolve().parent.parent / "config" / "HALT"


@dataclass(frozen=True)
class Guards:
    max_order_usd: float = 20_000.0   # 1회 주문 금액 상한
    max_orders_per_day: int = 4       # 하루 주문 건수 상한
    min_order_usd: float = 10.0       # 이보다 작으면 주문하지 않는다
    preferred_window_min: int = 30    # 개장 후 이 시간 안에 내는 것이 목표
    max_late_min: int = 120           # 개장 후 이 시간이 지나면 주문하지 않는다

    # 체결 시각 상한의 근거 (1시간봉 2.9년, 실제 신호일 35건 실측):
    # 진입·청산 시각 8×8 = 64개 조합에서 **09:30 개장이 최고**였고 늦출수록 단조롭게
    # 나빠졌다. 09:30 진입 기준 청산 시각별 누적은
    #   09:30 30.9배 / 10:30 30.2배 / 11:30 27.3배 / 12:30 16.2배 / 15:30 13.1배 / 종가 23.0배.
    # 첫 한 시간은 사실상 동등하지만 12:30부터 반토막 난다. 그래서 목표는 30분,
    # 상한은 120분(≈11:30 ET)으로 둔다. 그 이후에는 거래를 **건너뛰는 편이 낫다.**
    # docs/STRATEGY.md 7장 참고.


@dataclass(frozen=True)
class Plan:
    action: str            # OPEN | CLOSE
    symbol: str
    side: str              # BUY | SELL
    amount_usd: float | None   # OPEN: 금액 주문
    quantity: float | None     # CLOSE: 수량 주문
    client_order_id: str
    reason: str

    def describe(self) -> str:
        what = (f"${self.amount_usd:,.2f} 금액 주문" if self.amount_usd is not None
                else f"{self.quantity:.6f}주 수량 주문")
        return f"{self.action} {self.symbol} {self.side} MARKET — {what}"


def _client_order_id(signal_date, action: str, symbol: str) -> str:
    """멱등성 키. 최대 36자, 영숫자와 -_ 만 허용."""
    return f"soxx-{signal_date:%Y%m%d}-{action}-{symbol}"[:36]


def build_plans(decision: dict, pos: dict | None, amount_usd: float) -> list[Plan]:
    """오늘의 지시를 주문 계획 목록으로 바꾼다.

    - OPEN   → 매수 1건
    - CLOSE  → 매도 1건
    - SWITCH → **매도 1건 + 매수 1건** (같은 장에서 순서대로)
    - HOLD / NONE → 빈 목록 (HOLD가 restamp면 호출자가 RESTAMP 기록만 남긴다)

    `amount_usd` 는 **이미 사이징이 끝난 금액**이다(`risk_manager.position_size_usd`).
    여기서 `equity * exposure` 를 다시 계산하지 않는 이유: 그렇게 했더니 자본 하한
    규칙이 빠져서, 화면에는 하한이 적용된 $5,000이 뜨는데 실제 주문은 $0으로 나갔다.
    사이징 정책은 risk_manager 한 곳에만 둔다.
    """
    action = decision.get("action")
    day = decision["date"]
    held = (pos or {}).get("ticker") or ""

    def sell(symbol: str) -> Plan:
        return Plan(action="CLOSE", symbol=symbol, side="SELL", amount_usd=None,
                    quantity=float((pos or {}).get("shares") or 0),
                    client_order_id=_client_order_id(day, "CLOSE", symbol),
                    reason=decision.get("exit_reason") or decision.get("reason", ""))

    def buy(symbol: str) -> Plan:
        return Plan(action="OPEN", symbol=symbol, side="BUY",
                    amount_usd=amount_usd, quantity=None,
                    client_order_id=_client_order_id(day, "OPEN", symbol),
                    reason=decision.get("reason", ""))

    if action == "OPEN":
        return [buy(decision["ticker"])]
    if action == "CLOSE":
        return [sell(held or decision.get("ticker") or "")] if (held or decision.get("ticker")) else []
    if action == "SWITCH":
        if not held:
            return [buy(decision["ticker"])]
        return [sell(held), buy(decision["ticker"])]
    return []


def executor_records(days: int = 3) -> list[dict]:
    """최근 executor 실행 기록. 하루 건수 상한과 중복 방지에 쓴다.

    ⚠️ **로컬 날짜로 "오늘"을 세지 않는다.** 미국 정규장(09:30~16:00 ET)은 한국시간으로
    22:30~05:00이라 **자정을 넘어간다.** 로컬 날짜를 기준으로 삼으면 같은 장 안에서
    날짜가 바뀌어 중복 주문이 통과할 수 있다. 그래서 최근 며칠을 보고, 중복 판정은
    아래 `already_done()` 이 **멱등성 키(신호일+액션+종목)** 로 한다.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [r for r in journal.read_all()
            if r.get("timestamp", "") >= cutoff
            and r.get("action") in ("OPEN", "CLOSE")
            and "executor" in (r.get("note") or "")]


def already_done(client_order_id: str) -> bool:
    """이 멱등성 키로 이미 실행된 기록이 있는지. 재실행 시 중복 주문을 막는다.

    서버 멱등성은 10분만 유효하므로(토스 스펙), 그 이후를 journal로 막는다.
    """
    return any(client_order_id in (r.get("note") or "") for r in executor_records())


def preflight(client: TossClient, plan: Plan, guards: Guards,
              pos: dict | None, *, ignore_session: bool = False,
              sold: frozenset = frozenset(),
              warn_out: list | None = None) -> list[str]:
    """주문을 막아야 하는 이유를 모두 모아 돌려준다. 빈 리스트면 통과.

    `sold` 는 이번 실행에서 이미 매도를 넣은 종목들이다. SWITCH의 매수 다리를 검사할 때
    직전에 팔아버린 종목이 아직 잔고에 남아 보일 수 있으므로 그 종목은 보유 검사에서 뺀다.

    경고(차단은 아님)는 `warn_out` 리스트에 담아 돌려준다.
    """
    blocks: list[str] = []
    warnings: list[str] = warn_out if warn_out is not None else []

    if HALT_FILE.exists():
        blocks.append(f"킬 스위치가 켜져 있습니다 ({HALT_FILE}). 파일을 지우면 해제됩니다.")

    if already_done(plan.client_order_id):
        blocks.append(f"같은 주문이 이미 실행됐습니다 (멱등키 {plan.client_order_id}). "
                      "재실행이어도 중복 주문을 내지 않습니다.")
    recent = executor_records(days=1)
    if len(recent) >= guards.max_orders_per_day:
        blocks.append(f"최근 24시간에 이미 {len(recent)}건 주문했습니다 "
                      f"(상한 {guards.max_orders_per_day}).")

    # 정규장 확인 — 금액 주문·소수점 수량은 '정규장 종료 1시간 전'까지만 접수된다.
    if not ignore_session:
        try:
            session = client.us_regular_session()
        except TossError as e:
            blocks.append(f"장 운영 정보를 조회할 수 없습니다: {e}")
            session = None
        if session is None:
            blocks.append("오늘은 미국 정규장이 열리지 않습니다(또는 세션 정보를 받지 못했습니다).")
        else:
            start, end = (toss_api.parse_session_time(s) for s in session)
            if start is None or end is None:
                blocks.append(f"정규장 시각을 해석할 수 없습니다: {session}. "
                              "확인 후 --ignore-session 으로만 진행하세요.")
            else:
                now = datetime.now(timezone.utc)
                amount_cutoff = end - timedelta(hours=1)   # 금액 주문·소수점 수량 접수 한계
                late_cutoff = min(start + timedelta(minutes=guards.max_late_min),
                                  amount_cutoff)
                if now < start:
                    blocks.append(f"아직 개장 전입니다 (개장 {start:%H:%M} UTC, 현재 {now:%H:%M} UTC).")
                elif now > late_cutoff:
                    mins = (now - start).total_seconds() / 60
                    blocks.append(
                        f"개장 후 {mins:.0f}분이 지났습니다 (상한 {guards.max_late_min}분). "
                        "실측상 개장을 놓치면 체결 시점이 나빠지므로 오늘은 건너뜁니다 "
                        f"(허용 {start:%H:%M}~{late_cutoff:%H:%M} UTC).")
                else:
                    mins = (now - start).total_seconds() / 60
                    if mins > guards.preferred_window_min:
                        late_note = (f"개장 후 {mins:.0f}분 경과 — 목표 "
                                     f"{guards.preferred_window_min}분을 넘겼습니다(주문은 진행).")
                        warnings.append(late_note)

    # 포지션 대조 — 기록과 실제 보유가 어긋나면 손대지 않는다.
    try:
        if plan.action == "OPEN":
            for sym in {plan.symbol, "SOXL", "SOXS"} - set(sold):
                held = client.held_quantity(sym)
                if held > 0:
                    blocks.append(f"신규 매수인데 이미 {sym} {held:g}주를 보유 중입니다. "
                                  "journal과 증권사 잔고를 먼저 맞추세요.")
            power = client.buying_power_usd()
            if power < guards.min_order_usd:
                blocks.append(f"USD 매수 가능 금액이 부족합니다 (${power:,.2f}).")
            elif plan.amount_usd and plan.amount_usd > power:
                blocks.append(f"주문 금액 ${plan.amount_usd:,.2f} > 매수 가능 ${power:,.2f}.")
        else:
            sellable = client.sellable_quantity(plan.symbol)
            if sellable <= 0:
                blocks.append(f"{plan.symbol} 판매 가능 수량이 0입니다. "
                              "이미 매도됐거나 기록이 어긋났습니다.")
            elif pos and pos.get("shares") and abs(sellable - float(pos["shares"])) / float(pos["shares"]) > 0.02:
                blocks.append(f"보유 수량 불일치: journal {float(pos['shares']):g}주 vs "
                              f"증권사 판매가능 {sellable:g}주 (2% 초과). 확인이 필요합니다.")
    except TossError as e:
        blocks.append(f"잔고 조회 실패: {e}")

    if plan.amount_usd is not None:
        if plan.amount_usd > guards.max_order_usd:
            blocks.append(f"주문 금액 ${plan.amount_usd:,.2f} 이 상한 ${guards.max_order_usd:,.2f} 을 넘습니다.")
        if plan.amount_usd < guards.min_order_usd:
            blocks.append(f"주문 금액 ${plan.amount_usd:,.2f} 이 최소 ${guards.min_order_usd:,.2f} 미만입니다.")
    return blocks


def execute(client: TossClient, plan: Plan, *, live: bool) -> dict:
    """주문을 보낸다. live=False면 실제로 보내지 않고 계획만 돌려준다."""
    if not live:
        return {"dryRun": True, "plan": plan.describe()}
    if plan.action == "OPEN":
        # 금액 주문(US MARKET 전용) — 자산의 정해진 비율을 정확히 투입한다.
        return client.create_order(symbol=plan.symbol, side="BUY", order_type="MARKET",
                                   order_amount=plan.amount_usd,
                                   client_order_id=plan.client_order_id)
    # 전량 매도 — 증권사 판매 가능 수량을 그대로 쓴다(소수점 허용: US MARKET SELL).
    qty = client.sellable_quantity(plan.symbol)
    return client.create_order(symbol=plan.symbol, side="SELL", order_type="MARKET",
                               quantity=qty, client_order_id=plan.client_order_id)


def settle(client: TossClient, order: dict, *, tries: int = 20, wait: float = 3.0) -> dict:
    """주문 체결 상태를 확인한다.

    `POST /api/v1/orders` 는 `orderId` 만 돌려주므로 체결 수량·평균가는 반드시
    `GET /api/v1/orders/{orderId}` 로 다시 읽어야 한다. 시장가라 보통 즉시 체결되지만
    보장은 없으므로 몇 번 확인하고, 그래도 미확정이면 마지막 상태를 그대로 돌려준다.
    """
    order_id = str(order.get("orderId") or "")
    if not order_id:
        return order
    last = order
    for _ in range(tries):
        last = client.get_order(order_id)
        if str(last.get("status")) in ("FILLED", "CANCELED", "REJECTED"):
            return last
        time.sleep(wait)
    return last


def filled_summary(order: dict) -> tuple[float, float, float]:
    """(체결수량, 평균체결가, 수수료+세금). 체결 정보가 없으면 0으로 돌려준다.

    필드명은 스펙의 `OrderExecution` 을 따른다:
    filledQuantity / averageFilledPrice / filledAmount / commission / tax.
    """
    ex = order.get("execution") or {}
    qty = float(ex.get("filledQuantity") or 0)
    price = float(ex.get("averageFilledPrice") or 0)
    if not price and qty:
        price = float(ex.get("filledAmount") or 0) / qty
    cost = float(ex.get("commission") or 0) + float(ex.get("tax") or 0)
    return qty, price, cost


def fund_buy_plan(client: TossClient, plan: Plan, guards: Guards) -> tuple[Plan, str | None]:
    """매수 계획의 금액을 **실제 매수 가능 금액**에 맞춘다.

    SWITCH의 매수 다리는 직전에 낸 매도 대금을 쓴다. 해외주식 결제는 T+1이라 매도 대금이
    즉시 매수 가능 금액에 반영되는지는 증권사 정책에 달려 있고 **이 코드는 그것을 확인하지
    못했다.** 그래서 반영된 만큼만 쓰고, 최소 금액에 못 미치면 이유를 돌려준다.
    """
    power = client.buying_power_usd()
    target = min(plan.amount_usd or 0.0, power, guards.max_order_usd)
    if target < guards.min_order_usd:
        return plan, (f"매수 가능 금액이 ${power:,.2f} 뿐입니다. 해외주식 매도 대금이 아직 "
                      "반영되지 않았을 수 있습니다(결제 T+1). 잠시 후 다시 실행하거나 "
                      "계좌에서 직접 확인하세요.")
    return replace(plan, amount_usd=target), None


def record_restamp(decision: dict, pos: dict | None, equity: float) -> None:
    """매매 없이 청산 규칙만 갱신했음을 남긴다 (strategy의 restamp).

    주문이 나가지 않았으므로 수량·체결가는 0이고, journal.current_position() 이 이 기록을
    보고 레짐과 보유일 기준만 새로 잡는다.
    """
    journal.append(journal.Entry(
        timestamp=journal.now(), action="RESTAMP",
        ticker=(pos or {}).get("ticker", ""), regime=decision.get("regime", ""),
        z=round(float(decision.get("z", 0)), 3),
        signal_price=round(float(decision.get("price") or decision.get("close") or 0), 4),
        fill_price=0, shares=0, equity_after=round(equity, 2),
        note=f"executor restamp {decision.get('exit_reason') or ''}".strip()))


def reconcile_pending(client: TossClient) -> list[str]:
    """저널에 체결가가 비어 있는 봇 주문을 증권사에서 다시 읽어 채운다.

    시장가라도 체결이 폴링 창(60초)을 넘길 수 있다. 그때 저널에는 수량 0·체결가 0이
    남는데, 그대로 두면 포지션 수량과 원가가 틀린 채로 굴러간다. 매 실행 앞에서
    한 번 훑어 자가 복구한다.
    """
    import csv as _csv
    rows = journal.read_all()
    fixed = []
    for r in rows:
        if r.get("action") not in ("OPEN", "CLOSE") or float(r.get("fill_price") or 0) > 0:
            continue
        note = r.get("note") or ""
        oid = next((t[3:] for t in note.split() if t.startswith("id=")), "")
        if not oid or oid == "-":
            continue
        try:
            o = client.get_order(oid)
        except TossError:
            continue
        qty, avg, cost = filled_summary(o)
        if qty <= 0 or avg <= 0:
            continue
        r["fill_price"] = str(round(avg, 4))
        r["shares"] = str(round(qty, 6))
        r["note"] = note.replace("PENDING", str(o.get("status") or "FILLED")) + \
            f" | 사후확인 cost={cost:.2f}"
        fixed.append(f"{r['timestamp'][:16]} {r['action']} {r['ticker']} "
                     f"{qty:g}주 @ ${avg:.4f}")
    if fixed:
        with journal.TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
            w = _csv.DictWriter(f, journal.FIELDS); w.writeheader(); w.writerows(rows)
    return fixed


def record(plan: Plan, decision: dict, order: dict, equity: float, *, live: bool) -> None:
    """실행 결과를 journal에 남긴다. note에 'executor'를 넣어 수동 기록과 구분한다."""
    qty, price, cost = filled_summary(order)
    status = str(order.get("status") or ("DRY_RUN" if not live else "UNKNOWN"))
    cost_note = f" cost={cost:.2f}" if cost else ""
    journal.append(journal.Entry(
        timestamp=journal.now(), action=plan.action, ticker=plan.symbol,
        regime=decision.get("regime", ""), z=round(float(decision.get("z", 0)), 3),
        # ⚠️ 거래 종목(SOXL/SOXS)의 기준가를 쓴다. decision["close"] 는 **SOXX 종가**라
        # 그걸 쓰면 슬리피지 계산과 포지션 원가가 통째로 어긋난다(2026-09-22에 겪음:
        # SOXS를 $35.85에 샀는데 저널에 559.34가 적혔다).
        signal_price=round(float(decision.get("ref_price") or 0), 4),
        fill_price=round(price, 4), shares=round(qty, 6),
        equity_after=round(equity, 2),
        note=(f"executor {'live' if live else 'dryrun'} {status}{cost_note} "
              f"{plan.client_order_id} id={order.get('orderId') or '-'} "
              f"{plan.reason}").strip(),
    ))
