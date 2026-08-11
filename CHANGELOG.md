# Changelog

## 1.3.0

- Adds new `PROVISIONING_MODE=RESIZE_ONLY` operating mode for resizing existing Always Free Ampere A1 VMs (e.g., 1 OCPU / 6 GB to 2 OCPU / 12 GB) via an existing Resource Manager stack.
- Completely bypasses standard candidate rotation and new-instance provisioning logic when `RESIZE_ONLY` mode is active.
- Guarantees absolute safety: in `RESIZE_ONLY` mode, ZERO execution path can call create/apply on any other stack (AD1, AD2, `purgatory03-ad3e`, etc.).
- Eliminates capacity-report prechecks (`CreateComputeCapacityReport`) in `RESIZE_ONLY` mode to submit actual Resource Manager APPLY jobs each cycle without lagging capacity-report info.
- Authorizes `RESIZE_STACK_OCID` for resize attempts regardless of whether its OCID appears in `successful_stack_ocids`.
- Preserves existing VM intact on Out of Host Capacity errors during resize, with zero fallback to other stacks or ADs.
- Automatically marks provisioning complete (`COMPLETE.json`) once Compute API confirms instance reaches or exceeds target shape (`RESIZE_TARGET_OCPUS` / `RESIZE_TARGET_MEMORY_GB`).
- Adds strictly read-only `sudo oci-a1-launcherctl resize-plan` (and `launcher.py resize-plan`) diagnostic command showing target instance, current vs target shape, resize stack, active jobs, and next cycle action without making OCI mutations.
- Fails safe if required `RESIZE_*` variables are missing or use placeholder values when `RESIZE_ONLY` mode is enabled.
- Updates `upgrade.sh` to append `PROVISIONING_MODE` and `RESIZE_*` settings and validate configuration.
- Adds comprehensive 12-case regression unit test suite in `tests/test_launcher.py`.

## 1.2.2

- Restores the `print_status` function in `src/launcher.py` required by `sudo oci-a1-launcherctl status`.
- Fixes `NameError: name 'print_status' is not defined` when executing `launcherctl status`.
- Preserves exact status output reporting current `state.json`, `Paused` status, `Complete` status, and `Throttled` cooldown status.
- Adds CLI dispatch audit and regression unit tests for `status` command execution and subparser targets in `tests/test_launcher.py`.
- No changes to provisioning behavior, stack selection, rotation, or OCI Resource Manager submission logic.

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
