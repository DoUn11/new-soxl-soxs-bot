#!/bin/bash
# 자동매매 일일 실행 — cron/launchd에서 불러 쓴다.
#
# 설계 의도: **여러 번 실행돼도 안전하다.**
#   - executor가 **개장 후 120분 안**이 아니면 주문을 거부한다(기본값 --max-late 120).
#     1시간봉 35건 실측에서 09:30 개장이 최적이고 12:30부터 반토막 났다. 개장을 놓치면
#     늦게 체결하는 것보다 **그날을 건너뛰는 편이 낫다.**
#   - 멱등성 키(신호일+액션+종목)로 같은 주문은 한 번만 나간다.
#   - 그래서 창을 촘촘히 두드려도 주문은 한 번, 개장 직후에만 나간다.
#
# 개장 시각 (KST): 서머타임 22:30 / 표준시 23:30. 120분 창이면 22:30~00:30 또는 23:30~01:30.
# 15분마다 두드려 두면 서머타임 전환(3월 초·11월 초)을 코드 수정 없이 흡수한다.
#
# 설치 (아직 설치하지 않았다. 직접 실행할 것):
#   crontab -e  후 아래 한 줄 추가
#     */15 22,23,0,1 * * 1-6 /Users/dounkim/ClaudeProject/new-soxl-soxs-bot/scripts/daily_trade.sh
#   (미국장이 KST 새벽까지 이어지므로 요일은 1-6으로 둔다)
#
# 즉시 중단:
#   touch config/HALT     # 이 파일이 있으면 어떤 주문도 나가지 않는다
#   rm config/HALT        # 해제
#
# 대시보드:
#   매매가 실제로 일어나면(저널이 바뀌면) build_dashboard.py 를 바로 돌려
#   results/dashboard.html 을 갱신한다. 아무 일도 없는 실행에서는 건너뛴다.
#   ⚠️ claude.ai 에 발행된 페이지는 이 스크립트가 갱신할 수 없다 — 재발행이 필요하다.
#
# 로그: journal/logs/YYYY-MM.log  (KST 기준 월별)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/journal/logs"
LOG="$LOG_DIR/$(date '+%Y-%m').log"

mkdir -p "$LOG_DIR"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >>"$LOG"; }

if [ ! -x "$PY" ]; then
  log "ERROR 파이썬을 찾을 수 없습니다: $PY"
  exit 1
fi

# 킬 스위치는 executor도 검사하지만, 여기서 먼저 걸러 불필요한 API 호출을 막는다.
if [ -f "$ROOT/config/HALT" ]; then
  log "HALT 파일이 있어 건너뜁니다"
  exit 0
fi

MODE="${1:---live}"   # 기본 --live. 점검할 때는 인자로 --execute 를 주면 드라이런.
log "시작 ($MODE)"

# 매매 중임을 알리는 락. **레짐 콘솔이 이 파일을 보고 토스 API 호출을 멈춘다.**
# 토스 토큰은 client당 1개만 유효해서, 콘솔이 토큰을 새로 받으면 이 프로세스의 토큰이
# 무효가 된다. toss_api._request() 에는 401 재시도가 없으므로 그 순간 주문이 실패하고,
# SWITCH 중간이면 매도만 되고 매수가 빠질 수 있다. 그래서 주문이 도는 동안에는
# 콘솔이 얌전히 Yahoo로 물러나게 한다.
LOCK="$ROOT/config/TRADING"
printf '%s pid=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$$" >"$LOCK"
trap 'rm -f "$LOCK"' EXIT INT TERM    # 어떤 경로로 끝나든 반드시 지운다

# 저널 지문을 미리 찍어둔다. 실행 후 달라지면 실제로 매매(또는 RESTAMP)가 일어난 것이다.
JOURNAL="$ROOT/journal/trades.csv"
before="$( [ -f "$JOURNAL" ] && wc -c <"$JOURNAL" || echo 0 )"

# cd 는 하지 않는다. alerter.py 가 자기 위치로 경로를 잡는다.
OUT="$("$PY" "$ROOT/src/alerter.py" "$MODE" --notify 2>&1)"
RC=$?
printf '%s\n' "$OUT" >>"$LOG"

# 매매가 있었으면 대시보드를 즉시 다시 만든다.
# 무조건 돌리지 않는 이유: 이 스크립트는 장중 15분마다 실행되고, 대시보드 생성은
# yfinance 와 토스 조회를 여러 번 호출한다. 아무 일도 없었는데 매번 부르면 낭비다.
after="$( [ -f "$JOURNAL" ] && wc -c <"$JOURNAL" || echo 0 )"
if [ "$before" != "$after" ]; then
  log "저널 변경 감지 ($before → $after 바이트) — 대시보드 재생성"
  DOUT="$("$PY" "$ROOT/scripts/build_dashboard.py" 2>&1)"
  DRC=$?
  printf '%s\n' "$DOUT" >>"$LOG"
  log "대시보드 재생성 rc=$DRC"
  log "※ claude.ai 발행 페이지는 재발행이 필요합니다 (Claude에게 요청)"
else
  log "저널 변경 없음 — 대시보드 건너뜀"
fi

log "종료 rc=$RC"
exit "$RC"
