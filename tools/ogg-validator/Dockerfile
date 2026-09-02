FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libtheora-bin \
        oggz-tools \
        vorbis-tools \
    && rm -rf /var/lib/apt/lists/*

COPY validate-ogv.sh /usr/local/bin/validate-ogv
RUN chmod 0755 /usr/local/bin/validate-ogv

ENTRYPOINT ["/usr/local/bin/validate-ogv"]
