#!/bin/sh
set -e

UPSTREAM_HOST="${UPSTREAM_HOST:-printer-cam}"

# Basic auth, if configured, is applied here as well as by go2rtc, so that
# /print is covered too -- it never reaches go2rtc. nginx accepts a SHA-1
# digest in its password file, which the stdlib can produce without pulling in
# apache2-utils.
if [ -n "${AUTH_USER:-}" ]; then
  python3 - "$AUTH_USER" "${AUTH_PASS:-}" <<'PY' > /etc/nginx/htpasswd
import base64, hashlib, sys
user, password = sys.argv[1], sys.argv[2]
digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
print(f"{user}:{{SHA}}{digest}")
PY
  chmod 600 /etc/nginx/htpasswd
  printf 'auth_basic "printer-cam";\nauth_basic_user_file /etc/nginx/htpasswd;\n' > /etc/nginx/auth.conf
  echo "printer-cam-web: basic auth enabled for user ${AUTH_USER}"
else
  : > /etc/nginx/auth.conf
  echo "printer-cam-web: no basic auth (AUTH_USER unset)"
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

# Keep the status endpoint alive on its own. If it stops, /print returns 502 and
# the overlay hides itself; the video is unaffected either way.
(
  while :; do
    python3 /opt/print_status.py || echo "printer-cam-web: status service exited, restarting"
    sleep 2
  done
) &

exec nginx -g 'daemon off;'
