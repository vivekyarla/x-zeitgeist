#!/bin/sh
# Platform volumes (Fly.io, Railway, Render) mount root-owned, so start as root just long
# enough to hand the data dir to the app user, then drop privileges for the server itself.
set -e
D="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$D"
  [ "$(stat -c %u "$D")" = "10001" ] || chown -R timeline:timeline "$D"
  exec setpriv --reuid=timeline --regid=timeline --init-groups "$@"
fi
exec "$@"
