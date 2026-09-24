#!/bin/bash
# 레짐 콘솔 실행 — 맥에서 띄우고 폰으로 접속한다. 읽기 전용, 주문 기능 없음.
#
# 사용법:
#   ./scripts/serve.sh              # 기존 접속 토큰 유지
#   ./scripts/serve.sh --new-token  # 토큰을 새로 발급 (주소가 유출됐다고 의심될 때)
#
# 종료: 이 터미널에서 Ctrl+C. 맥 화면이 꺼지면(또는 뚜껑을 닫으면) 함께 멈춘다 —
# 자동매매 cron과 달리 예약 기상 대상이 아니다. 콘솔을 쓰는 동안만 맥을 켜 두면 된다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/src/server.py" "$@"
