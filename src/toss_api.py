"""토스증권 Open API 클라이언트 — HTTP 호출만 담당한다.

공식 스펙: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json (작성 시 v1.2.17)

  - 인증  : `POST /oauth2/token` (OAuth2 Client Credentials, form-urlencoded).
            발급 토큰을 모든 요청의 `Authorization: Bearer {token}` 헤더로 전달한다.
            **client 당 유효 토큰은 1개**이고 재발급 시 이전 토큰이 즉시 무효화되므로,
            같은 자격증명으로 두 프로세스를 동시에 돌리면 서로를 끊는다.
  - 계좌  : 계좌 관련 엔드포인트는 `X-Tossinvest-Account` 헤더에 `accountSeq`(정수)가 필요하다.
            계좌번호(`accountNo`)가 아니라 `accountSeq`다.
  - 응답  : 성공 `{"result": ...}` / 실패 `{"error": {requestId, code, message, data}}`
  - 금액·수량은 전부 **문자열 decimal**로 주고받는다.

이 모듈에는 전략 판단·사이징·안전장치를 넣지 않는다. 그건 executor.py가 담당한다
(AGENTS.md의 모듈 경계).

⚠️ **실제 주문이 나가는 코드다.**
   - 자격증명은 `config/.env` 에만 두고 로그·예외 메시지에 절대 남기지 않는다.
   - **IP 허용 목록 등록이 선행 조건이다.** 등록하지 않으면 토큰 발급이
     `403 access_denied "IP address not allowed"` 로 막힌다. 가정용 회선은 IP가 바뀌므로
     무인 운전에서 조용한 실패 원인이 된다.

실API 검증 현황 (2026-09-22, accountSeq=1 계좌):
   - ✅ `POST /oauth2/token`, `GET /api/v1/accounts`, `X-Tossinvest-Account` 헤더
   - ✅ `buying-power` / `holdings` / `sellable-quantity` / `prices` / `market-calendar`(US·KR)
   - ✅ `POST /api/v1/orders` — **수량 기반 LIMIT SELL** (프리장, SOXS 1주 → FILLED)
   - ✅ `GET /api/v1/orders/{orderId}` — 상태와 `execution` 필드 파싱
   - ✅ `parse_session_time` — 실제 형식은 `2026-09-22T22:30:00.000+09:00` (KST 오프셋).
        스펙에 명시가 없어 관용 파싱했는데 그대로 통과했다.
   - ❌ **`orderAmount`(금액 주문) 경로는 아직 미검증이다.** 전략의 매수가 이 경로를 쓴다.
   - ❌ `MARKET` 주문 유형, SWITCH의 매도→매수 연속 자금(결제 T+1) 도 미검증.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal

import requests

import notifier  # load_env 재사용 (config/.env 로딩)

BASE = "https://openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"
TIMEOUT = 15


class TossError(RuntimeError):
    """API가 반환한 오류. 자격증명은 담지 않는다."""

    def __init__(self, status: int, code: str, message: str, request_id: str = "", data=None):
        super().__init__(f"[{status} {code}] {message}".strip())
        self.status, self.code, self.message = status, code, message
        self.request_id, self.data = request_id, data


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    account_seq: int = 0      # 0이면 미설정. 계좌가 필요한 엔드포인트에서 막는다.

    @property
    def has_account(self) -> bool:
        return self.account_seq > 0

    @staticmethod
    def from_env(require_account: bool = True) -> "Credentials | None":
        """config/.env 의 TOSS_API_* 를 읽는다.

        `require_account=False` 면 accountSeq 없이도 돌려준다. **계좌 목록 조회는
        accountSeq가 필요 없으므로**, 처음 설정할 때 이 경로로 accountSeq를 알아낸다
        (그러지 않으면 "accountSeq를 알려면 accountSeq가 필요한" 모순이 된다).
        """
        notifier.load_env()
        cid = os.environ.get("TOSS_API_CLIENT_ID", "").strip()
        sec = os.environ.get("TOSS_API_CLIENT_SECRET", "").strip()
        seq = os.environ.get("TOSS_API_ACCOUNT_SEQ", "").strip()
        if not (cid and sec):
            return None
        try:
            n = int(seq) if seq else 0
        except ValueError:
            n = 0
        if require_account and n <= 0:
            return None
        return Credentials(cid, sec, n)


def dec_str(value, places: int) -> str:
    """스펙의 `^\\d+(\\.\\d+)?$` 를 만족하는 문자열로 만든다.

    지수 표기와 음수를 허용하지 않으므로 Decimal로 내림 처리한다.
    내림(ROUND_DOWN)인 이유: 매수 금액·수량이 의도보다 커지지 않게 한다.
    """
    q = Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    if q <= 0:
        raise ValueError(f"0 이하 값은 주문에 쓸 수 없습니다: {value}")
    return format(q, "f")


class TossClient:
    def __init__(self, creds: Credentials, base: str = BASE, timeout: int = TIMEOUT):
        self._creds = creds
        self._base = base.rstrip("/")
        self._timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0
        self._session = requests.Session()

    # ------------------------------------------------------------------ 인증

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token
        r = self._session.post(
            self._base + TOKEN_PATH,
            data={"grant_type": "client_credentials",
                  "client_id": self._creds.client_id,
                  "client_secret": self._creds.client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self._timeout,
        )
        try:
            body = r.json()
        except ValueError:
            raise TossError(r.status_code, "token-parse-failed", "토큰 응답을 해석할 수 없습니다.")
        if r.status_code != 200 or "access_token" not in body:
            # OAuth2 표준 오류 형식. error_description에 자격증명이 담길 일은 없다.
            raise TossError(r.status_code, str(body.get("error", "token-failed")),
                            str(body.get("error_description", "토큰 발급 실패")))
        self._token = body["access_token"]
        # 만료 60초 전에 갱신한다.
        self._token_expires_at = time.time() + max(int(body.get("expires_in", 600)) - 60, 30)
        return self._token

    # ------------------------------------------------------------------ 요청

    def _request(self, method: str, path: str, *, params=None, json_body=None,
                 with_account: bool = False):
        headers = {"Authorization": f"Bearer {self._access_token()}",
                   "Accept": "application/json"}
        if with_account:
            if not self._creds.has_account:
                raise TossError(0, "account-seq-missing",
                                "TOSS_API_ACCOUNT_SEQ 가 설정되지 않았습니다. "
                                "alerter.py --toss-accounts 로 확인해 config/.env 에 넣으세요.")
            headers["X-Tossinvest-Account"] = str(self._creds.account_seq)
        r = self._session.request(method, self._base + path, params=params, json=json_body,
                                  headers=headers, timeout=self._timeout)
        try:
            body = r.json()
        except ValueError:
            raise TossError(r.status_code, "parse-failed", f"응답을 해석할 수 없습니다 ({r.status_code}).")
        if r.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            raise TossError(r.status_code, str(err.get("code", "unknown")),
                            str(err.get("message", "")), str(err.get("requestId", "")),
                            err.get("data"))
        return body.get("result")

    # ------------------------------------------------------------------ 조회

    def accounts(self) -> list[dict]:
        """계좌 목록. 주문에 쓸 값은 `accountSeq` 다."""
        return self._request("GET", "/api/v1/accounts") or []

    def buying_power_usd(self) -> float:
        """현금 기준 USD 매수 가능 금액 (미수 미발생 기준)."""
        res = self._request("GET", "/api/v1/buying-power",
                            params={"currency": "USD"}, with_account=True) or {}
        return float(res.get("cashBuyingPower") or 0)

    def holdings(self, symbol: str | None = None) -> dict:
        return self._request("GET", "/api/v1/holdings",
                             params={"symbol": symbol} if symbol else None,
                             with_account=True) or {}

    def held_quantity(self, symbol: str) -> float:
        """해당 종목 보유 수량. 없으면 0."""
        for item in (self.holdings(symbol).get("items") or []):
            if str(item.get("symbol", "")).upper() == symbol.upper():
                return float(item.get("quantity") or 0)
        return 0.0

    def sellable_quantity(self, symbol: str) -> float:
        res = self._request("GET", "/api/v1/sellable-quantity",
                            params={"symbol": symbol}, with_account=True) or {}
        return float(res.get("sellableQuantity") or 0)

    def strategy_equity_usd(self, symbols) -> float:
        """**봇이 지금 굴리고 있는 돈** = 전략 종목(SOXL/SOXS)의 평가금액. 보유가 없으면 0.

        ⚠️ **USD 예수금은 세지 않는다.** 예전에는 더했다가 2026-09-24에 사용자가 바로
        잡았다 — 수수료용으로 $724를 입금하자 전략 자산이 $5,359→$6,359로 뛰어 다음
        주문이 의도보다 27% 커질 뻔했다. 계좌의 현금에는 수수료 여유분·다른 목적의
        입금이 섞이고, 이 계좌에는 국내 주식과 BRK·XXRP 같은 봇과 무관한 종목도 있다.
        **입금·출금은 전략 성과가 아니므로 자본을 바꾸면 안 된다.**

        즉 전략 자본은 **포지션 평가금액**이다. 이익이 나면 커지고 손실이 나면 작아진다
        (사용자가 고른 "전액 굴리기" = 복리). 포지션이 없으면 0을 돌려주고, 호출자가
        journal의 마지막 기록(= 직전 청산 대금)으로 물러난다.
        """
        total = 0.0
        wanted = {str(s).upper() for s in symbols}
        for item in (self.holdings().get("items") or []):
            if (str(item.get("symbol", "")).upper() in wanted
                    and str(item.get("currency", "")).upper() == "USD"):
                total += float((item.get("marketValue") or {}).get("amount") or 0)
        return total

    def last_price(self, symbol: str) -> float:
        rows = self._request("GET", "/api/v1/prices", params={"symbols": symbol}) or []
        for row in rows:
            if str(row.get("symbol", "")).upper() == symbol.upper():
                return float(row.get("lastPrice") or 0)
        raise TossError(200, "price-missing", f"{symbol} 현재가를 받지 못했습니다.")

    def us_regular_session(self) -> tuple[str, str] | None:
        """오늘 미국 정규장 (startTime, endTime). 휴장이면 None."""
        res = self._request("GET", "/api/v1/market-calendar/US") or {}
        today = res.get("today") or {}
        reg = today.get("regularMarket")
        if not reg:
            return None
        return str(reg.get("startTime")), str(reg.get("endTime"))

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/orders/{order_id}", with_account=True) or {}

    # ------------------------------------------------------------------ 주문

    def create_order(self, *, symbol: str, side: str, order_type: str = "MARKET",
                     quantity=None, order_amount=None, price=None,
                     time_in_force: str | None = None,
                     client_order_id: str | None = None) -> dict:
        """주문 생성. `quantity` 와 `order_amount` 중 정확히 하나만 준다.

        - `order_amount`(금액 주문)는 **US + MARKET 전용**이고, 정규장 시작부터
          정규장 종료 1시간 전까지만 접수된다.
        - 소수점 `quantity` 는 US MARKET **매도**에만 허용된다.
        - `client_order_id` 는 멱등성 키다(서버에서 10분간 유효). 같은 값으로 재요청하면
          이전 주문 결과를 그대로 돌려주므로, 재실행 시 중복 주문을 막는 데 쓴다.
        """
        if side not in ("BUY", "SELL"):
            raise ValueError(f"side는 BUY/SELL: {side}")
        if order_type not in ("LIMIT", "MARKET"):
            raise ValueError(f"order_type은 LIMIT/MARKET: {order_type}")
        if (quantity is None) == (order_amount is None):
            raise ValueError("quantity 와 order_amount 중 정확히 하나만 지정하세요.")
        if order_amount is not None and order_type != "MARKET":
            raise ValueError("금액 주문(order_amount)은 MARKET 만 허용됩니다.")
        if order_type == "LIMIT" and price is None:
            raise ValueError("LIMIT 주문은 price가 필요합니다.")
        if order_type == "MARKET" and price is not None:
            raise ValueError("MARKET 주문에 price를 줄 수 없습니다.")

        body: dict = {"symbol": symbol, "side": side, "orderType": order_type}
        if quantity is not None:
            # 소수점은 US MARKET 매도만 허용 → 그 외는 정수로 내림.
            fractional_ok = (order_type == "MARKET" and side == "SELL")
            body["quantity"] = dec_str(quantity, 6 if fractional_ok else 0)
        else:
            body["orderAmount"] = dec_str(order_amount, 2)
        if price is not None:
            body["price"] = dec_str(price, 4 if float(price) < 1 else 2)
        if time_in_force:
            body["timeInForce"] = time_in_force
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/orders", json_body=body, with_account=True) or {}


def parse_session_time(value: str) -> datetime | None:
    """세션 시각 문자열을 관용적으로 파싱한다.

    스펙에 형식이 명시돼 있지 않아 ISO 8601(오프셋 포함/미포함)만 처리하고,
    해석할 수 없으면 None을 돌려준다. 호출자는 None을 "판단 불가"로 다뤄야 한다.
    """
    v = (value or "").strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
