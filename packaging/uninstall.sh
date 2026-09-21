#!/bin/sh
set -eu

PURGE=0
if [ "${1:-}" = "--purge" ]; then
    PURGE=1
elif [ "$#" -gt 0 ]; then
    echo "Usage: sudo sh packaging/uninstall.sh [--purge]" >&2
    exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "Run this uninstaller as root (sudo)." >&2
    exit 1
fi

systemctl disable --now mantau-agent.service >/dev/null 2>&1 || true
rm -f /etc/systemd/system/mantau-agent.service
rm -f /usr/local/bin/mantau-agent
systemctl daemon-reload

if [ "$PURGE" -eq 1 ]; then
    # Exact, fixed application-owned paths only. Default uninstall preserves
    # enrollment, camera configuration, sequence state, and pending events.
    rm -rf /etc/mantau-agent /var/lib/mantau-agent
    userdel mantau-agent >/dev/null 2>&1 || true
    groupdel mantau-agent >/dev/null 2>&1 || true
    echo "Mantau agent and its persisted state were removed."
else
    echo "Mantau agent removed; /etc/mantau-agent and /var/lib/mantau-agent were preserved."
fi
