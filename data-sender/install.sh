#!/bin/sh
# Install locally on Debian/Ubuntu. Run: sudo sh install.sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root: sudo sh install.sh" >&2
    exit 1
fi
command -v python3 >/dev/null
command -v systemctl >/dev/null
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'
SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d -m 0755 /usr/lib/workload-collector /etc/workload-collector
install -m 0644 "$SOURCE_DIR/workload_collector.py" /usr/lib/workload-collector/workload_collector.py
install -m 0644 "$SOURCE_DIR/systemd/workload-collector.service" /etc/systemd/system/workload-collector.service
systemctl daemon-reload
systemctl enable workload-collector.service
systemctl restart workload-collector.service
echo "Installed. Logs: journalctl -u workload-collector.service -f"
CONFIG_FILE="/usr/lib/workload-collector/config.toml"
if [ ! -f "$CONFIG_FILE" ]; then
    install -m 0644 "$SOURCE_DIR/config.toml" "$CONFIG_FILE"
    echo "Configuration file installed"
else
    echo "Existing configuration preserved"
fi
