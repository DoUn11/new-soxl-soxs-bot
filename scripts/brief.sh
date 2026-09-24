#!/bin/bash
# 개장 직전 요약 알림 — 크론이 부른다. **읽기 전용, 주문 없음** (--live 를 주지 않는다).
# 미국 정규장은 22:30 KST 개장이므로 22:25에 보낸다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$ROOT/journal/logs/$(date '+%Y-%m').log"
mkdir -p "$(dirname "$LOG")"
OUT="$("$ROOT/.venv/bin/python" "$ROOT/src/alerter.py" --brief 2>&1)"
printf '%s 개장전 요약 발송 rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$?" >>"$LOG"
exit 0
