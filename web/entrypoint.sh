#!/bin/sh
set -e

UPSTREAM_HOST="${UPSTREAM_HOST:-printer-cam}"
STATE_DIR="${STATE_DIR:-/data}"

# --- viewer password (also covers /print, which never reaches go2rtc) -------
if [ -n "${AUTH_USER:-}" ]; then
  python3 - "$AUTH_USER" "${AUTH_PASS:-}" <<'PY' > /etc/nginx/htpasswd
import base64, hashlib, sys
user, password = sys.argv[1], sys.argv[2]
digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
print(f"{user}:{{SHA}}{digest}")
PY
  # nginx's workers run as `nginx`, not root, and read the password file per
  # request -- root-only permissions here produce a 500, not a 401.
  chown nginx /etc/nginx/htpasswd && chmod 400 /etc/nginx/htpasswd
  printf 'auth_basic "printer-cam";\nauth_basic_user_file /etc/nginx/htpasswd;\n' > /etc/nginx/auth.conf
  echo "printer-cam-web: viewer basic auth enabled for user ${AUTH_USER}"
else
  : > /etc/nginx/auth.conf
  echo "printer-cam-web: no viewer basic auth (AUTH_USER unset)"
fi

# --- the switch's own port -------------------------------------------------
# Defence in depth only. The real separation is that this listens on a port
# your tunnel does not forward. Private ranges by default rather than a
# specific subnet, because whether nginx sees the true client address depends
# on how Docker publishes the port on your host.
ADMIN_ALLOW="${ADMIN_ALLOW:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.1}"
: > /etc/nginx/admin-allow.conf
for cidr in $ADMIN_ALLOW; do
  echo "allow $cidr;" >> /etc/nginx/admin-allow.conf
done
echo "deny all;" >> /etc/nginx/admin-allow.conf
echo "printer-cam-web: switch reachable from ${ADMIN_ALLOW}"

if [ -n "${ADMIN_PASS:-}" ]; then
  python3 - "${ADMIN_USER:-admin}" "$ADMIN_PASS" <<'PY' > /etc/nginx/admin-htpasswd
import base64, hashlib, sys
user, password = sys.argv[1], sys.argv[2]
digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
print(f"{user}:{{SHA}}{digest}")
PY
  chown nginx /etc/nginx/admin-htpasswd && chmod 400 /etc/nginx/admin-htpasswd
  printf 'auth_basic "camera switch";\nauth_basic_user_file /etc/nginx/admin-htpasswd;\n' \
    > /etc/nginx/admin-auth.conf
  echo "printer-cam-web: switch also password protected"
else
  : > /etc/nginx/admin-auth.conf
fi

# --- the always-on LAN port ------------------------------------------------
LAN_ALLOW="${LAN_ALLOW:-10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.1}"
: > /etc/nginx/lan-allow.conf
for cidr in $LAN_ALLOW; do
  echo "allow $cidr;" >> /etc/nginx/lan-allow.conf
done
echo "deny all;" >> /etc/nginx/lan-allow.conf
echo "printer-cam-web: always-on LAN viewer reachable from ${LAN_ALLOW}"

# --- restore the switch position before nginx starts -----------------------
mkdir -p "$STATE_DIR"
if [ -f "$STATE_DIR/enabled" ] && [ "$(cat "$STATE_DIR/enabled")" = "0" ]; then
  printf 'return 503;\n' > /etc/nginx/gate.conf
  echo "printer-cam-web: camera is SWITCHED OFF (restored from $STATE_DIR/enabled)"
else
  : > /etc/nginx/gate.conf
  echo "printer-cam-web: camera is live"
fi

resolve() {
  python3 -c "import socket,sys;print(socket.gethostbyname(sys.argv[1]))" "$1" 2>/dev/null
}

# nginx refuses to start at all if a proxy_pass hostname does not resolve, so
# wait for it rather than dying in a restart loop. depends_on only guarantees
# the other container was started, not that it is in DNS yet.
i=0
while [ $i -lt 60 ]; do
  ADDR=$(resolve "$UPSTREAM_HOST") && [ -n "$ADDR" ] && break
  [ $i -eq 0 ] && echo "printer-cam-web: waiting for ${UPSTREAM_HOST} to resolve..."
  i=$((i + 1))
  sleep 1
done
if [ -z "${ADDR:-}" ]; then
  echo "printer-cam-web: ${UPSTREAM_HOST} never resolved; is the printer-cam service running?" >&2
  exit 1
fi
echo "printer-cam-web: ${UPSTREAM_HOST} -> ${ADDR}"

# nginx caches that address for the life of the worker, so if go2rtc is
# recreated on its own it would keep proxying to an address nobody answers.
# Watch for the change and reload rather than requiring a manual restart.
(
  while :; do
    sleep 30
    NEW=$(resolve "$UPSTREAM_HOST") || continue
    if [ -n "$NEW" ] && [ "$NEW" != "$ADDR" ]; then
      echo "printer-cam-web: ${UPSTREAM_HOST} moved ${ADDR} -> ${NEW}, reloading"
      ADDR="$NEW"
      nginx -s reload || true
    fi
  done
) &

# Keep the status endpoint alive on its own. If it stops, /print returns 502
# and the overlay hides itself; the video is unaffected either way. The switch
# lives here too, so a crash must not leave it unusable.
(
  while :; do
    python3 /opt/print_status.py || echo "printer-cam-web: status service exited, restarting"
    sleep 2
  done
) &

exec nginx -g 'daemon off;'
