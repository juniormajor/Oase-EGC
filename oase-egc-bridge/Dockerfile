# Base image pinned in the Dockerfile; build.yaml is deprecated and the
# Supervisor no longer passes BUILD_FROM by default (since 2026.04.0).
FROM ghcr.io/home-assistant/base:3.24
RUN apk add --no-cache python3 py3-pip openssl
COPY requirements.txt /tmp/
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt
COPY oase_bridge.py /app/oase_bridge.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
