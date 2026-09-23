# Build context is the mantau-prototype/ PARENT directory (this repo's
# sibling), e.g.: docker build -f Dockerfile -t mantau-agent ..
# Needs the mantau-core and mantau-AI sibling checkouts (mantau-AI provides
# on-device fall detection: MediaPipe pose, fall rules, ONNX classifier).
# Builds for linux/amd64 and linux/arm64 alike, e.g.:
#   docker buildx build --platform linux/arm64 -f mantau-agent/Dockerfile .
#
# Stands in for the target hardware (Orange Pi Zero 2W class, ~Rp350k,
# decided separately) for this weekend -- same image, same code either way;
# only the base image's architecture would need to change for a real ARM
# board (python:3.12-slim already publishes arm64 variants).
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgl1 libglib2.0-0 libegl1 libgles2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY mantau-AI /app/mantau-AI
COPY mantau-core /app/mantau-core
COPY mantau-agent /app/agent
# Golden protocol examples: only the contract tests read them.
COPY protocol/examples /app/protocol/examples

RUN pip install --no-cache-dir -e /app/mantau-AI \
 && pip install --no-cache-dir -e /app/mantau-core \
 && pip install --no-cache-dir -e "/app/agent[dev]"

WORKDIR /app/agent
ENV MANTAU_SEQ_PATH=/data/seq.txt
ENV MANTAU_SPOOL_PATH=/data/spool.db
ENV MANTAU_DETECTOR_BACKEND=mediapipe
VOLUME ["/data"]

CMD ["python", "-m", "mantau_agent.main"]
