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
# Require a password before the review screen (which shows Aadhaar PII) can be
# accessed. The password comes from VNC_PASSWORD (set in docker-compose.yml).
# The storepasswd step is wrapped in an `if` so that `set -e` cannot abort the
# container if it returns non-zero; if the password file can't be created we
# fall back to supplying the password inline.
VNC_PW="${VNC_PASSWORD:-aadhaar123}"
if x11vnc -storepasswd "$VNC_PW" /tmp/vncpass >/tmp/x11vnc_pw.log 2>&1 && [ -s /tmp/vncpass ]; then
    x11vnc -display "$DISPLAY" -forever -shared -rfbauth /tmp/vncpass -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
else
    x11vnc -display "$DISPLAY" -forever -shared -passwd "$VNC_PW" -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
fi
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

sleep 1
exec python app.py
