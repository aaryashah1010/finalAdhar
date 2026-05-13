#!/bin/sh
set -eu

mkdir -p "$AADHAAR_INPUT_DIR" "$AADHAAR_OUTPUT_DIR" /tmp/.X11-unix

Xvfb "$DISPLAY" -screen 0 "${VNC_RESOLUTION:-1280x900x24}" &

for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    if [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; then
        break
    fi
    sleep 0.25
done

fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

sleep 1
exec python app.py
