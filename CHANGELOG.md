# Changelog

## 1.1.0

- Reduces active-job checks from six per scan to one compartment-wide Resource Manager request.
- Treats OCI HTTP 429 responses as `THROTTLED` rather than a failed systemd service run.
- Adds a one-hour no-API cooldown after OCI throttling.
- Changes the launcher interval from 20 minutes to 40 minutes.
- Changes the second-stack delay from 300 seconds to 600 seconds.
- Moves the previous-day report from 8:00 AM to 7:50 AM America/New_York.
- Adds throttle entries and counts to the daily report.
- Decouples stack OCIDs and control server OCIDs from source code into environment configuration (`STACK_OCID_AD1` through `STACK_OCID_AD3E` or `STACK_OCIDS` JSON map).
- Hardens `upgrade.sh` to safely append newly required stack OCID environment keys and pause live provisioning until populated.
- Adds `upgrade.sh` to preserve configuration, state, and logs during an in-place upgrade.
