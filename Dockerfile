# Thin layer over upstream go2rtc: our viewer + our config, nothing else.
# No build step, no compilation - this just bakes two directories in so the
# stack can be deployed straight from git with no bind mounts.
FROM alexxit/go2rtc:1.9.14

COPY www/ /app/www/
COPY config/go2rtc.yaml /config/go2rtc.yaml

EXPOSE 1984
