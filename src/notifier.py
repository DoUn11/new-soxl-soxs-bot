"""텔레그램 알림 발송.

토큰은 **절대 소스에 적지 않는다.** config/.env 에서 읽고, 그 파일은 .gitignore 대상이다.
설정 방법은 README의 "텔레그램 알림 설정" 참고.

환경변수:
    TELEGRAM_BOT_TOKEN   BotFather가 발급한 토큰
    TELEGRAM_CHAT_ID     내 계정의 chat id
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

ENV_PATH = Path(__file__).resolve().parent.parent / "config" / ".env"
API = "https://api.telegram.org/bot{token}/{method}"


def load_env() -> None:
    """config/.env 를 os.environ에 로드한다 (이미 설정된 값은 덮어쓰지 않는다)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def credentials() -> tuple[str, str] | None:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if token and chat_id else None


def send(text: str) -> bool:
    """메시지 발송. 설정이 없거나 실패하면 False를 돌려주고 예외를 던지지 않는다.

    알림 실패가 알리미 자체를 멈추게 해서는 안 되므로 조용히 실패한다.
    """
    cred = credentials()
    if cred is None:
        return False
    token, chat_id = cred
    try:
        r = requests.post(API.format(token=token, method="sendMessage"),
                          json={"chat_id": chat_id, "text": text,
                                "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=15)
        return r.ok
    except Exception:
        return False


def find_chat_id() -> list[dict]:
    """봇에게 아무 메시지나 보낸 뒤 실행하면 chat id를 찾아준다 (최초 설정용)."""
    cred_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not cred_token:
        load_env()
        cred_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not cred_token:
        return []
    try:
        r = requests.get(API.format(token=cred_token, method="getUpdates"), timeout=15)
        out = []
        for u in r.json().get("result", []):
            chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
            if chat:
                out.append(dict(chat_id=chat.get("id"), name=chat.get("first_name") or chat.get("title", "")))
        return out
    except Exception:
        return []


def format_alert(d: dict, pos: dict | None, equity: float, amount: float,
                 ref_price: float, ext: tuple[float, str] | None) -> str:
    """휴대폰에서 읽기 쉬운 짧은 메시지."""
    icon = {"OPEN": "🟢 매수", "CLOSE": "🔴 매도", "SWITCH": "🔄 교체",
            "HOLD": "⚪ 보유", "NONE": "⚫ 대기"}[d["action"]]
    lines = [f"<b>{icon}</b>",
             f"SOXX ${d['close']:.2f}  z={d['z']:+.2f}",
             f"레짐 <b>{d['regime']}</b> ({d['streak']}일째)",
             ""]
    if d["action"] == "OPEN":
        lines += [f"<b>{d['ticker']} 매수</b>",
                  f"금액 ${amount:,.0f}  기준가 ${ref_price:.2f}",
                  f"약 {amount/ref_price:.1f}주"]
        if ext:
            lines.append(f"연장시간 ${ext[0]:.2f} ({(ext[0]/ref_price-1)*100:+.2f}%)")
    elif d["action"] == "SWITCH":
        lines += [f"<b>{d.get('close_ticker', '')} 전량 매도 → {d['ticker']} 매수</b>",
                  "같은 장에서 두 주문을 연달아 실행합니다.",
                  f"평가 {d.get('unrealized', 0):+.2f}%  사유 {d['reason']}"]
    elif d["action"] == "CLOSE":
        lines += [f"<b>{d['ticker']} 전량 매도</b>",
                  f"평가 {d.get('unrealized', 0):+.2f}%  사유 {d['reason']}"]
    elif d["action"] == "HOLD" and pos:
        lines += [f"{pos['ticker']} 보유 중",
                  f"평가 {d.get('unrealized', 0):+.2f}%  {d.get('days', 0)}거래일"]
    else:
        lines.append(d["reason"])
    lines += ["", "체결: 다음 정규장 개장 직후 (09:30 ET)"]
    return "\n".join(lines)


def format_brief(d: dict, pos: dict | None, equity: float, amount: float) -> str:
    """개장 직전 요약 — **짧게**. (2026-09-24, 사용자 요청)

    담는 것은 넷뿐이다: 오늘 할 행동 / 현재 포지션(현금이면 금액, 주식이면 평가금액) /
    현재 레짐. 더 넣지 마라 — 매일 밤 오는 알림이라 길면 안 읽게 된다.
    자세한 내용은 SOXL/SOXS 봇 스테이터스(SOXL-SOXS 봇 스테이터스.app)에서 본다.
    """
    icon = {"OPEN": "🟢 매수", "CLOSE": "🔴 매도", "SWITCH": "🔄 교체",
            "HOLD": "⚪ 보유", "NONE": "⚫ 대기"}[d["action"]]
    lines = [f"<b>{icon}</b>  {d.get('detail') or '아무것도 하지 않음'}"]

    if pos:
        # 주식 포지션 — 평가금액과 손익
        val = equity
        lines.append(f"포지션 <b>{pos['ticker']}</b> {float(pos['shares']):.2f}주 · "
                     f"평가 <b>${val:,.0f}</b> ({d.get('unrealized', 0):+.2f}%)")
    else:
        # 현금 포지션 — 굴릴 수 있는 금액
        lines.append(f"포지션 <b>현금</b> · <b>${equity:,.0f}</b>"
                     + (f" (진입 시 ${amount:,.0f})" if d["action"] in ("OPEN", "SWITCH") else ""))

    lines.append(f"레짐 <b>{d['regime']}</b> ({d['streak']}일째) · z {d['z']:+.2f}")
    return "\n".join(lines)


def format_execution(d: dict, plan_desc: str, blocks: list[str], order: dict,
                     live: bool, filled: tuple[float, float, float] | None) -> str:
    """주문 실행 **결과** 보고. 신호 안내가 아니라 무엇을 했는지 알린다."""
    head = "🔴 실거래" if live else "🟡 드라이런"
    lines = [f"<b>{head} — {d['regime']} / z={d['z']:+.2f}</b>",
             f"SOXX ${d['close']:.2f}  ({d['date']:%Y-%m-%d} 종가 신호)", ""]
    if blocks:
        lines.append("<b>⛔ 주문하지 않았습니다</b>")
        lines += [f"· {b}" for b in blocks]
        lines += ["", f"계획: {plan_desc}"]
        return "\n".join(lines)
    lines.append(f"<b>{plan_desc}</b>")
    if not live:
        lines += ["", "실제 주문은 나가지 않았습니다 (--live 필요)."]
        return "\n".join(lines)
    status = str(order.get("status") or "접수")
    lines.append(f"주문 {order.get('orderId', '?')}  상태 <b>{status}</b>")
    if filled and filled[0] > 0:
        qty, price, cost = filled
        lines.append(f"체결 {qty:.4f}주 @ ${price:.4f}")
        if cost:
            lines.append(f"수수료·세금 ${cost:,.2f}")
    elif status not in ("FILLED",):
        lines.append("아직 체결 확인이 안 됐습니다. 계좌에서 확인하세요.")
    return "\n".join(lines)
