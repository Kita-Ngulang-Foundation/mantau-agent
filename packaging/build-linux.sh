#!/bin/sh
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PROTOTYPE_DIR=$(CDPATH= cd -- "$REPO_DIR/.." && pwd)

build_one() {
    PLATFORM=$1
    TAG=$2
    NAME=$3
    docker buildx build --load --platform "$PLATFORM" \
        -f "$REPO_DIR/packaging/Dockerfile.pyinstaller" \
        -t "mantau-agent-pyi:$TAG" "$PROTOTYPE_DIR"
    mkdir -p "$REPO_DIR/dist"
    docker run --rm --platform "$PLATFORM" \
        -v "$REPO_DIR/dist:/out" "mantau-agent-pyi:$TAG" --name "$NAME"
}

build_one linux/amd64 amd64 mantau-agent-linux-x64
build_one linux/arm64 arm64 mantau-agent-linux-arm64
