#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/snapshots
    # Fly volumes may be mounted root-owned. The application itself never runs as root.
    chown -R app:app /data
    exec gosu app "$@"
fi
exec "$@"
