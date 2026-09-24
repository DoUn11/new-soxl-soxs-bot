#!/bin/bash
# 대시보드 데이터 갱신 — cron에서 1시간마다 부른다.
#
# 무엇이 갱신되는가:
#   ✅ results/dashboard.json  — 데이터 스냅샷
#   ✅ results/dashboard.html  — **로컬에서 열어보는 페이지** (자동으로 최신)
#   ❌ claude.ai 에 발행된 페이지 — 재발행이 필요하고, 그건 이 스크립트가 못 한다.
#
# 왜 발행 페이지는 자동이 안 되는가: 발행된 아티팩트는 CSP가 외부 fetch를 전부 막아
# 이 장비의 데이터를 가져갈 수 없고, 런타임 기능 중 로컬 프로세스가 페이지에 데이터를
# 밀어넣을 수 있는 것도 없다. 휴대폰에서 보려면 Claude에게 "대시보드 갱신해"라고
# 요청해 재발행해야 한다.
#
# 설치 (직접 실행할 것):
#   crontab -e  후 아래 한 줄 추가 — 매시 정각
#     0 * * * * /Users/dounkim/ClaudeProject/new-soxl-soxs-bot/scripts/refresh_dashboard.sh
#
# 로컬 페이지 보기:
#   open results/dashboard.html          # 브라우저가 1분마다 자동 새로고침한다
#
# 로그: journal/logs/dashboard-YYYY-MM.log
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/journal/logs"
LOG="$LOG_DIR/dashboard-$(date '+%Y-%m').log"

mkdir -p "$LOG_DIR"
log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >>"$LOG"; }

if [ ! -x "$PY" ]; then
  log "ERROR 파이썬을 찾을 수 없습니다: $PY"
  exit 1
fi

# 주문을 내지 않는 조회 전용 스크립트라 킬 스위치와 무관하게 돌려도 안전하다.
OUT="$("$PY" "$ROOT/scripts/build_dashboard.py" 2>&1)"
RC=$?
printf '%s\n' "$OUT" >>"$LOG"
log "종료 rc=$RC"
exit "$RC"
