# Front door for the stack: nginx routes, and a small stdlib-only Python
# service turns Moonraker's output into the four numbers the overlay needs.
# Both live here so the whole thing is one container and one image.
FROM python:3.12-alpine

RUN apk add --no-cache nginx

COPY nginx.conf     /etc/nginx/http.d/default.conf
COPY upgrade.conf   /etc/nginx/http.d/upgrade.conf
COPY print_status.py /opt/print_status.py
COPY entrypoint.sh  /usr/local/bin/entrypoint.sh

RUN touch /etc/nginx/auth.conf \
    # the alpine nginx package logs to files; send them where docker logs looks
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

EXPOSE 80
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
