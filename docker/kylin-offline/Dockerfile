FROM python:3.12-slim-bookworm AS python-builder

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY . /src
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install \
        --disable-pip-version-check \
        --no-cache-dir \
        .

FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        exfatprogs \
        gpgv \
        kmod \
        nginx \
        parted \
        qemu-utils \
        udev \
        util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/apt-mirror2
COPY . /opt/apt-mirror2
COPY --from=python-builder /opt/venv /opt/venv

RUN install -m 0644 examples/kylin/mirror-amd64.list /etc/apt/mirror-kylin.list \
    && install -m 0644 docker/kylin-offline/nginx.conf /etc/nginx/nginx.conf \
    && install -m 0755 docker/kylin-offline/entrypoint.sh /usr/local/bin/kylin-entrypoint \
    && mkdir -p /srv/apt-mirror /mnt/kylin-mirror /mnt/kylin-transfer /var/www \
    && touch /kylin-target.vhdx /kylin-transfer.vhdx \
    && /opt/venv/bin/apt-mirror-offline --help >/dev/null \
    && nginx -t

ENV PATH="/opt/venv/bin:/opt/apt-mirror2/scripts/linux:${PATH}" \
    KYLIN_MIRROR_ROOT="/srv/apt-mirror" \
    KYLIN_MIRROR_MOUNT="/mnt/kylin-mirror" \
    KYLIN_MIRROR_NBD="/dev/nbd0" \
    KYLIN_MIRROR_PARTITION="1" \
    KYLIN_MIRROR_SUBDIR="mirror" \
    KYLIN_PUBLIC_SUBDIR="archive.kylinos.cn" \
    KYLIN_TRANSFER_MOUNT="/mnt/kylin-transfer" \
    KYLIN_TRANSFER_NBD="/dev/nbd1" \
    KYLIN_TRANSFER_PARTITION="1"

EXPOSE 80
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python3 -c "import http.client; c=http.client.HTTPConnection('127.0.0.1', 80, timeout=2); c.request('GET', '/__health'); assert c.getresponse().status == 200" || exit 1

ENTRYPOINT ["/usr/local/bin/kylin-entrypoint"]
CMD ["serve"]
