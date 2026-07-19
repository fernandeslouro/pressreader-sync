#!/bin/sh
set -eu

cd "$(dirname "$0")"
mkdir -p data/browser data/library data/state/diagnostics
chmod 700 data/browser data/state
chmod 755 data/library

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created deploy/.env. Replace PRESSREADER_SYNC_TOKEN before starting services."
fi

echo "PressReader Sync deployment directories are ready."
