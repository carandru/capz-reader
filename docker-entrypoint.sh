#!/bin/sh
set -eu

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

mkdir -p /data /cache /library
chown -R "$PUID:$PGID" /data /cache

exec gosu "$PUID:$PGID" "$@"
