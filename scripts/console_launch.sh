#!/bin/bash
# 레짐 콘솔 실행 — 로컬 서버를 띄우고 **크롬 앱 창**으로 연다. (2026-09-23)
#
# 왜 tkinter가 아닌가: 이 맥(macOS 26)의 파이썬은 CommandLineTools 3.9뿐이고 Tk가 **8.5**다.
# 2010년판 deprecated Aqua Tk라 최신 macOS에서 **창은 뜨는데 내용이 하나도 안 그려진다**
# (네이티브 위젯인 tk.Button만 보인다). Tk 8.6을 쓰려면 파이썬을 새로 깔아야 해서,
# 이미 잘 도는 웹 콘솔을 크롬 --app 모드로 띄우는 쪽을 택했다. 탭·주소창이 없고 Dock
# 아이콘도 따로 떠서 사실상 데스크톱 앱처럼 쓴다. src/gui_console.py 는 남겨 뒀지만
# Tk 8.6 환경이 생기기 전까지는 쓰지 않는다.
#
# 읽기 전용이다 — 서버(server.py)에 주문 기능이 없다.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PORT="${CONSOLE_PORT:-8787}"
LOG="$ROOT/journal/logs/console.log"
mkdir -p "$(dirname "$LOG")"

# Finder에서 띄우면 x86_64로 실행돼 arm64로 깔린 numpy/pandas가 import되지 않는다.
# uname -m 은 번역된 프로세스 기준이라 못 쓴다 — 하드웨어를 직접 묻는다.
ARCH=""
[ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && ARCH="arch -arm64"

# 토큰 파일이 없으면 여기서 만든다. server.py 를 부르면 서버가 떠서 멈추므로 쓸 수 없다.
TOKENFILE="$ROOT/config/server_token"
if [ ! -s "$TOKENFILE" ]; then
  mkdir -p "$(dirname "$TOKENFILE")"
  openssl rand -base64 16 | tr -d '=+/' | cut -c1-16 >"$TOKENFILE"
  chmod 600 "$TOKENFILE"
fi
TOKEN="$(cat "$TOKENFILE" 2>/dev/null)"
URL="http://127.0.0.1:$PORT/?t=$TOKEN"

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }
log "콘솔 실행 요청 (port=$PORT, arch강제=${ARCH:-없음})"

# 이미 우리 서버가 떠 있으면 다시 띄우지 않는다 (포트 충돌 방지)
if curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/api/state?t=$TOKEN"; then
  log "서버가 이미 실행 중"
else
  log "서버 시작"
  nohup $ARCH "$ROOT/.venv/bin/python" "$ROOT/src/server.py" --port "$PORT" >>"$LOG" 2>&1 &
  for _ in $(seq 1 40); do                     # 최대 20초 대기
    sleep 0.5
    curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/api/state?t=$TOKEN" && break
  done
fi

if ! curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:$PORT/api/state?t=$TOKEN"; then
  log "❌ 서버가 응답하지 않습니다. 이 로그 위쪽의 파이썬 오류를 확인하세요."
  osascript -e 'display alert "레짐 콘솔" message "서버를 띄우지 못했습니다.\njournal/logs/console.log 를 확인하세요."' 2>/dev/null
  exit 1
fi

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ]; then
  # --app 모드: 탭·주소창 없는 독립 창.
  # **--user-data-dir 가 핵심이다.** 이걸 빼면 크롬이 이미 떠 있을 때 기존 세션으로
  # 넘겨버려("기존 브라우저 세션에서 여는 중") 그냥 탭으로 열리고, 실행한 프로세스는
  # 바로 종료된다. 전용 프로필을 주면 독립 프로세스로 떠서 앱 창이 보장되고
  # Dock 아이콘도 따로 생긴다. 평소 쓰는 크롬 프로필과 섞이지 않는 장점도 있다.
  "$CHROME" --app="$URL" \
            --user-data-dir="$ROOT/.chrome-console" \
            --window-size=460,900 --no-first-run --no-default-browser-check \
            >>"$LOG" 2>&1 &
  log "크롬 앱 창 실행 (전용 프로필)"
else
  log "크롬이 없어 기본 브라우저로 엽니다"
  open "$URL"
fi
exit 0
