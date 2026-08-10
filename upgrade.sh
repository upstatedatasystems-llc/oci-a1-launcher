#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this upgrade with sudo: sudo bash upgrade.sh" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG=/etc/oci-a1-launcher/launcher.env
DATA_DIR=/var/lib/oci-a1-launcher
BACKUP_DIR=/var/backups/oci-a1-launcher
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

launcher_enabled=false
daily_enabled=false
systemctl is-enabled --quiet oci-a1-launcher.timer 2>/dev/null && launcher_enabled=true || true
systemctl is-enabled --quiet oci-a1-daily-report.timer 2>/dev/null && daily_enabled=true || true

systemctl stop oci-a1-launcher.timer oci-a1-daily-report.timer 2>/dev/null || true
if systemctl is-active --quiet oci-a1-launcher.service; then
  echo "The launcher service is still running. Wait for it to finish, then rerun sudo bash upgrade.sh." >&2
  exit 1
fi
if systemctl is-active --quiet oci-a1-daily-report.service; then
  echo "The daily-report service is still running. Wait for it to finish, then rerun sudo bash upgrade.sh." >&2
  exit 1
fi

install -d -m 0700 "$BACKUP_DIR"
if [[ -f "$CONFIG" ]]; then
  cp -a "$CONFIG" "$BACKUP_DIR/launcher.env.$STAMP"
  chmod 0600 "$BACKUP_DIR/launcher.env.$STAMP"
fi
if [[ -d "$DATA_DIR" ]]; then
  tar -C /var/lib -czf "$BACKUP_DIR/runtime-data.$STAMP.tar.gz" oci-a1-launcher
  chmod 0600 "$BACKUP_DIR/runtime-data.$STAMP.tar.gz"
fi

bash "$SOURCE_DIR/install.sh"

set_env_value() {
  local key="$1" value="$2"
  if grep -qE "^${key}=" "$CONFIG"; then
    sed -i -E "s|^${key}=.*$|${key}=${value}|" "$CONFIG"
  else
    printf '\n%s=%s\n' "$key" "$value" >> "$CONFIG"
  fi
}

set_env_value SECOND_JOB_DELAY_SECONDS 600
set_env_value THROTTLE_COOLDOWN_SECONDS 3600
chmod 0600 "$CONFIG"

systemctl daemon-reload
systemctl reset-failed oci-a1-launcher.service oci-a1-daily-report.service 2>/dev/null || true

if [[ "$launcher_enabled" == true ]]; then
  systemctl enable --now oci-a1-launcher.timer
fi
if [[ "$daily_enabled" == true ]]; then
  systemctl enable --now oci-a1-daily-report.timer
fi

echo
echo "Upgrade complete."
echo "Preserved: $CONFIG and $DATA_DIR"
echo "Backups: $BACKUP_DIR/*.$STAMP and $BACKUP_DIR/*.$STAMP.tar.gz"
echo "SECOND_JOB_DELAY_SECONDS=600"
echo "THROTTLE_COOLDOWN_SECONDS=3600"
echo "Launcher interval: 40 minutes"
echo "Daily report: 07:50 America/New_York"
