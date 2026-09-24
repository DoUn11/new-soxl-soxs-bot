"""레짐 콘솔 — 맥에서 띄우고 폰 브라우저로 보는 **읽기 전용** 앱.

왜 이게 필요한가: `results/dashboard.html` 은 cron이 1시간마다 다시 만드는 **스냅샷**이고,
claude.ai에 발행한 페이지는 CSP 때문에 이 장비의 데이터를 아예 가져올 수 없다. 둘 다
"지금 레짐이 뭔지" 를 바로 보여주지 못한다. 이 서버는 그 자리를 메운다 — 폰에서 열어두면
15초마다 스스로 갱신된다.

**주문 기능은 없다.** 조회만 한다. 매매는 `alerter.py --live` 한 경로로만 나가야 하고,
안전장치(executor.preflight)를 우회하는 두 번째 주문 경로를 만들지 않기 위해서다.

의존성: 표준 라이브러리 + yfinance. 공용 venv(`../soxl-soxs-bot/.venv`)를 건드리지
않으려고 웹 프레임워크를 쓰지 않았다.

실행:
    ./scripts/serve.sh            # 주소와 접속 토큰을 출력한다
    ./.venv/bin/python src/server.py --port 8787

폰에서 보려면 맥과 **같은 와이파이**에 있어야 한다. 실행 시 출력되는 주소를 그대로 연다.
"""
from __future__ import annotations

import argparse
import json
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import journal  # noqa: E402
import regime as regime_mod  # noqa: E402
import strategy  # noqa: E402
from data_fetcher import fetch_history  # noqa: E402
from risk_manager import RiskConfig, position_size_usd  # noqa: E402
from strategy import (Holding, Params, build_signals, decide_action,  # noqa: E402
                      kind_for, mr_rule_for)

TOKEN_FILE = ROOT / "config" / "server_token"
APP_HTML = ROOT / "dashboard" / "app.html"

# 일봉은 자주 바뀌지 않는다. 장중 시세만 짧게 갱신한다.
BARS_TTL = 600.0     # 10분
QUOTE_TTL = 15.0     # 15초


class Cache:
    """yfinance 호출을 아껴 쓰기 위한 아주 단순한 TTL 캐시.

    폰에서 15초마다 폴링하는데 그때마다 일봉 전체를 받으면 야후가 막는다. 그래서
    일봉과 시세의 TTL을 따로 둔다. 락을 거는 이유는 ThreadingHTTPServer가 요청마다
    스레드를 띄우기 때문이다 — 동시에 두 요청이 들어오면 같은 시세를 두 번 받게 된다.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str, ttl: float, fn):
        with self._lock:
            hit = self._store.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            value = fn()
            self._store[key] = (time.time(), value)
            return value


CACHE = Cache()
PARAMS = Params()
RISK = RiskConfig(total_capital_usd=5000.0, fee_rate=0.001)


def _bars() -> dict[str, pd.DataFrame]:
    def load():
        return {k: fetch_history(t, "max")
                for k, t in (("signal", PARAMS.tickers["signal"]),
                             ("long", PARAMS.tickers["long"]),
                             ("short", PARAMS.tickers["short"]))}
    return CACHE.get("bars", BARS_TTL, load)


TRADING_LOCK = ROOT / "config" / "TRADING"


def _quotes_yahoo() -> dict[str, float | None]:
    import yfinance as yf
    out: dict[str, float | None] = {}
    for t in PARAMS.tickers.values():
        try:
            h = yf.Ticker(t).history(period="1d", interval="1m")
            out[t] = float(h["Close"].iloc[-1]) if len(h) else None
        except Exception:
            out[t] = None
    return out


def _quotes_toss() -> dict[str, float | None] | None:
    """토스 실시간 호가. 쓸 수 없으면 None을 돌려 호출자가 Yahoo로 물러나게 한다.

    **왜 토스를 쓰는가**: Yahoo의 SOXL/SOXS 값은 소급조정된 시계열이라 실제 호가와
    2% 가까이 어긋난다(2026-09-23 실측: SOXL +1.97%, SOXS -1.95%). 3배 ETF에서 그만큼
    틀리면 평가손익이 통째로 어긋난다. 토스 값이 실제로 사고팔 수 있는 가격이다.

    **왜 매매 중에는 쓰지 않는가**: 토스 토큰은 client당 1개만 유효하다. 여기서 토큰을
    새로 받으면 매매 프로세스(daily_trade.sh)의 토큰이 무효가 되는데,
    `toss_api._request()` 에는 401 재시도가 없어서 그 순간 주문이 그냥 실패한다.
    SWITCH 중간이면 매도만 되고 매수가 빠진다. 그래서 `config/TRADING` 락이 있는 동안은
    손대지 않고 Yahoo로 물러난다. 조회 정확도보다 주문이 우선이다.

    심볼 3개를 **한 번의 호출**로 받는다(토큰 사용을 최소화).
    """
    if TRADING_LOCK.exists():
        return None
    try:
        import toss_api
        creds = toss_api.Credentials.from_env()
        if creds is None:
            return None
        symbols = list(PARAMS.tickers.values())
        rows = toss_api.TossClient(creds)._request(
            "GET", "/api/v1/prices", params={"symbols": ",".join(symbols)}) or []
        by = {str(r.get("symbol", "")).upper(): float(r.get("lastPrice") or 0) for r in rows}
        out = {t: (by.get(t.upper()) or None) for t in symbols}
        return out if any(v for v in out.values()) else None
    except Exception:
        return None


def _quotes() -> dict[str, float | None]:
    """장중 현재가. 토스 우선, 안 되면 Yahoo. 실패해도 앱이 죽지 않는다."""
    def load():
        q = _quotes_toss()
        if q is not None:
            q["_source"] = "toss"       # type: ignore[assignment]
            return q
        q = _quotes_yahoo()
        q["_source"] = "yahoo"          # type: ignore[assignment]
        return q
    return CACHE.get("quotes", QUOTE_TTL, load)


def market_state(now_et: pd.Timestamp) -> dict:
    mins = now_et.hour * 60 + now_et.minute
    if now_et.weekday() >= 5:
        return dict(state="휴장", detail="주말", open=False)
    if mins < 9 * 60 + 30:
        left = 9 * 60 + 30 - mins
        return dict(state="개장 전", detail=f"{left // 60}시간 {left % 60}분 뒤 개장", open=False)
    if mins < 16 * 60:
        left = 16 * 60 - mins
        return dict(state="정규장", detail=f"마감까지 {left // 60}시간 {left % 60}분", open=True)
    return dict(state="마감", detail="애프터장 (체결 불가로 본다)", open=False)


def _label(frame: pd.DataFrame, view: pd.Series, idx) -> dict:
    row = frame.loc[idx]
    return dict(
        date=str(pd.Timestamp(idx).date()),
        close=round(float(row["close"]), 2),
        z=round(float(row["z"]), 2),
        regime=row["regime"],
        lt_trend=(row["lt_trend"] if pd.notna(row["lt_trend"]) else None),
        lt_chg=(round(float(row["lt_chg"]) * 100, 1) if pd.notna(row["lt_chg"]) else None),
        view=(view.loc[idx] if pd.notna(view.loc[idx]) else None),
    )


def build_state() -> dict:
    bars = _bars()
    raw_quotes = _quotes()
    # `_source` 는 시세가 아니라 출처 표시다. 숫자만 남겨 두지 않으면 아래
    # `round(v, 3)` 에서 문자열을 반올림하려다 터진다.
    quote_source = str(raw_quotes.get("_source") or "?")
    quotes = {k: v for k, v in raw_quotes.items() if k != "_source"}
    now_et = pd.Timestamp.now(tz="America/New_York")

    sig_df = bars["signal"]
    frame = build_signals(sig_df, PARAMS).dropna(subset=["z", "regime"])
    view = regime_mod.describe(frame["close"])
    settled = _label(frame, view, frame.index[-1])

    # 장중 잠정 — 지금 가격이 종가라면 레짐·z가 어떻게 되는지. **매매에는 쓰지 않는다**
    # (신호는 완성된 일봉만 쓴다). 사용자가 "지금 어디로 가고 있는지" 보려는 값이다.
    live = None
    px_signal = quotes.get(PARAMS.tickers["signal"])
    if px_signal:
        projected = sig_df.copy()
        stamp = pd.Timestamp(now_et.date())
        projected.loc[stamp, "Close"] = px_signal
        projected = projected.sort_index()
        pf = build_signals(projected, PARAMS).dropna(subset=["z", "regime"])
        pv = regime_mod.describe(pf["close"])
        live = _label(pf, pv, pf.index[-1])
        live["changed"] = (live["regime"] != settled["regime"])

    # 포지션 — 저널 기준. 평가손익은 장중 시세로 다시 계산한다.
    pos = journal.current_position()
    position = None
    decision = None
    if pos:
        side = "long" if pos["ticker"] == PARAMS.tickers["long"] else "short"
        px = quotes.get(pos["ticker"])
        entry = float(pos["entry_price"] or 0)
        shares = float(pos["shares"] or 0)
        unreal = (px / entry - 1) if (px and entry) else 0.0
        position = dict(ticker=pos["ticker"], side=side, entry=entry, shares=round(shares, 4),
                        price=(round(px, 3) if px else None),
                        unrealized=round(unreal * 100, 2),
                        pnl=(round((px - entry) * shares, 2) if px else None),
                        value=(round(px * shares, 2) if px else None),
                        regime=pos["regime"], opened=pos["opened"], filled=pos["filled"])

        since = min(pd.Timestamp(pos["opened"][:10]), frame.index[-1])
        hist = frame.loc[since:]
        leg = bars[side]["Close"].loc[since:]
        peak = float(leg.max()) if len(leg) else (px or entry)
        holding = Holding(side=side, regime=pos["regime"], kind=kind_for(pos["regime"]),
                          rule=mr_rule_for(pos["regime"]), bars_held=max(len(leg) - 1, 0),
                          peak_ratio=((px or entry) / peak if peak else 1.0),
                          unrealized=unreal)
        d = decide_action(frame.iloc[-1], holding, hist)
        decision = dict(action=d["action"], reason=d["reason"], exit_reason=d["exit_reason"])
    else:
        d = decide_action(frame.iloc[-1], None, None)
        amount = (position_size_usd(journal.current_equity(RISK.total_capital_usd), RISK)
                  if d["action"] in ("OPEN", "SWITCH") else None)
        decision = dict(action=d["action"], reason=d["reason"], exit_reason=None,
                        target=(PARAMS.tickers[d["target"]] if d["target"] else None),
                        amount=(round(amount, 2) if amount else None))

    # 청산까지 얼마나 남았는가 — 사람이 보기 위한 거리 계산
    gaps = None
    if pos:
        r = frame.iloc[-1]
        mid_gap = (0 - float(r["z"]))
        gaps = dict(
            regime=dict(label="레짐 이탈", hit=(r["regime"] != pos["regime"]),
                        detail=f"진입 {pos['regime']} → 현재 {r['regime']}"),
            center=dict(label="중심선 z=0", hit=(float(r["z"]) <= 0 if side == "short"
                                                 else float(r["z"]) >= 0),
                        detail=f"현재 z {float(r['z']):+.2f} ({mid_gap:+.2f}σ 남음)"),
        )

    orders = journal.bot_orders()[-20:][::-1]
    halted = (ROOT / "config" / "HALT").exists()

    return dict(
        now=str(now_et)[:19], now_kst=str(now_et.tz_convert("Asia/Seoul"))[:19],
        market=market_state(now_et), settled=settled, live=live,
        quotes={k: (round(v, 3) if v else None) for k, v in quotes.items()},
        quote_source=quote_source,
        position=position, decision=decision, gaps=gaps, orders=orders, halted=halted,
        params=dict(entry_z=strategy.SIDEWAYS_RULE.entry_z, lt_days=PARAMS.lt_days),
    )


class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, fmt, *args):  # 요청마다 터미널을 더럽히지 않는다
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # 같은 와이파이의 다른 사람에게 포지션·수익률이 그대로 보이면 곤란하므로
        # 토큰을 요구한다. 대단한 보안은 아니고 우연한 노출을 막는 수준이다.
        if q.get("t", [""])[0] != self.token:
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        if u.path in ("/", "/index.html"):
            self._send(200, APP_HTML.read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/state":
            try:
                body = json.dumps(build_state(), ensure_ascii=False, default=str).encode()
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:                      # 폰 화면이 백지가 되지 않도록
                self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                           "application/json; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="레짐 콘솔 (읽기 전용). 주문 기능 없음.")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="0.0.0.0", help="0.0.0.0이면 같은 와이파이에서 접속 가능")
    ap.add_argument("--new-token", action="store_true", help="접속 토큰을 새로 만든다")
    args = ap.parse_args()

    TOKEN_FILE.parent.mkdir(exist_ok=True)
    if args.new_token or not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(12))
        TOKEN_FILE.chmod(0o600)
    Handler.token = TOKEN_FILE.read_text().strip()

    url = f"http://{lan_ip()}:{args.port}/?t={Handler.token}"
    print("=" * 70)
    print(" 레짐 콘솔 (읽기 전용 — 주문 기능 없음)")
    print("=" * 70)
    print(f"  폰에서 열기:  {url}")
    print(f"  맥에서 열기:  http://127.0.0.1:{args.port}/?t={Handler.token}")
    print()
    print("  · 폰과 맥이 같은 와이파이에 있어야 합니다.")
    print("  · 15초마다 스스로 갱신됩니다. 종료는 Ctrl+C.")
    print("=" * 70)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
