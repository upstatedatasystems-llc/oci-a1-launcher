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

append_env_key_if_missing() {
  local key="$1" default_value="$2" comment="${3:-}"
  if ! grep -qE "^${key}=" "$CONFIG"; then
    if [[ -n "$comment" ]]; then
      printf '\n# %s\n' "$comment" >> "$CONFIG"
    fi
    printf '%s=%s\n' "$key" "$default_value" >> "$CONFIG"
  fi
}

set_env_value SECOND_JOB_DELAY_SECONDS 600
set_env_value THROTTLE_COOLDOWN_SECONDS 3600

# Safely append placeholders for newly required stack OCID environment variables if missing
append_env_key_if_missing STACK_OCID_AD1 "REPLACE_WITH_STACK_OCID_AD1" "Resource Manager Stack OCIDs (Required for v1.1.0+)"
append_env_key_if_missing STACK_OCID_AD1E "REPLACE_WITH_STACK_OCID_AD1E"
append_env_key_if_missing STACK_OCID_AD2 "REPLACE_WITH_STACK_OCID_AD2"
append_env_key_if_missing STACK_OCID_AD2E "REPLACE_WITH_STACK_OCID_AD2E"
append_env_key_if_missing STACK_OCID_AD3 "REPLACE_WITH_STACK_OCID_AD3"
append_env_key_if_missing STACK_OCID_AD3E "REPLACE_WITH_STACK_OCID_AD3E"

# Safely append default for optional extra small stacks (v1.2.0+)
append_env_key_if_missing EXTRA_SMALL_STACKS_JSON "[]" "Optional additional 1 OCPU / 6 GB stacks (v1.2.0+)"

# Safely append defaults for resize-only provisioning mode (v1.3.0+)
append_env_key_if_missing PROVISIONING_MODE "STANDARD" "Provisioning Mode: STANDARD (new instances) or RESIZE_ONLY (resize existing VM)"
append_env_key_if_missing RESIZE_INSTANCE_OCID "REPLACE_WITH_EXISTING_INSTANCE_OCID" "Target instance OCID for RESIZE_ONLY mode"
append_env_key_if_missing RESIZE_STACK_OCID "REPLACE_WITH_EXISTING_STACK_OCID" "Resource Manager stack OCID managing target instance for RESIZE_ONLY mode"
append_env_key_if_missing RESIZE_TARGET_OCPUS "2" "Target OCPUs for RESIZE_ONLY mode"
append_env_key_if_missing RESIZE_TARGET_MEMORY_GB "12" "Target Memory (GB) for RESIZE_ONLY mode"

chmod 0600 "$CONFIG"

systemctl daemon-reload
systemctl reset-failed oci-a1-launcher.service oci-a1-daily-report.service 2>/dev/null || true

# Validate configuration
config_valid=true
mode="$(grep -E "^PROVISIONING_MODE=" "$CONFIG" | cut -d'=' -f2- | tr -d ' "' | tr '[:lower:]' '[:upper:]' || true)"
if [[ "$mode" == "RESIZE_ONLY" ]]; then
  res_inst="$(grep -E "^RESIZE_INSTANCE_OCID=" "$CONFIG" | cut -d'=' -f2- | tr -d ' "' || true)"
  res_stk="$(grep -E "^RESIZE_STACK_OCID=" "$CONFIG" | cut -d'=' -f2- | tr -d ' "' || true)"
  if [[ -z "$res_inst" || "$res_inst" == *"REPLACE"* || "$res_inst" == *"example"* || -z "$res_stk" || "$res_stk" == *"REPLACE"* || "$res_stk" == *"example"* ]]; then
    config_valid=false
  fi
elif grep -qE "^STACK_OCIDS=" "$CONFIG" && ! grep -qE "^STACK_OCIDS=.*(REPLACE|example)" "$CONFIG"; then
  config_valid=true
else
  for key in STACK_OCID_AD1 STACK_OCID_AD1E STACK_OCID_AD2 STACK_OCID_AD2E STACK_OCID_AD3 STACK_OCID_AD3E; do
    val="$(grep -E "^${key}=" "$CONFIG" | cut -d'=' -f2- | tr -d ' "' || true)"
    if [[ -z "$val" || "$val" == *"REPLACE"* || "$val" == *"example"* ]]; then
      config_valid=false
      break
    fi
  done
fi

if [[ "$daily_enabled" == true ]]; then
  systemctl enable --now oci-a1-daily-report.timer
fi

if [[ "$config_valid" == true ]]; then
  if [[ "$launcher_enabled" == true ]]; then
    systemctl enable --now oci-a1-launcher.timer
  fi
  echo
  echo "Upgrade complete."
  echo "Preserved: $CONFIG and $DATA_DIR"
  echo "Backups: $BACKUP_DIR/*.$STAMP and $BACKUP_DIR/*.$STAMP.tar.gz"
  echo "SECOND_JOB_DELAY_SECONDS=600"
  echo "THROTTLE_COOLDOWN_SECONDS=3600"
  echo "Launcher interval: 40 minutes"
  echo "Daily report: 07:50 America/New_York"
else
  # Pause launcher to prevent unconfigured runs
  install -d -m 0700 "$DATA_DIR"
  touch "$DATA_DIR/PAUSED"
  chmod 0600 "$DATA_DIR/PAUSED"
  systemctl disable oci-a1-launcher.timer 2>/dev/null || true

  echo
  echo "========================================================================"
  echo "ACTION REQUIRED BEFORE RESUMING PROVISIONING"
  echo "========================================================================"
  echo "v1.1.0 requires Resource Manager Stack OCIDs to be defined in"
  echo "$CONFIG."
  echo
  echo "Placeholder keys have been appended to $CONFIG."
  echo "Please edit $CONFIG and populate:"
  echo "  - STACK_OCID_AD1"
  echo "  - STACK_OCID_AD1E"
  echo "  - STACK_OCID_AD2"
  echo "  - STACK_OCID_AD2E"
  echo "  - STACK_OCID_AD3"
  echo "  - STACK_OCID_AD3E"
  echo "(or set STACK_OCIDS as a JSON string)."
  echo
  echo "The launcher timer remains PAUSED and DISABLED for safety."
  echo "After populating the stack OCIDs, run:"
  echo "  sudo oci-a1-launcherctl doctor"
  echo "  sudo oci-a1-launcherctl resume"
  echo "========================================================================"
fi
