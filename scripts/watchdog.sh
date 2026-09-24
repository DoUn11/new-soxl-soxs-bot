#!/bin/bash
# 무인 운전 감시 — 크론이 부른다. 읽기 전용, 주문 기능 없음.
#   ./scripts/watchdog.sh --check    # 매일: 문제 있을 때만 텔레그램
#   ./scripts/watchdog.sh --report   # 주 1회: 주간 요약을 항상 발송
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT/journal/logs/$(date '+%Y-%m').log"
mkdir -p "$ROOT/journal/logs"
OUT="$("$ROOT/.venv/bin/python" "$ROOT/src/watchdog.py" "$@" 2>&1)"
RC=$?
printf '%s 감시(%s) rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" "$RC" >>"$LOG"
[ $RC -ne 0 ] && printf '%s\n' "$OUT" | sed 's/^/    /' >>"$LOG"
exit 0
