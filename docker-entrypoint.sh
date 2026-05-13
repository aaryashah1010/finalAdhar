#!/bin/sh
set -eu

mkdir -p "$AADHAAR_INPUT_DIR" "$AADHAAR_OUTPUT_DIR" /tmp/.X11-unix

Xvfb "$DISPLAY" -screen 0 "${VNC_RESOLUTION:-1280x900x24}" &
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display "$DISPLAY" -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

sleep 1
exec python app.py
