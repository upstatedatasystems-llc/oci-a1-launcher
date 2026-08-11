# OCI Always Free A1 Stack Launcher

Automated, resilient provisioning for Oracle Cloud Infrastructure (OCI) Always Free Ampere A1 Compute instances (up to 4 OCPUs and 24 GB RAM) using pre-built OCI Resource Manager stacks, instance-principal authentication, and systemd timers on an Ubuntu control server.

---

## Table of Contents

- [Overview & Purpose](#overview--purpose)
- [OCI Prerequisites](#oci-prerequisites)
- [OCI IAM Dynamic Group & Policy Setup](#oci-iam-dynamic-group--policy-setup)
- [Expected Resource Manager Stacks](#expected-resource-manager-stacks)
- [Additional Small-Stack Configuration (v1.2.0+)](#additional-small-stack-configuration-v120)
- [Installation on Ubuntu Control Instance](#installation-on-ubuntu-control-instance)
- [Configuration Reference](#configuration-reference)
- [Systemd Integration & Provisioning Schedule](#systemd-integration--provisioning-schedule)
  - [40-Minute Provisioning Cycle](#40-minute-provisioning-cycle)
  - [Delay Between Provisioning Attempts](#delay-between-provisioning-attempts)
  - [Throttling & 429 Cooldown Behavior](#throttling--429-cooldown-behavior)
  - [Daily 7:50 AM Email Report](#daily-750-am-email-report)
- [Dry-Run Mode](#dry-run-mode)
- [Administration & Candidate Inspection Commands](#administration--candidate-inspection-commands)
- [Upgrade Procedure](#upgrade-procedure)
- [Disabling and Uninstalling](#disabling-and-uninstalling)
- [Security Considerations](#security-considerations)
- [License](#license)

---

## Overview & Purpose

OCI Always Free Ampere A1 instances are frequently subject to "Out of Host Capacity" errors when requested manually or in high-demand availability domains. This package automates retries from a lightweight control server (such as an Always Free AMD E2.1.Micro instance running Ubuntu).

Key features:
- **Instance Principal Authentication**: No static OCI user API keys or private keys are stored on disk. Authentication is bound directly to the control server's instance OCID via OCI IAM Dynamic Groups.
- **Resilient Stack Rotation**: Continuously rotates across Availability Domains attempting both 2 OCPU / 12 GB and 1 OCPU / 6 GB configurations until the maximum 4 OCPU / 24 GB allocation is full.
- **Same-AD Multi-Stack Support**: Supports multiple distinct 1 OCPU / 6 GB stacks per Availability Domain without reapplying previously succeeded Terraform-managed stacks.
- **Throttling Protection**: Handles OCI HTTP 429 (`TooManyRequests`) rate limits gracefully by entering a 1-hour no-API cooldown period.
- **Daily Status Reports**: Delivers a comprehensive email report every morning at 7:50 AM America/New_York detailing all provisioning attempts, errors, and inventory changes.
- **Automatic Completion**: Automatically halts all provisioning API calls once the target A1 allocation is satisfied.

---

## OCI Prerequisites

1. **OCI Tenancy**: An active Oracle Cloud Infrastructure account.
2. **Ubuntu Control Server**: An active E2.1.Micro instance (e.g., `purgatory01-vm`) running 24/7 on Ubuntu 20.04/22.04/24.04 LTS.
3. **Pre-configured Resource Manager Stacks**: Terraform stacks created in OCI Resource Manager corresponding to the Availability Domains and shape sizes (see [Expected Resource Manager Stacks](#expected-resource-manager-stacks)).
4. **Gmail SMTP Access**: A Gmail account (or alternative SMTP provider) with 2-Step Verification and a dedicated 16-character **App Password** for sending automated email updates.

---

## OCI IAM Dynamic Group & Policy Setup

All operations rely on **Instance Principals**. The control instance must be authorized via an OCI Dynamic Group and IAM Policy.

### 1. Create a Dynamic Group

In the OCI Console under **Identity & Security** > **Domains** > **Default** > **Dynamic Groups**:
- **Name**: `OCI_A1_Launcher_DG`
- **Matching Rule**:
  ```text
  ALL {instance.id = 'ocid1.instance.oc1.iad.your_control_instance_ocid'}
  ```
*(Replace `your_control_instance_ocid` with the actual OCID of your control VM).*

### 2. Create an IAM Policy

In the OCI Console under **Identity & Security** > **Policies** (created in the root compartment):
- **Name**: `OCI-A1-Launcher-Policy`
- **Statements**:
  ```text
  Allow dynamic-group OCI_A1_Launcher_DG to use orm-stacks in tenancy
  Allow dynamic-group OCI_A1_Launcher_DG to read orm-jobs in tenancy
  Allow dynamic-group OCI_A1_Launcher_DG to manage orm-jobs in tenancy where target.job.operation = 'APPLY'
  Allow dynamic-group OCI_A1_Launcher_DG to manage instance-family in tenancy
  Allow dynamic-group OCI_A1_Launcher_DG to use virtual-network-family in tenancy
  Allow dynamic-group OCI_A1_Launcher_DG to manage volume-family in tenancy
  ```

> [!NOTE]
> IAM policies intentionally grant permission only for `APPLY` Resource Manager jobs. `DESTROY` operations are not permitted.

---

## Expected Resource Manager Stacks

The launcher expects primary OCI Resource Manager Terraform stacks whose OCIDs are configured in `/etc/oci-a1-launcher/launcher.env`:

| Stack Name | AD | OCPUs | Memory (GB) | Environment Variable |
| :--- | :---: | :---: | :---: | :--- |
| `purgatory02-ad1` | 1 | 2 | 12 | `STACK_OCID_AD1` |
| `purgatory02-ad1e` | 1 | 1 | 6 | `STACK_OCID_AD1E` |
| `purgatory02-ad2` | 2 | 2 | 12 | `STACK_OCID_AD2` |
| `purgatory02-ad2e` | 2 | 1 | 6 | `STACK_OCID_AD2E` |
| `purgatory02-ad3` | 3 | 2 | 12 | `STACK_OCID_AD3` |
| `purgatory02-ad3e` | 3 | 1 | 6 | `STACK_OCID_AD3E` |

---

## Additional Small-Stack Configuration (v1.2.0+)

If a 1 OCPU / 6 GB stack in a specific Availability Domain has already successfully provisioned an instance (e.g. `purgatory02-ad3e` in AD-3), that exact stack OCID is marked consumed and will not be re-applied.

To allow future 1/6 provisioning attempts in that same AD, create an additional Resource Manager stack in OCI (e.g., `purgatory03-ad3e`) and configure it via `EXTRA_SMALL_STACKS_JSON`:

```bash
EXTRA_SMALL_STACKS_JSON='[
  {
    "name": "purgatory03-ad3e",
    "ocid": "ocid1.ormstack.oc1.iad.examplepurgatory03ad3e",
    "ad": 3
  }
]'
```

The launcher will evaluate both standard and extra small stacks, excluding consumed stack OCIDs while keeping all eligible unused 1/6 stacks available in AD rotation.

---

## Installation on Ubuntu Control Instance

### Primary Path: Clone from GitHub

1. Connect to your control server via SSH:
   ```bash
   ssh -i /path/to/ssh-key ubuntu@CONTROL_SERVER_IP
   ```

2. Clone the repository and run the installer:
   ```bash
   git clone https://github.com/upstatedatasystems-llc/oci-a1-launcher.git
   cd oci-a1-launcher
   sudo bash install.sh
   ```

### Optional Alternative: Download Release Archive

Alternatively, download and extract a release archive from GitHub:
```bash
wget https://github.com/upstatedatasystems-llc/oci-a1-launcher/archive/refs/tags/v1.2.0.tar.gz
tar -xzf v1.2.0.tar.gz
cd oci-a1-launcher-1.2.0
sudo bash install.sh
```

### Post-Installation Configuration

1. **Configure Environment Variables**:
   ```bash
   sudo nano /etc/oci-a1-launcher/launcher.env
   sudo chmod 0600 /etc/oci-a1-launcher/launcher.env
   ```
   Populate `COMPARTMENT_OCID`, `CONTROL_INSTANCE_OCID`, `SMTP_USER`, `SMTP_APP_PASSWORD`, and the six `STACK_OCID_*` variables.

2. **Validate Access & Candidate Plan**:
   ```bash
   sudo oci-a1-launcherctl doctor
   sudo oci-a1-launcherctl candidates
   sudo oci-a1-launcherctl test-email
   ```

3. **Enable Systemd Timers**:
   ```bash
   sudo oci-a1-launcherctl start
   ```

---

## Configuration Reference

The configuration file resides at `/etc/oci-a1-launcher/launcher.env`. Below is a reference of all supported variables:

| Variable | Default / Required | Description |
| :--- | :--- | :--- |
| `OCI_REGION` | `us-ashburn-1` | OCI Region containing your compartment and stacks |
| `COMPARTMENT_OCID` | *(Required)* | OCID of the target compartment |
| `CONTROL_INSTANCE_OCID` | *(Required)* | OCID of the control VM running this automation |
| `CONTROL_INSTANCE_NAME` | `purgatory01-vm` | Human-readable name of the control server |
| `STACK_OCID_AD1` | *(Required)* | Resource Manager Stack OCID for AD-1 (2 OCPU / 12 GB) |
| `STACK_OCID_AD1E` | *(Required)* | Resource Manager Stack OCID for AD-1 (1 OCPU / 6 GB) |
| `STACK_OCID_AD2` | *(Required)* | Resource Manager Stack OCID for AD-2 (2 OCPU / 12 GB) |
| `STACK_OCID_AD2E` | *(Required)* | Resource Manager Stack OCID for AD-2 (1 OCPU / 6 GB) |
| `STACK_OCID_AD3` | *(Required)* | Resource Manager Stack OCID for AD-3 (2 OCPU / 12 GB) |
| `STACK_OCID_AD3E` | *(Required)* | Resource Manager Stack OCID for AD-3 (1 OCPU / 6 GB) |
| `STACK_OCIDS` | *(Optional)* | JSON string mapping stack names to OCIDs (alternative to individual env vars) |
| `EXTRA_SMALL_STACKS_JSON` | `[]` | Optional JSON array defining extra 1 OCPU / 6 GB stacks |
| `LOCAL_TIMEZONE` | `America/New_York` | Timezone for reports and log entries |
| `DATA_DIR` | `/var/lib/oci-a1-launcher` | Directory holding runtime state and logs |
| `SMTP_HOST` | `smtp.gmail.com` | SMTP server host |
| `SMTP_PORT` | `465` | SMTP SSL port |
| `SMTP_USER` | *(Required)* | SMTP username / email address |
| `SMTP_FROM` | *(Required)* | From header email address |
| `SMTP_TO` | *(Required)* | Recipient email address(es), comma-separated |
| `SMTP_APP_PASSWORD` | *(Required)* | 16-character App Password for SMTP authentication |
| `JOB_TIMEOUT_SECONDS` | `1200` | Timeout (seconds) waiting for an RM job to finish |
| `POLL_INTERVAL_SECONDS` | `20` | Interval (seconds) between RM job status checks |
| `SECOND_JOB_DELAY_SECONDS` | `600` | Delay (seconds) before trying 1/6 stack after 2/12 capacity failure |
| `INVENTORY_SETTLE_SECONDS` | `120` | Max wait time (seconds) for new instance to appear in Compute list |
| `THROTTLE_COOLDOWN_SECONDS` | `3600` | Cooldown period (seconds) after receiving OCI HTTP 429 |
| `MAX_ERROR_LINES_PER_JOB` | `20` | Max error lines included per job in email reports |
| `DRY_RUN` | `true` | When `true`, scans inventory without creating RM jobs |

---

## Systemd Integration & Provisioning Schedule

The automation is driven by two systemd timers installed in `/etc/systemd/system/`:

### 40-Minute Provisioning Cycle
- **Timer**: `oci-a1-launcher.timer` (`OnBootSec=5min`, `OnUnitActiveSec=40min`)
- **Service**: `oci-a1-launcher.service` (`Type=oneshot`, `TimeoutStartSec=30min`)
- **Behavior**: Every 40 minutes, the launcher acquires an exclusive file lock (`launcher.lock`), verifies control instance health, checks for active RM jobs, evaluates A1 instance inventory, and executes stack provisioning if capacity is needed.

### Delay Between Provisioning Attempts
- When zero A1 instances exist, the launcher first attempts the 2 OCPU / 12 GB stack for the current AD in rotation.
- If the 2/12 stack fails specifically due to **Out of Host Capacity**, the launcher waits until at least **600 seconds (10 minutes)** have elapsed since the first job submission before attempting the secondary 1 OCPU / 6 GB stack in the same cycle.

### Throttling & 429 Cooldown Behavior
- If OCI APIs return an HTTP 429 (`TooManyRequests`) error, the launcher creates `/var/lib/oci-a1-launcher/THROTTLED.json` with a 1-hour expiration.
- The service exits cleanly (`status=0`) to avoid systemd failure alerts.
- Subsequent 40-minute timer runs exit immediately without calling OCI APIs until the 1-hour cooldown expires.

### Daily 7:50 AM Email Report
- **Timer**: `oci-a1-daily-report.timer` (`OnCalendar=*-*-* 07:50:00 America/New_York`)
- **Service**: `oci-a1-daily-report.service`
- **Behavior**: Reads the chronological log (`events.jsonl`) for the previous local calendar day, compiles job status, capacity errors, throttle events, and inventory changes, and emails the summary report.

---

## Dry-Run Mode

When `DRY_RUN=true` is set in `/etc/oci-a1-launcher/launcher.env`:
- The launcher runs all inventory checks, active job checks, and AD rotation logic.
- It logs a `dry_run` event with `DRY_RUN_NOT_SUBMITTED` status.
- It **does not create** any Resource Manager jobs.

Always verify installation in dry-run mode using:
```bash
sudo oci-a1-launcherctl run
sudo tail -n 30 /var/lib/oci-a1-launcher/events.jsonl
```

Set `DRY_RUN=false` when ready to enable live provisioning attempts.

---

## Administration & Candidate Inspection Commands

Use `oci-a1-launcherctl` for standard administrative and diagnostic tasks:

```bash
# Check OCI IAM permissions, compute access, and stack accessibility
sudo oci-a1-launcherctl doctor

# Inspect candidate stack selection, AD rotation, and consumed stacks (Read-Only)
sudo oci-a1-launcherctl candidates
sudo oci-a1-launcherctl plan

# Send a test email to verify SMTP configuration
sudo oci-a1-launcherctl test-email

# Execute one manual launcher run cycle
sudo oci-a1-launcherctl run

# View current launcher state, pause status, completion marker, and throttle status
sudo oci-a1-launcherctl status

# Run the daily report manually
sudo oci-a1-launcherctl daily-report

# Temporarily pause provisioning (daily reports remain active)
sudo oci-a1-launcherctl pause

# Resume paused provisioning
sudo oci-a1-launcherctl resume

# Pause provisioning and disable the launcher timer
sudo oci-a1-launcherctl stop

# Enable both launcher and daily-report systemd timers
sudo oci-a1-launcherctl start
```

---

## Upgrade Procedure

To upgrade an existing deployment using `upgrade.sh`:

1. Pause the running launcher:
   ```bash
   sudo oci-a1-launcherctl pause
   ```
2. Fetch the latest code (`git pull` or extract new release archive) and run:
   ```bash
   sudo bash upgrade.sh
   ```
   `upgrade.sh` safely creates a root-only backup under `/var/backups/oci-a1-launcher`, updates binaries and systemd definitions, preserves existing credentials in `/etc/oci-a1-launcher/launcher.env`, and appends placeholders for any newly required variables (`STACK_OCID_AD1` through `STACK_OCID_AD3E` and `EXTRA_SMALL_STACKS_JSON='[]'`).

3. **Inspect Candidate Plan**:
   ```bash
   sudo oci-a1-launcherctl candidates
   ```

4. **Validate and Resume**:
   ```bash
   sudo oci-a1-launcherctl doctor
   sudo oci-a1-launcherctl resume
   ```

---

## Disabling and Uninstalling

### Pause Provisioning Temporarily
```bash
sudo oci-a1-launcherctl pause
```

### Disable Timers (Keep Installed Files)
```bash
sudo oci-a1-launcherctl stop
sudo systemctl disable oci-a1-daily-report.timer
```

### Complete Uninstallation
```bash
# Stop and disable systemd services and timers
sudo systemctl disable --now oci-a1-launcher.timer oci-a1-daily-report.timer
sudo rm -f /etc/systemd/system/oci-a1-launcher.service /etc/systemd/system/oci-a1-launcher.timer
sudo rm -f /etc/systemd/system/oci-a1-daily-report.service /etc/systemd/system/oci-a1-daily-report.timer
sudo systemctl daemon-reload

# Remove package binaries, configuration, CLI helper, and data
sudo rm -rf /opt/oci-a1-launcher
sudo rm -rf /etc/oci-a1-launcher
sudo rm -rf /var/lib/oci-a1-launcher
sudo rm -f /usr/local/sbin/oci-a1-launcherctl
```

---

## Security Considerations

1. **No Disk Credentials**: OCI authentication uses **Instance Principals**. No tenancy API keys or private keys are written to disk.
2. **File Permissions**: The environment file `/etc/oci-a1-launcher/launcher.env` contains the SMTP password and is restricted to `0600` (root read/write only). The runtime data directory `/var/lib/oci-a1-launcher` is restricted to `0700`.
3. **IAM Scoping**: IAM policies permit only `APPLY` Resource Manager operations. No `DESTROY` or deletion operations are permitted to the dynamic group.
4. **Google App Passwords**: SMTP integration uses dedicated 16-character Google App Passwords rather than primary Google account credentials.

---

## License

This project is licensed under the [MIT License](LICENSE).
Copyright (c) 2026 Upstate Data Systems LLC.
