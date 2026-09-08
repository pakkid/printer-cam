# Front door for the stack: nginx routes, and a small stdlib-only Python
# service turns Moonraker's output into the numbers the overlay needs and
# holds the on/off switch. Both live here so it is one container, one image.
FROM python:3.12-alpine

RUN apk add --no-cache nginx

COPY main.conf       /etc/nginx/conf.d/main.conf
COPY nginx.conf      /etc/nginx/http.d/default.conf
COPY admin.conf      /etc/nginx/http.d/admin.conf
COPY off.html        /opt/pages/_off.html
COPY admin.html      /opt/pages/admin.html
COPY print_status.py /opt/print_status.py
COPY entrypoint.sh   /usr/local/bin/entrypoint.sh

RUN touch /etc/nginx/auth.conf /etc/nginx/gate.conf \
      /etc/nginx/admin-allow.conf /etc/nginx/admin-auth.conf \
    && mkdir -p /data \
    # the alpine nginx package logs to files; send them where docker logs looks
    && ln -sf /dev/stdout /var/log/nginx/access.log \
    && ln -sf /dev/stderr /var/log/nginx/error.log

# 80 is the viewer (point your tunnel here); 81 is the switch (do not).
EXPOSE 80 81
VOLUME /data
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
