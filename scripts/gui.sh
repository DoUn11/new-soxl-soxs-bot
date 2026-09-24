#!/bin/bash
# 레짐 콘솔 — 예전 이름. 실제 동작은 console_launch.sh 에 있다.
# (기존 문서·습관을 깨지 않으려고 이름만 남겨 뒀다.)
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/console_launch.sh" "$@"
