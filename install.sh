#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer with sudo: sudo bash install.sh" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/oci-a1-launcher
CONFIG_DIR=/etc/oci-a1-launcher
DATA_DIR=/var/lib/oci-a1-launcher

apt-get update
apt-get install -y python3 python3-venv ca-certificates

install -d -m 0755 "$INSTALL_DIR" "$INSTALL_DIR/src" "$CONFIG_DIR"
install -d -m 0700 "$DATA_DIR"
install -m 0644 "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
install -m 0755 "$SOURCE_DIR/src/launcher.py" "$INSTALL_DIR/src/launcher.py"
install -m 0755 "$SOURCE_DIR/src/daily_report.py" "$INSTALL_DIR/src/daily_report.py"
install -m 0644 "$SOURCE_DIR/src/common.py" "$INSTALL_DIR/src/common.py"
install -m 0755 "$SOURCE_DIR/launcherctl" /usr/local/sbin/oci-a1-launcherctl

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel
"$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

if [[ ! -f "$CONFIG_DIR/launcher.env" ]]; then
  install -m 0600 "$SOURCE_DIR/config/launcher.env.example" "$CONFIG_DIR/launcher.env"
  echo "Created $CONFIG_DIR/launcher.env. Edit the Gmail app password before testing."
else
  chmod 0600 "$CONFIG_DIR/launcher.env"
  echo "Preserved existing $CONFIG_DIR/launcher.env."
fi

install -m 0644 "$SOURCE_DIR/systemd/oci-a1-launcher.service" /etc/systemd/system/oci-a1-launcher.service
install -m 0644 "$SOURCE_DIR/systemd/oci-a1-launcher.timer" /etc/systemd/system/oci-a1-launcher.timer
install -m 0644 "$SOURCE_DIR/systemd/oci-a1-daily-report.service" /etc/systemd/system/oci-a1-daily-report.service
install -m 0644 "$SOURCE_DIR/systemd/oci-a1-daily-report.timer" /etc/systemd/system/oci-a1-daily-report.timer
systemctl daemon-reload

echo
echo "Installation complete. Timers were NOT enabled."
echo "Next: edit $CONFIG_DIR/launcher.env, configure OCI IAM, then run the doctor command from README.md."
