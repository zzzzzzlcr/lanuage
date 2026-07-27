#!/bin/bash
# 多开 Chrome + VNC 实例，每个独立端口
# Usage: bash start-multi-chrome.sh <数量> [起始display号]
# Example: bash start-multi-chrome.sh 3     → 启动3个实例
#          bash start-multi-chrome.sh 3 100  → 从 display :100 开始

set -e

COUNT="${1:-2}"
START_DISPLAY="${2:-99}"

RECORD_DIR="/company/record"
CHROME_BIN=$(command -v google-chrome || command -v chromium || echo "")

if [ -z "$CHROME_BIN" ]; then
    for p in /usr/bin/google-chrome /usr/bin/chromium /usr/bin/chromium-browser; do
        [ -x "$p" ] && { CHROME_BIN="$p"; break; }
    done
fi

echo "============================================"
echo "  Starting $COUNT Chrome + VNC instances"
echo "  Chrome: $CHROME_BIN"
echo "  Start display: :$START_DISPLAY"
echo "============================================"

for i in $(seq 0 $((COUNT - 1))); do
    DISPLAY_NUM=$((START_DISPLAY + i))
    VNC_PORT=$((5901 + i))
    NOVNC_PORT=$((6080 + i))
    CDP_PORT=$((9222 + i))
    USER_DATA="/tmp/chrome-profile-${i}"

    echo ""
    echo "--- Instance $((i + 1)) ---"
    echo "  DISPLAY  :${DISPLAY_NUM}"
    echo "  VNC      :${VNC_PORT}"
    echo "  noVNC    http://192.168.1.51:${NOVNC_PORT}/vnc.html"
    echo "  CDP      http://localhost:${CDP_PORT}"

    # 1. Xvfb
    Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 +extension RANDR &
    sleep 0.5

    # 2. openbox
    DISPLAY=":${DISPLAY_NUM}" openbox &
    sleep 0.5

    # 3. x11vnc
    x11vnc -display ":${DISPLAY_NUM}" -nopw -forever -quiet -rfbport "${VNC_PORT}" &
    sleep 0.5

    # 4. noVNC
    if [ -d /opt/novnc ]; then
        /opt/novnc/utils/novnc_proxy --listen "${NOVNC_PORT}" --vnc "localhost:${VNC_PORT}" &
    fi

    # 5. Chrome
    mkdir -p "$USER_DATA"
    "$CHROME_BIN" \
        --remote-debugging-port="${CDP_PORT}" \
        --no-first-run \
        --no-default-browser-check \
        --disable-gpu \
        --disable-sync \
        --no-sandbox \
        --display=":${DISPLAY_NUM}" \
        --window-size=1920,1080 \
        --user-data-dir="$USER_DATA" \
        about:blank &
    sleep 2
done

echo ""
echo "============================================"
echo "  All $COUNT instances started!"
echo "============================================"
echo ""
echo "  Instance summary:"
for i in $(seq 0 $((COUNT - 1))); do
    NOVNC_PORT=$((6080 + i))
    CDP_PORT=$((9222 + i))
    echo "  [$((i + 1))] VNC: http://192.168.1.51:${NOVNC_PORT}/vnc.html  CDP: ${CDP_PORT}"
done
echo ""
echo "  Stop all: pkill -f 'Xvfb|x11vnc|novnc_proxy|chromium'"
