#!/usr/bin/env bash
# Open or close the restricted SSH tunnel from an Actions job to the GPU pool host.
#
#   tunnel.sh open    needs TUNNEL_KEY, TUNNEL_HOST_KEY, TRANSPORT_TOKEN in the environment;
#                     forwards 127.0.0.1:18768 to the host job server and writes the
#                     job-server token to $RUNNER_TEMP/ziwu-transport/token
#   tunnel.sh close   closes the tunnel and removes the key and token
set -euo pipefail
DIR="$RUNNER_TEMP/ziwu-transport"
RELAY="${GPU_RELAY:?set the repository variable GPU_RELAY to user@host of the GPU pool relay}"
case "$1" in
  open)
    umask 077
    mkdir -p "$DIR"
    printf '%s\n' "$TUNNEL_KEY" > "$DIR/key"
    printf '%s\n' "$TUNNEL_HOST_KEY" > "$DIR/known_hosts"
    printf '%s\n' "$TRANSPORT_TOKEN" > "$DIR/token"
    ssh -fN -M -S "$DIR/socket" \
      -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
      -o UserKnownHostsFile="$DIR/known_hosts" \
      -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
      -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
      -i "$DIR/key" -L 127.0.0.1:18768:127.0.0.1:18771 "$RELAY"
    ;;
  close)
    if test -S "$DIR/socket"; then ssh -S "$DIR/socket" -O exit "$RELAY" || true; fi
    rm -f "$DIR/key" "$DIR/token"
    ;;
  *) echo "usage: $0 open|close" >&2; exit 2 ;;
esac
