"""거래 기록 — 신호와 실제 체결을 남기고 현재 포지션 상태를 복원한다.

백테스트와 실거래의 차이를 나중에 대조하려면 **신호 시점 가격**과 **실제 체결 가격**을
따로 기록해야 한다. 둘의 차이가 곧 슬리피지이고, 그게 백테스트 가정(편도 0.1%)보다 크면
전략의 기대수익이 통째로 달라진다.

파일: journal/trades.csv (git에 포함 — 실거래 기록이므로 보존)
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

JOURNAL_DIR = Path(__file__).resolve().parent.parent / "journal"
TRADES_CSV = JOURNAL_DIR / "trades.csv"

FIELDS = ["timestamp", "action", "ticker", "regime", "z", "signal_price",
          "fill_price", "shares", "equity_after", "note"]


@dataclass
class Entry:
    timestamp: str
    action: str          # OPEN | CLOSE | RESTAMP | SKIPPED | SMOKE | HOLD | NONE
                         #   SMOKE 는 연결 점검용 단발 주문. 포지션 복원에서 제외된다
    ticker: str
    regime: str
    z: float
    signal_price: float  # 신호가 뜬 시점의 종가 (백테스트 기준가)
    fill_price: float    # 실제 체결가. 미체결이면 0
    shares: float
    equity_after: float
    note: str = ""


def _ensure() -> None:
    JOURNAL_DIR.mkdir(exist_ok=True)
    if not TRADES_CSV.exists():
        with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, FIELDS).writeheader()


def append(entry: Entry) -> None:
    _ensure()
    with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, FIELDS).writerow(asdict(entry))


def read_all() -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def current_position() -> dict | None:
    """가장 최근 OPEN 이후 CLOSE가 없으면 그 포지션이 열려 있는 것으로 본다.

    `RESTAMP` 는 **매매 없이 청산 규칙만 갈아끼운** 기록이다. 같은 방향으로 재진입할
    조건이 됐을 때 팔고 되사면 왕복 비용만 나가므로 거래하지 않는데, 그때 레짐·보유일·
    고점 기준은 새로 잡아야 한다(strategy.decide_action 의 restamp). 수량과 원가는
    실제로 바뀌지 않으므로 그대로 둔다.
    """
    rows = [r for r in read_all() if r["action"] in ("OPEN", "CLOSE", "RESTAMP")]  # SKIPPED 제외
    last_close = max((i for i, r in enumerate(rows) if r["action"] == "CLOSE"), default=-1)
    tail = rows[last_close + 1:]
    opens = [r for r in tail if r["action"] == "OPEN"]
    if not opens:
        return None
    r = opens[-1]
    # fill_price 가 "0"(미체결)이면 문자열로는 참이므로 수치로 판정해야 한다.
    fill = float(r["fill_price"] or 0)
    pos = dict(ticker=r["ticker"], regime=r["regime"],
               entry_price=fill if fill > 0 else float(r["signal_price"] or 0),
               filled=fill > 0,
               shares=float(r["shares"] or 0), opened=r["timestamp"])
    for s in tail:
        if s["action"] == "RESTAMP" and s["ticker"] == pos["ticker"]:
            pos["regime"] = s["regime"]      # 새 레짐의 청산 규칙을 쓴다
            pos["opened"] = s["timestamp"]   # 보유일·고점 기준을 여기서 다시 센다
    return pos


def bot_orders() -> list[dict]:
    """**봇이 낸 주문만** 돌려준다. 대시보드의 주문내역이 이걸 쓴다.

    왜 저널이 기준인가: 토스 주문 목록 API는 `clientOrderId` 를 돌려주지 않는다.
    그래서 증권사 쪽 내역만 보면 봇 주문과 사람이 직접 낸 주문을 구분할 수 없다.
    봇은 주문할 때마다 여기에 `executor ... id=<orderId>` 를 남기므로, 그 기록이
    "봇이 한 일"의 유일한 근거다. orderId로 증권사 상세를 덧붙일 수 있다.
    """
    out = []
    for r in read_all():
        note = r.get("note") or ""
        if "executor" not in note and "smoke" not in note:
            continue
        oid = ""
        for tok in note.split():
            if tok.startswith("id="):
                oid = tok[3:]
        out.append(dict(r, order_id=("" if oid == "-" else oid),
                        kind=("smoke" if "smoke" in note else "strategy")))
    return out


def current_equity(default: float) -> float:
    """가장 최근의 유효한 자산 값. 없으면 default.

    ⚠️ `equity_after` 가 문자열 `"0.0"` 이면 파이썬에서 **참**이다. 그래서 그냥
    `if r["equity_after"]` 로 걸러내면 0을 자산으로 채택해 버린다. 실제로 SMOKE 기록
    (점검 주문, equity_after=0)이 들어온 뒤 자산이 $0으로 읽혀 주문 금액이 0이 됐다.
    `fill_price` 에서 한 번 겪은 것과 같은 함정이라 **반드시 수치로 판정**한다.
    """
    for r in reversed(read_all()):
        try:
            v = float(r.get("equity_after") or 0)
        except ValueError:
            continue
        if v > 0:
            return v
    return default


def pending_fills() -> list[dict]:
    """체결가가 아직 입력되지 않은 매매 지시. 실제로 샀는지 확인하기 위한 목록."""
    return [r for r in read_all()
            if r["action"] in ("OPEN", "CLOSE") and not float(r["fill_price"] or 0)]


def record_fill(fill_price: float, timestamp: str = "", shares: float | None = None,
                skipped: bool = False) -> dict | None:
    """미체결 신호에 실제 체결가를 채운다.

    timestamp를 비우면 **가장 오래된 미체결 건**에 기록한다(보통 하나뿐이다).
    skipped=True면 "신호는 받았지만 매매하지 않음"으로 표시해 포지션 상태를 되돌린다.
    """
    rows = read_all()
    target = None
    for r in rows:
        if r["action"] not in ("OPEN", "CLOSE") or float(r["fill_price"] or 0):
            continue
        if timestamp and not r["timestamp"].startswith(timestamp):
            continue
        target = r
        break
    if target is None:
        return None
    if skipped:
        target["action"] = "SKIPPED"
        target["note"] = (target["note"] + " | 미실행").strip(" |")
    else:
        target["fill_price"] = str(fill_price)
        if shares is not None:
            target["shares"] = str(round(shares, 4))
        sp = float(target["signal_price"] or 0)
        if sp:
            target["note"] = (target["note"] +
                              f" | 슬리피지 {(fill_price/sp-1)*100:+.3f}%").strip(" |")
    with TRADES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader(); w.writerows(rows)
    return target


def slippage_excluded(row: dict) -> str | None:
    """슬리피지 통계에서 빼야 하는 기록이면 그 사유를, 아니면 None.

    빼는 이유는 둘이다. ① 봇 밖에서 넣은 수동 거래는 신호가가 봇의 것이 아니다.
    ② note에 "슬리피지 제외"를 남긴 기록 — 신호가가 Yahoo 소급조정값이라 실호가인
    체결가와 비교할 수 없는 경우(2026-09-22 SOXS: 신호가 559.34 vs 체결가 35.85).
    기준을 |차이| 크기로 잡지 않은 건 임계값이 곧 추측이라서다. 빼는 기록은 호출자가
    목록으로 보여줘야 한다 — 조용히 빼면 통계가 좋아 보이는 쪽으로만 틀어진다.
    """
    note = row.get("note") or ""
    if "슬리피지 제외" in note:
        return "신호가가 실호가가 아님"
    if "봇 외부" in note:
        return "수동 거래"
    return None


def slippage_report(include_excluded: bool = False) -> list[dict]:
    """신호가 대비 실제 체결가 차이 — 백테스트 가정(편도 0.1%)과 대조용.

    기본은 `slippage_excluded()` 에 걸리는 기록을 뺀다. `include_excluded=True` 면
    전부 돌려주고, 각 항목의 `excluded` 에 제외 사유(없으면 None)를 담는다.
    """
    out = []
    for r in read_all():
        if r["action"] in ("OPEN", "CLOSE") and r["fill_price"] and r["signal_price"]:
            sp, fp = float(r["signal_price"]), float(r["fill_price"])
            if sp <= 0 or fp <= 0:
                continue
            why = slippage_excluded(r)
            if why and not include_excluded:
                continue
            out.append(dict(timestamp=r["timestamp"], action=r["action"], ticker=r["ticker"],
                            signal=sp, fill=fp, diff_pct=(fp / sp - 1) * 100, excluded=why))
    return out


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
