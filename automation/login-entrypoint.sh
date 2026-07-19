#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/browser /library /state/diagnostics
    chown -R pwuser:pwuser /data/browser /library /state
    exec gosu pwuser "$0" "$@"
fi

export DISPLAY=:99
Xvfb :99 -screen 0 1440x1000x24 -ac -nolisten tcp &
sleep 1
fluxbox >/tmp/fluxbox.log 2>&1 &
x11vnc -display :99 -forever -shared -nopw -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &

exec python /app/pressreader_worker.py login
