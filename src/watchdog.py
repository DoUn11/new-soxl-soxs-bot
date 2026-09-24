"""무인 운전 감시 — 조용히 멈춘 상태를 잡아낸다. (2026-09-23)

이 봇의 가장 위험한 실패는 손실이 아니라 **아무 일도 일어나지 않는 것**이다.
IP 허용 목록에서 빠지거나(가정용 IP는 바뀐다), 맥이 잠들거나, 토큰이 무효화되면
봇은 **아무 알림 없이 그냥 거래를 안 한다.** 사용자가 개입하지 않기로 한 기간에는
이걸 알아챌 방법이 없다. 그래서 이 스크립트가 따로 돈다.

두 가지 모드:
  --check   (매일)   이상 징후만 찾아 문제가 있을 때만 알린다
  --report  (주 1회) 이상 여부와 무관하게 주간 요약을 보낸다

검사 항목:
  1. 최근 N일 안에 daily_trade.sh 가 정상 종료한 기록이 있는가 (journal/logs)
  2. 증권사 API가 이 IP에서 되는가 (IP 허용 목록 이탈이 가장 잦은 원인)
  3. 저널 포지션과 증권사 실제 잔고가 맞는가
  4. 안전장치에 막힌 주문(⛔)이 로그에 있는가
  5. 킬 스위치가 켜진 채 방치돼 있는가
  6. 미체결로 남은 기록이 있는가

**주문 기능은 없다.** 조회와 알림만 한다 — server.py·gui_console.py와 같은 원칙이다.

실행:
    ./.venv/bin/python src/watchdog.py --check      # 문제 있을 때만 알림
    ./.venv/bin/python src/watchdog.py --report     # 주간 요약 (항상 알림)
    ./.venv/bin/python src/watchdog.py --report --no-notify   # 화면에만 출력
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import journal  # noqa: E402
import notifier  # noqa: E402

LOG_DIR = ROOT / "journal" / "logs"
HALT_FILE = ROOT / "config" / "HALT"
STALE_DAYS = 3          # 이 기간 안에 정상 실행이 없으면 이상으로 본다


def recent_log_lines(days: int = 10) -> list[str]:
    """최근 며칠치 실행 로그. 월별 파일이라 두 달치를 훑는다."""
    out: list[str] = []
    now = datetime.now()
    for month in {now.strftime("%Y-%m"), (now - timedelta(days=31)).strftime("%Y-%m")}:
        f = LOG_DIR / f"{month}.log"
        if f.exists():
            out += f.read_text(encoding="utf-8", errors="replace").splitlines()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    return [ln for ln in out if ln[:10] >= cutoff]


def session_runs(lines: list[str], hours: int = 14) -> int:
    """직전 미국 장 세션 동안 `--live` 가 몇 번 돌았는가.

    **왜 이게 따로 필요한가**: "최근 N일 안에 실행이 있었나"만 보면 **세션 중간에 멈춘
    것을 못 잡는다.** 2026-09-23 밤에 실제로 겪었다 — 23:20에 노트북을 닫아(Clamshell
    Sleep) 크론이 23:30~01:45을 통째로 건너뛰었는데, 마지막 실행이 23:15이라 "0일 전"
    으로 읽혀 경고가 뜨지 않았다. 그날은 지시가 HOLD라 놓친 거래가 없었지만, 매매가
    필요한 날이었다면 조용히 건너뛰었을 것이다.

    크론은 22:00~01:59 KST에 15분마다 돌므로 정상이면 세션당 약 16회다.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for ln in lines if ln[:19] >= cutoff and "시작 (--live)" in ln)


def last_success(lines: list[str]) -> str | None:
    """가장 최근에 정상 종료(rc=0)한 시각."""
    for ln in reversed(lines):
        if "종료 rc=0" in ln or "HALT 파일이 있어" in ln:
            return ln[:19]
    return None


def check() -> tuple[list[str], list[str]]:
    """(문제 목록, 정보 목록)."""
    problems: list[str] = []
    info: list[str] = []
    lines = recent_log_lines()

    # 1. 최근 실행 기록
    ok_at = last_success(lines)
    if ok_at is None:
        problems.append(f"최근 10일간 정상 실행 기록이 없습니다 (로그: {LOG_DIR})")
    else:
        age = (datetime.now() - datetime.strptime(ok_at, "%Y-%m-%d %H:%M:%S")).days
        (problems if age >= STALE_DAYS else info).append(
            f"마지막 정상 실행 {ok_at} ({age}일 전)")

    # 1-b. 직전 세션이 중간에 끊겼는가 (맥 절전이 가장 흔한 원인)
    runs = session_runs(lines)
    if runs == 0:
        info.append("직전 14시간 --live 실행 없음 (미국 휴장이면 정상)")
    elif runs < 12:
        problems.append(f"직전 장 세션이 중간에 끊겼습니다 — --live 가 {runs}회만 실행"
                        "(정상 약 16회). 맥이 절전에 들어갔을 가능성이 큽니다. "
                        "장 시간(22:30~05:00 KST)에는 뚜껑을 열어두거나 caffeinate 를 쓰세요.")
    else:
        info.append(f"직전 세션 --live {runs}회 실행 (정상)")

    # 2. 킬 스위치
    if HALT_FILE.exists():
        problems.append("킬 스위치가 켜져 있습니다 — 어떤 주문도 나가지 않습니다")

    # 3. 안전장치에 막힌 주문
    blocked = [ln for ln in lines if "⛔" in ln or "주문하지 않았습니다" in ln]
    if blocked:
        problems.append(f"막힌 주문 {len(blocked)}건: {blocked[-1][:120]}")

    # 4. 미체결
    pend = journal.pending_fills()
    if pend:
        problems.append(f"미체결로 남은 기록 {len(pend)}건 — --fill 또는 --skip 으로 처리 필요")

    # 5. 증권사 연결 + 잔고 대조 (IP 이탈이 가장 잦은 조용한 실패 원인)
    pos = journal.current_position()
    try:
        import toss_api
        creds = toss_api.Credentials.from_env()
        if creds is None:
            problems.append("토스 자격증명을 읽지 못했습니다 (config/.env)")
        else:
            c = toss_api.TossClient(creds)
            power = c.buying_power_usd()
            eq = c.strategy_equity_usd(["SOXL", "SOXS"])
            # 자본(포지션 평가액)과 예비금(현금)은 **다른 것**이다 — 현금은 자본 하한
            # $5,000을 채우는 재원이지 굴리는 돈이 아니다(CLAUDE.md '사이징 자산').
            info.append(f"증권사 연결 정상 · 전략 자본 ${eq:,.2f} (포지션 평가액) "
                        f"· 예비금 ${power:,.2f}")
            if eq and eq < 5000 and power < (5000 - eq):
                problems.append(f"자본이 하한 미달(${eq:,.0f})인데 예비금이 부족합니다 "
                                f"(${power:,.0f}, ${5000-eq-power:,.0f} 모자람) — 다음 진입이 막힐 수 있습니다")
            if pos:
                held = c.held_quantity(pos["ticker"])
                shares = float(pos["shares"] or 0)
                gap = abs(held - shares) / shares * 100 if shares else 0.0
                line = f"{pos['ticker']} 저널 {shares:g}주 vs 증권사 {held:g}주 ({gap:.2f}% 차이)"
                (problems if gap > 2 else info).append(line)
            else:
                for sym in ("SOXL", "SOXS"):
                    if c.held_quantity(sym) > 0:
                        problems.append(f"저널은 '포지션 없음'인데 {sym}을 보유 중입니다")
    except Exception as e:                      # 연결 실패 자체가 알려야 할 사건이다
        problems.append(f"증권사 조회 실패: {type(e).__name__} {e} "
                        "(IP 허용 목록에서 빠졌을 가능성이 높습니다)")

    if pos:
        info.append(f"보유: {pos['ticker']} {float(pos['shares']):g}주 @ ${pos['entry_price']} "
                    f"({pos['opened'][:10]} 진입, 진입레짐 {pos['regime']})")
    else:
        info.append("보유: 없음")
    # 사이징은 증권사 실제 잔고를 쓴다(alerter.resolve_equity). journal 값은 그 조회가
    # 실패했을 때의 폴백이므로, 둘을 나란히 보여 줘야 어긋남을 알아챌 수 있다.
    info.append(f"저널 자산(폴백용) ${journal.current_equity(5000.0):,.2f}")
    return problems, info


def weekly_summary() -> list[str]:
    """지난 7일간 실제로 무슨 일이 있었는가."""
    out: list[str] = []
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    rows = [r for r in journal.read_all() if r.get("timestamp", "") >= cutoff]
    trades = [r for r in rows if r["action"] in ("OPEN", "CLOSE")]
    out.append(f"지난 7일 매매 {len(trades)}건")
    for r in trades:
        fp = float(r.get("fill_price") or 0)
        sp = float(r.get("signal_price") or 0)
        # 2026-09-22 이전 기록에는 signal_price에 SOXS가 아니라 **SOXX 종가**가 적힌 행이
        # 있다(executor.record 주석 참고). 그런 행의 슬리피지는 의미가 없으므로 버린다.
        ratio = (fp / sp - 1) if (fp and sp) else None
        slip = f" (신호가 대비 {ratio*100:+.2f}%)" if ratio is not None and abs(ratio) < 0.2 else ""
        out.append(f"  {r['timestamp'][:16]} {r['action']} {r['ticker']} "
                   f"{float(r['shares'] or 0):g}주 @ ${fp:.2f}{slip}")
    if not trades:
        out.append("  (매매 없음 — 진입 조건이 서지 않았거나 거부권에 막혔습니다)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="무인 운전 감시 (읽기 전용, 주문 없음)")
    ap.add_argument("--check", action="store_true", help="문제가 있을 때만 알린다")
    ap.add_argument("--report", action="store_true", help="주간 요약을 항상 보낸다")
    ap.add_argument("--no-notify", action="store_true", help="텔레그램 발송 없이 화면에만")
    args = ap.parse_args()

    problems, info = check()
    lines: list[str] = []
    if args.report:
        lines.append("📅 주간 리포트 — SOXL/SOXS 봇")
        lines += weekly_summary()
        lines.append("")
    if problems:
        lines.append("⚠️ 확인이 필요합니다")
        lines += [f"  · {p}" for p in problems]
        lines.append("")
    if args.report or problems:
        lines.append("현재 상태")
        lines += [f"  · {i}" for i in info]

    text = "\n".join(lines)
    if not text:
        print(f"[{journal.now()}] 이상 없음 — 알림을 보내지 않습니다.")
        return
    print(text)
    if not args.no_notify:
        ok = notifier.send(text)
        print(f"\n텔레그램 발송: {'성공' if ok else '실패(설정 없음이거나 오류) — 화면 출력만'}")
    # 문제가 있으면 크론이 알 수 있도록 종료 코드를 남긴다
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
