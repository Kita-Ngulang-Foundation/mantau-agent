#!/bin/sh
set -eu

SERVICE_USER=mantau-agent
INSTALL_PATH=/usr/local/bin/mantau-agent
UNIT_PATH=/etc/systemd/system/mantau-agent.service
CONFIG_DIR=/etc/mantau-agent
CONFIG_PATH=$CONFIG_DIR/config.json
DATA_DIR=/var/lib/mantau-agent
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
NO_SETUP=0
BINARY_PATH=

usage() {
    echo "Usage: sudo sh packaging/install.sh [--no-setup] [path-to-binary]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --no-setup) NO_SETUP=1 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; exit 2 ;;
        *)
            if [ -n "$BINARY_PATH" ]; then usage >&2; exit 2; fi
            BINARY_PATH=$1
            ;;
    esac
    shift
done

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (sudo)." >&2
    exit 1
fi

case "$(uname -m)" in
    x86_64|amd64) ARTIFACT=mantau-agent-linux-x64 ;;
    aarch64|arm64) ARTIFACT=mantau-agent-linux-arm64 ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

if [ -z "$BINARY_PATH" ]; then
    BINARY_PATH=$SCRIPT_DIR/../dist/$ARTIFACT
fi
if [ ! -f "$BINARY_PATH" ]; then
    echo "Agent binary not found: $BINARY_PATH" >&2
    exit 1
fi

if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_USER"
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" \
        --shell /usr/sbin/nologin "$SERVICE_USER"
fi

systemctl stop mantau-agent.service >/dev/null 2>&1 || true
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$CONFIG_DIR" "$DATA_DIR"
install -o root -g root -m 0755 "$BINARY_PATH" "$INSTALL_PATH"
install -o root -g root -m 0644 "$SCRIPT_DIR/systemd/mantau-agent.service" "$UNIT_PATH"
systemctl daemon-reload

if [ ! -f "$CONFIG_PATH" ] && [ "$NO_SETUP" -eq 0 ]; then
    echo "Starting one-time enrollment and camera validation..."
    runuser -u "$SERVICE_USER" -- "$INSTALL_PATH" --config "$CONFIG_PATH" setup
fi

if [ -f "$CONFIG_PATH" ]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG_PATH"
    chmod 0600 "$CONFIG_PATH"
    systemctl enable --now mantau-agent.service
    echo "Mantau agent installed and running."
    echo "Local status: $INSTALL_PATH --config $CONFIG_PATH --status-path $DATA_DIR/status.json status --json"
else
    systemctl enable mantau-agent.service
    echo "Installed without configuration; service was enabled but not started."
    echo "Run: sudo -u $SERVICE_USER $INSTALL_PATH --config $CONFIG_PATH setup"
fi
