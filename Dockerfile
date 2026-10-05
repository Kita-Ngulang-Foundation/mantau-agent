# Build from the standalone agent repository, for amd64 or arm64.
FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . /app
RUN set -eu; \
    ref="$(tr -d '[:space:]' < mantau-core.ref)"; \
    pip install --no-cache-dir "mantau-core @ https://github.com/Kita-Ngulang-Foundation/mantau-core/archive/${ref}.tar.gz"; \
    pip install --no-cache-dir .
ENV MANTAU_SEQ_PATH=/data/seq.txt
ENV MANTAU_SPOOL_PATH=/data/spool.db
ENV MANTAU_CLIP_SPOOL_DIR=/data/clips
ENV MANTAU_REQUIRE_HTTPS=true
VOLUME ["/data"]
CMD ["python", "-m", "mantau_agent.main"]
