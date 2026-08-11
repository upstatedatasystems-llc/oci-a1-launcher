# Changelog

## 1.2.1

- Fixes test isolation in `tests/test_launcher.py` by centralizing automatic test setup in an isolated temporary workspace directory.
- Ensures all unit tests run hermetically without requiring root privileges, sudo access, or touching production paths (`/var/lib/oci-a1-launcher` or `/etc/oci-a1-launcher`).
- Adds explicit regression test verifying that test execution completes cleanly without permission errors even when default production paths are inaccessible.

## 1.2.0

- Adds support for additional 1 OCPU / 6 GB ("E"/small-stack) Resource Manager stacks via `EXTRA_SMALL_STACKS_JSON`.
- Changes small-stack selection so an Availability Domain is not rejected merely because an A1 instance already exists in that AD.
- Preserves exact consumed stack OCID exclusions (`successful_stack_ocids`) so previously succeeded Terraform-managed stacks are never re-applied.
- Validates additional stack configurations against duplicate stack names, duplicate OCIDs, invalid AD numbers, and malformed JSON.
- Adds read-only `candidates` (and `plan`) diagnostic command to `launcherctl` (`sudo oci-a1-launcherctl candidates`) to inspect inventory, rotation, and stack eligibility without modifying resources.
- Adds comprehensive regression unit test suite (`tests/test_launcher.py`).
- Updates `upgrade.sh` to safely append `EXTRA_SMALL_STACKS_JSON` and maintain backward compatibility for existing v1.1.0 installations.

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
