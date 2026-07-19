#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/browser /library /state/diagnostics
    chown -R pwuser:pwuser /data/browser /library /state
    exec gosu pwuser "$0" "$@"
fi

exec "$@"
