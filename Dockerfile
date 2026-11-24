# OpenFlux (Python port) — one image, two roles selected at runtime by ROLE.
FROM python:3.12-slim

WORKDIR /app
# iproute2 + iptables: only used by the L3 exit mode (MODE=l3, needs
# --cap-add NET_ADMIN and /dev/net/tun; not available on Render).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
        iproute2 iptables \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY openflux ./openflux
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
