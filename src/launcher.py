#!/usr/bin/env python3
"""Try prebuilt OCI Resource Manager stacks until the Always Free A1 allocation is full."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

try:
    import oci
except ImportError:
    oci = None  # type: ignore[assignment]

from common import (
    ACTIVE_JOB_STATES,
    COMPLETE_FILE,
    NON_TERMINATED_INSTANCE_STATES,
    PAUSE_FILE,
    REGION,
    STACKS,
    STACK_BY_OCID,
    THROTTLE_FILE,
    StackSpec,
    all_results,
    append_event,
    classify_a1_instances,
    create_oci_clients,
    exclusive_lock,
    get_env_required,
    get_resize_config,
    get_stack_for_ocid,
    infer_ad_number,
    instance_summary,
    load_state,
    local_timestamp,
    mark_complete,
    now_utc,
    save_state,
    send_email,
    serialize_instance,
    stack_as_dict,
    utc_iso,
)

SERVICE_ERROR_TYPE = (oci.exceptions.ServiceError,) if oci is not None and hasattr(oci, "exceptions") else ()

COMPARTMENT_OCID = os.getenv("COMPARTMENT_OCID", "").strip()
CONTROL_INSTANCE_OCID = os.getenv("CONTROL_INSTANCE_OCID", "").strip()
CONTROL_INSTANCE_NAME = os.getenv("CONTROL_INSTANCE_NAME", "purgatory01-vm").strip()
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "1200"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "20"))
SECOND_JOB_DELAY_SECONDS = int(os.getenv("SECOND_JOB_DELAY_SECONDS", "600"))
THROTTLE_COOLDOWN_SECONDS = int(os.getenv("THROTTLE_COOLDOWN_SECONDS", "3600"))
INVENTORY_SETTLE_SECONDS = int(os.getenv("INVENTORY_SETTLE_SECONDS", "120"))
MAX_ERROR_LINES = int(os.getenv("MAX_ERROR_LINES_PER_JOB", "20"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}


def get_control_instance(compute: oci.core.ComputeClient) -> dict[str, Any]:
    response = compute.get_instance(CONTROL_INSTANCE_OCID)
    return serialize_instance(response.data)


def list_a1_instances(compute: oci.core.ComputeClient) -> list[dict[str, Any]]:
    instances = all_results(compute.list_instances, compartment_id=COMPARTMENT_OCID)
    return [
        serialize_instance(instance)
        for instance in instances
        if getattr(instance, "shape", None) == "VM.Standard.A1.Flex"
        and getattr(instance, "lifecycle_state", None) in NON_TERMINATED_INSTANCE_STATES
    ]


def active_resource_manager_jobs(resource_manager: oci.resource_manager.ResourceManagerClient) -> list[dict[str, Any]]:
    """Find active jobs for managed stacks with one compartment-wide API call."""
    managed_stack_ids = set(STACK_BY_OCID)
    resize_cfg = get_resize_config()
    if resize_cfg["stack_ocid"]:
        managed_stack_ids.add(resize_cfg["stack_ocid"])

    kwargs: dict[str, Any] = {
        "compartment_id": COMPARTMENT_OCID,
        "sort_by": "TIMECREATED",
        "sort_order": "DESC",
        "limit": 100,
    }
    if oci is not None and hasattr(oci, "retry"):
        kwargs["retry_strategy"] = oci.retry.NoneRetryStrategy()

    jobs = resource_manager.list_jobs(**kwargs).data
    active: list[dict[str, Any]] = []
    for job in jobs:
        stack_id = getattr(job, "stack_id", None)
        state = getattr(job, "lifecycle_state", None)
        if stack_id not in managed_stack_ids or state not in ACTIVE_JOB_STATES:
            continue
        stack = get_stack_for_ocid(stack_id)
        active.append(
            {
                "stack_name": stack.name,
                "stack_ocid": stack.ocid,
                "job_id": getattr(job, "id", None),
                "state": state,
                "time_created": (
                    getattr(job, "time_created", None).isoformat()
                    if getattr(job, "time_created", None)
                    else None
                ),
            }
        )
    return active


def load_throttle_cooldown() -> dict[str, Any] | None:
    if not THROTTLE_FILE.exists():
        return None
    try:
        payload = json.loads(THROTTLE_FILE.read_text(encoding="utf-8"))
        until = datetime.fromisoformat(str(payload["until"]).replace("Z", "+00:00"))
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        THROTTLE_FILE.unlink(missing_ok=True)
        return None
    if until <= now_utc():
        THROTTLE_FILE.unlink(missing_ok=True)
        return None
    payload["until_datetime"] = until
    return payload


def set_throttle_cooldown(exc: oci.exceptions.ServiceError, run_id: str) -> dict[str, Any]:
    until = now_utc() + timedelta(seconds=THROTTLE_COOLDOWN_SECONDS)
    payload = {
        "created_at": utc_iso(),
        "until": utc_iso(until),
        "cooldown_seconds": THROTTLE_COOLDOWN_SECONDS,
        "status": exc.status,
        "code": exc.code,
        "message": exc.message,
        "opc_request_id": exc.request_id,
    }
    THROTTLE_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(THROTTLE_FILE, 0o600)
    append_event(
        {
            "event_type": "throttle",
            "run_id": run_id,
            "stage": "oci_service",
            **payload,
            "result": "THROTTLED",
        }
    )
    return payload


def get_error_lines(
    resource_manager: oci.resource_manager.ResourceManagerClient, job_id: str
) -> list[str]:
    last_error: Exception | None = None
    for attempt in range(1, 5):
        try:
            entries = all_results(
                resource_manager.get_job_logs,
                job_id,
                level_greater_than_or_equal_to="ERROR",
                sort_order="ASC",
            )
            lines: list[str] = []
            for entry in entries[:MAX_ERROR_LINES]:
                timestamp = getattr(entry, "timestamp", None)
                ts = timestamp.isoformat() if timestamp else ""
                level = getattr(entry, "level", "ERROR") or "ERROR"
                message = str(getattr(entry, "message", "") or "").strip()
                lines.append(f"{ts} {level} - {message}".strip())
            if len(entries) > MAX_ERROR_LINES:
                lines.append(f"... {len(entries) - MAX_ERROR_LINES} additional ERROR/FATAL log lines omitted")
            return lines
        except oci.exceptions.ServiceError as exc:
            if exc.status == 429 or str(exc.code).lower() == "toomanyrequests":
                raise
            last_error = exc
            if attempt < 4:
                time.sleep(attempt * 2)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < 4:
                time.sleep(attempt * 2)
    return [f"ERROR - Unable to retrieve Resource Manager job logs: {last_error}"]


def failure_detail_text(job: Any) -> str:
    details = getattr(job, "failure_details", None)
    if details is None:
        return ""
    pieces = []
    for field in ("code", "message"):
        value = getattr(details, field, None)
        if value:
            pieces.append(str(value))
    if pieces:
        return " | ".join(pieces)
    return str(details)


def is_capacity_error(error_lines: Sequence[str], job: Any) -> bool:
    text = "\n".join(error_lines) + "\n" + failure_detail_text(job)
    normalized = text.lower().replace("_", " ").replace("-", " ")
    patterns = (
        "out of host capacity",
        "outofhostcapacity",
        "out of capacity",
        "insufficient host capacity",
    )
    return any(pattern in normalized for pattern in patterns)


def create_apply_job(
    resource_manager: oci.resource_manager.ResourceManagerClient,
    stack: StackSpec,
    run_id: str,
) -> Any:
    display_name = f"a1-auto-{stack.name}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    if oci is not None and hasattr(oci, "resource_manager"):
        details = oci.resource_manager.models.CreateJobDetails(
            stack_id=stack.ocid,
            display_name=display_name,
            operation="APPLY",
            job_operation_details=oci.resource_manager.models.CreateApplyJobOperationDetails(
                operation="APPLY",
                execution_plan_strategy="AUTO_APPROVED",
                is_provider_upgrade_required=False,
            ),
            freeform_tags={"a1-launcher": "true", "run-id": run_id[:32]},
        )
    else:
        from types import SimpleNamespace

        details = SimpleNamespace(
            stack_id=stack.ocid,
            display_name=display_name,
            operation="APPLY",
        )

    return resource_manager.create_job(create_job_details=details).data


def wait_for_job(
    resource_manager: oci.resource_manager.ResourceManagerClient,
    job_id: str,
) -> tuple[Any, bool]:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    latest = resource_manager.get_job(job_id).data
    while getattr(latest, "lifecycle_state", None) not in {"SUCCEEDED", "FAILED", "CANCELED"}:
        if time.monotonic() >= deadline:
            return latest, True
        time.sleep(POLL_INTERVAL_SECONDS)
        latest = resource_manager.get_job(job_id).data
    # OCI notes that log content can trail terminal job state briefly.
    time.sleep(2)
    return latest, False


def wait_for_inventory_change(
    compute: oci.core.ComputeClient, before_ids: set[str]
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + INVENTORY_SETTLE_SECONDS
    latest = list_a1_instances(compute)
    while not ({item["id"] for item in latest} - before_ids) and time.monotonic() < deadline:
        time.sleep(10)
        latest = list_a1_instances(compute)
    return latest


def record_job_event(
    *,
    run_id: str,
    stack: StackSpec,
    job: Any,
    timed_out: bool,
    errors: list[str],
    capacity_error: bool,
    before_instances: Sequence[dict[str, Any]],
    after_instances: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    before_ids = {item["id"] for item in before_instances}
    new_instances = [item for item in after_instances if item["id"] not in before_ids]
    state = getattr(job, "lifecycle_state", "UNKNOWN")
    actual_creation = bool(new_instances)
    result = "CREATED" if actual_creation else state
    event = {
        "event_type": "job",
        "run_id": run_id,
        "stack": stack_as_dict(stack),
        "job_id": getattr(job, "id", None),
        "job_display_name": getattr(job, "display_name", None),
        "job_state": state,
        "timed_out": timed_out,
        "capacity_error": capacity_error,
        "failure_details": failure_detail_text(job),
        "error_lines": errors,
        "job_time_created": (
            getattr(job, "time_created", None).isoformat()
            if getattr(job, "time_created", None)
            else None
        ),
        "job_time_finished": (
            getattr(job, "time_finished", None).isoformat()
            if getattr(job, "time_finished", None)
            else None
        ),
        "before_a1_instances": list(before_instances),
        "after_a1_instances": list(after_instances),
        "new_instances": new_instances,
        "result": result,
    }
    append_event(event)
    return event


def success_email(event: dict[str, Any], classification: dict[str, Any]) -> None:
    stack = event["stack"]
    timestamp = local_timestamp()
    subject = f"OCI A1 Launcher - {timestamp} - SUCCESS"
    errors = event.get("error_lines") or ["No ERROR-level log entries."]
    new_instances = event.get("new_instances", [])
    lines = [
        f"{timestamp} | {CONTROL_INSTANCE_NAME} | hostname={socket.gethostname()}",
        "",
        f"[{stack['name']} | {event.get('job_id')} ]",
        *errors,
        "",
    ]
    for instance in new_instances:
        lines.append(f"CREATED: {instance_summary(instance)}")
    lines.extend(
        [
            "",
            f"RESULT: SUCCEEDED - Resource Manager state={event.get('job_state')}; "
            f"A1 inventory now has {len(classification['small'])} x 1/6 and "
            f"{len(classification['large'])} x 2/12.",
        ]
    )
    try:
        send_email(subject, "\n".join(lines))
        append_event(
            {
                "event_type": "email",
                "run_id": event.get("run_id"),
                "email_kind": "immediate_success",
                "subject": subject,
                "result": "SENT",
            }
        )
    except Exception as exc:  # noqa: BLE001
        append_event(
            {
                "event_type": "system_error",
                "run_id": event.get("run_id"),
                "stage": "success_email",
                "message": str(exc),
            }
        )


def update_success_state(state: dict[str, Any], stack: StackSpec, event: dict[str, Any]) -> None:
    successful_stacks = set(state.get("successful_stack_ocids", []))
    successful_stacks.add(stack.ocid)
    state["successful_stack_ocids"] = sorted(successful_stacks)
    instance_ids = set(state.get("successful_instance_ids", []))
    instance_ids.update(instance["id"] for instance in event.get("new_instances", []))
    state["successful_instance_ids"] = sorted(instance_ids)
    save_state(state)


def attempt_stack(
    compute: oci.core.ComputeClient,
    resource_manager: oci.resource_manager.ResourceManagerClient,
    stack: StackSpec,
    state: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    before = list_a1_instances(compute)
    started_monotonic = time.monotonic()

    if DRY_RUN:
        event = {
            "event_type": "dry_run",
            "run_id": run_id,
            "stack": stack_as_dict(stack),
            "before_a1_instances": before,
            "result": "DRY_RUN_NOT_SUBMITTED",
        }
        append_event(event)
        return {
            "event": event,
            "capacity_error": False,
            "actual_creation": False,
            "classification": classify_a1_instances(before),
            "started_monotonic": started_monotonic,
            "timed_out": False,
        }

    submitted = create_apply_job(resource_manager, stack, run_id)
    job, timed_out = wait_for_job(resource_manager, submitted.id)
    errors = get_error_lines(resource_manager, submitted.id)
    capacity_error = is_capacity_error(errors, job)
    after = wait_for_inventory_change(compute, {item["id"] for item in before})
    event = record_job_event(
        run_id=run_id,
        stack=stack,
        job=job,
        timed_out=timed_out,
        errors=errors,
        capacity_error=capacity_error,
        before_instances=before,
        after_instances=after,
    )
    classification = classify_a1_instances(after)
    actual_creation = bool(event.get("new_instances"))
    if actual_creation:
        update_success_state(state, stack, event)
        success_email(event, classification)
    return {
        "event": event,
        "capacity_error": capacity_error,
        "actual_creation": actual_creation,
        "classification": classification,
        "started_monotonic": started_monotonic,
        "timed_out": timed_out,
    }


def advance_rotation(state: dict[str, Any]) -> None:
    state["next_ad_index"] = (int(state.get("next_ad_index", 0)) + 1) % 3
    save_state(state)


def pair_for_rotation(state: dict[str, Any]) -> tuple[StackSpec, StackSpec]:
    ad = int(state.get("next_ad_index", 0)) + 1
    large = next(stack for stack in STACKS if stack.ad == ad and stack.ocpus == 2)
    small = next(stack for stack in STACKS if stack.ad == ad and stack.ocpus == 1)
    return large, small


def choose_small_stack(
    state: dict[str, Any],
    existing_small: Sequence[dict[str, Any]],
    preferred_ad: int | None = None,
) -> StackSpec:
    used_stack_ocids = set(state.get("successful_stack_ocids", []))
    rotation = [((int(state.get("next_ad_index", 0)) + offset) % 3) + 1 for offset in range(3)]
    if preferred_ad in (1, 2, 3):
        rotation = [preferred_ad] + [ad for ad in rotation if ad != preferred_ad]

    candidates = [
        stack for stack in STACKS
        if stack.ocpus == 1
        and stack.ocid
        and stack.ocid not in used_stack_ocids
        and "example" not in stack.ocid.lower()
        and "replace" not in stack.ocid.lower()
    ]

    for ad in rotation:
        for stack in candidates:
            if stack.ad == ad:
                return stack

    raise RuntimeError("No eligible 1 OCPU / 6 GB stack remains")


def validate_inventory(classification: dict[str, Any]) -> str | None:
    if classification["unexpected"]:
        return "Unexpected A1 shape configuration detected; refusing to apply another stack."
    if len(classification["large"]) > 1:
        return "More than one 2/12 A1 instance detected; refusing to continue."
    if classification["large"] and classification["small"]:
        return "Both a 2/12 and a 1/6 A1 instance are present; free allocation appears exceeded."
    if len(classification["small"]) > 2:
        return "More than two 1/6 A1 instances detected; refusing to continue."
    return None


def run_resize_only_cycle(
    compute: oci.core.ComputeClient,
    resource_manager: oci.resource_manager.ResourceManagerClient,
    state: dict[str, Any],
    run_id: str,
) -> int:
    """Execute one launcher cycle in RESIZE_ONLY mode.

    ABSOLUTE SAFETY GUARANTEE:
    This function ONLY references or attempts RESIZE_STACK_OCID.
    No other stack (AD1, AD2, etc.) can ever be created or applied.
    """
    resize_cfg = get_resize_config()
    if not resize_cfg["is_configured"]:
        append_event(
            {
                "event_type": "system_error",
                "run_id": run_id,
                "stage": "resize_configuration_validation",
                "message": (
                    "PROVISIONING_MODE=RESIZE_ONLY is set, but required RESIZE_INSTANCE_OCID or "
                    "RESIZE_STACK_OCID is missing or uses placeholder values. "
                    "Please edit /etc/oci-a1-launcher/launcher.env."
                ),
            }
        )
        return 1

    instance_ocid = resize_cfg["instance_ocid"]
    stack_ocid = resize_cfg["stack_ocid"]
    target_ocpus = resize_cfg["target_ocpus"]
    target_memory_gb = resize_cfg["target_memory_gb"]
    resize_stack = get_stack_for_ocid(stack_ocid)

    # 1. Fetch current target instance state from OCI Compute API
    try:
        instance_data = compute.get_instance(instance_ocid).data
        instance_dict = serialize_instance(instance_data)
    except Exception as exc:
        append_event(
            {
                "event_type": "system_error",
                "run_id": run_id,
                "stage": "resize_instance_lookup",
                "message": f"Failed to fetch target instance {instance_ocid}: {exc}",
            }
        )
        return 1

    current_ocpus = instance_dict["ocpus"]
    current_memory_gb = instance_dict["memory_gb"]

    # 2. Check if instance is already at or above target shape
    if current_ocpus >= target_ocpus and current_memory_gb >= target_memory_gb:
        classification = classify_a1_instances([instance_dict])
        mark_complete(
            classification,
            f"Resize target reached: {instance_dict.get('display_name')} is {current_ocpus:g} OCPU / {current_memory_gb:g} GB",
        )
        return 0

    # 3. Check for active RM jobs for RESIZE_STACK_OCID
    active_jobs = active_resource_manager_jobs(resource_manager)
    resize_active_jobs = [j for j in active_jobs if j["stack_ocid"] == stack_ocid]
    if resize_active_jobs:
        append_event(
            {
                "event_type": "run_skip",
                "run_id": run_id,
                "reason": "ACTIVE_RESOURCE_MANAGER_JOB",
                "active_jobs": resize_active_jobs,
            }
        )
        return 0

    # 4. Dry Run check
    if DRY_RUN:
        append_event(
            {
                "event_type": "dry_run",
                "run_id": run_id,
                "stage": "resize_only",
                "stack": stack_as_dict(resize_stack),
                "target_instance": instance_dict,
                "target_shape": {"ocpus": target_ocpus, "memory_gb": target_memory_gb},
                "result": "DRY_RUN_RESIZE_NOT_SUBMITTED",
            }
        )
        return 0

    # 5. Submit APPLY for RESIZE_STACK_OCID ONLY
    submitted = create_apply_job(resource_manager, resize_stack, run_id)
    job_id = getattr(submitted, "id", None) or str(submitted)
    job, timed_out = wait_for_job(resource_manager, job_id)
    errors = get_error_lines(resource_manager, job_id)
    capacity_error = is_capacity_error(errors, job)

    event = record_job_event(
        run_id=run_id,
        stack=resize_stack,
        job=job,
        timed_out=timed_out,
        errors=errors,
        capacity_error=capacity_error,
        before_instances=[instance_dict],
        after_instances=[instance_dict],
    )

    if capacity_error:
        # Logged by record_job_event; no fallback attempt is made.
        return 0
    if timed_out:
        return 0
    if getattr(job, "lifecycle_state", None) != "SUCCEEDED":
        return 1

    # 6. Re-query instance to verify resize completion
    try:
        refreshed_data = compute.get_instance(instance_ocid).data
        refreshed_dict = serialize_instance(refreshed_data)
        refreshed_classification = classify_a1_instances([refreshed_dict])

        if (
            refreshed_dict["ocpus"] >= target_ocpus
            and refreshed_dict["memory_gb"] >= target_memory_gb
        ):
            mark_complete(
                refreshed_classification,
                f"Resize succeeded: {refreshed_dict.get('display_name')} is now {refreshed_dict['ocpus']:g} OCPU / {refreshed_dict['memory_gb']:g} GB",
            )
            success_email(event, refreshed_classification)
    except Exception as exc:
        append_event(
            {
                "event_type": "system_error",
                "run_id": run_id,
                "stage": "resize_post_apply_verification",
                "message": f"Apply succeeded, but failed to verify instance state: {exc}",
            }
        )

    return 0


def run_once() -> int:
    run_id = str(uuid.uuid4())
    run_started = now_utc()
    with exclusive_lock(blocking=False) as acquired:
        if not acquired:
            return 0
        if PAUSE_FILE.exists():
            return 0
        if COMPLETE_FILE.exists():
            return 0

        cooldown = load_throttle_cooldown()
        if cooldown:
            until = cooldown["until_datetime"]
            append_event(
                {
                    "event_type": "run_skip",
                    "run_id": run_id,
                    "reason": "THROTTLE_COOLDOWN",
                    "cooldown_until": utc_iso(until),
                    "remaining_seconds": max(0, int((until - now_utc()).total_seconds())),
                }
            )
            return 0

        get_env_required("COMPARTMENT_OCID")
        get_env_required("CONTROL_INSTANCE_OCID")

        resize_cfg = get_resize_config()
        if resize_cfg["is_resize_only"]:
            state = load_state()
            append_event({"event_type": "run_start", "run_id": run_id, "mode": "RESIZE_ONLY", "dry_run": DRY_RUN})
            try:
                compute, resource_manager = create_oci_clients()
                control = get_control_instance(compute)
                if control["lifecycle_state"] not in NON_TERMINATED_INSTANCE_STATES:
                    raise RuntimeError(
                        f"Control instance {CONTROL_INSTANCE_NAME} is not active: {control['lifecycle_state']}"
                    )
                return run_resize_only_cycle(compute, resource_manager, state, run_id)
            except SERVICE_ERROR_TYPE as exc:
                if exc.status == 429:
                    set_throttle_cooldown(exc, run_id)
                    return 0
                append_event(
                    {
                        "event_type": "system_error",
                        "run_id": run_id,
                        "stage": "oci_service",
                        "status": exc.status,
                        "code": exc.code,
                        "message": exc.message,
                        "opc_request_id": exc.request_id,
                    }
                )
                return 1
            except Exception as exc:
                append_event(
                    {
                        "event_type": "system_error",
                        "run_id": run_id,
                        "stage": "unhandled_exception",
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                )
                return 1

        unconfigured = [
            stack.name
            for stack in STACKS
            if not stack.ocid
            or "example" in stack.ocid.lower()
            or "replace" in stack.ocid.lower()
        ]
        if unconfigured:
            append_event(
                {
                    "event_type": "system_error",
                    "run_id": run_id,
                    "stage": "stack_configuration_validation",
                    "message": f"Stack OCIDs are not configured for: {', '.join(unconfigured)}. "
                    "Edit /etc/oci-a1-launcher/launcher.env and set STACK_OCID_AD1 through STACK_OCID_AD3E.",
                }
            )
            return 1

        state = load_state()
        append_event({"event_type": "run_start", "run_id": run_id, "dry_run": DRY_RUN})
        try:
            compute, resource_manager = create_oci_clients()

            control = get_control_instance(compute)
            if control["lifecycle_state"] not in NON_TERMINATED_INSTANCE_STATES:
                raise RuntimeError(
                    f"Control instance {CONTROL_INSTANCE_NAME} is not active: {control['lifecycle_state']}"
                )

            active_jobs = active_resource_manager_jobs(resource_manager)
            if active_jobs:
                append_event(
                    {
                        "event_type": "run_skip",
                        "run_id": run_id,
                        "reason": "ACTIVE_RESOURCE_MANAGER_JOB",
                        "active_jobs": active_jobs,
                    }
                )
                return 0

            a1_instances = list_a1_instances(compute)
            classification = classify_a1_instances(a1_instances)
            inventory_error = validate_inventory(classification)
            if inventory_error:
                append_event(
                    {
                        "event_type": "system_error",
                        "run_id": run_id,
                        "stage": "inventory_validation",
                        "message": inventory_error,
                        "classification": classification,
                    }
                )
                return 1

            if classification["complete"]:
                mark_complete(classification, "Always Free A1 allocation is full")
                return 0

            # No A1 instances: try the matching 2/12 stack, then after at least
            # 600 seconds try the matching 1/6 stack only for a capacity failure.
            if not classification["small"] and not classification["large"]:
                large_stack, matching_small_stack = pair_for_rotation(state)
                first = attempt_stack(compute, resource_manager, large_stack, state, run_id)
                advance_rotation(state)

                if first["classification"]["complete"]:
                    mark_complete(first["classification"], f"Created {large_stack.name}")
                    return 0
                if first["actual_creation"]:
                    # A creation occurred, but inventory is not yet full. Let the
                    # next scheduled run reevaluate from a clean state.
                    return 0
                if first["timed_out"]:
                    return 0
                if not first["capacity_error"]:
                    # This run ends, but the timer remains enabled and retries on
                    # the next 40-minute schedule, as requested.
                    return 0

                remaining = SECOND_JOB_DELAY_SECONDS - (
                    time.monotonic() - first["started_monotonic"]
                )
                if remaining > 0:
                    time.sleep(remaining)

                # Required pre-second-job safety check.
                active_jobs = active_resource_manager_jobs(resource_manager)
                if active_jobs:
                    append_event(
                        {
                            "event_type": "run_skip",
                            "run_id": run_id,
                            "reason": "ACTIVE_JOB_BEFORE_SECOND_ATTEMPT",
                            "active_jobs": active_jobs,
                        }
                    )
                    return 0

                refreshed = list_a1_instances(compute)
                refreshed_classification = classify_a1_instances(refreshed)
                inventory_error = validate_inventory(refreshed_classification)
                if inventory_error:
                    append_event(
                        {
                            "event_type": "system_error",
                            "run_id": run_id,
                            "stage": "pre_second_inventory_validation",
                            "message": inventory_error,
                            "classification": refreshed_classification,
                        }
                    )
                    return 1
                if refreshed_classification["complete"]:
                    mark_complete(refreshed_classification, "A1 allocation became full before second attempt")
                    return 0
                if refreshed_classification["large"]:
                    mark_complete(refreshed_classification, "2/12 A1 instance detected before second attempt")
                    return 0

                if refreshed_classification["small"]:
                    second_stack = choose_small_stack(
                        state,
                        refreshed_classification["small"],
                        preferred_ad=matching_small_stack.ad,
                    )
                else:
                    second_stack = matching_small_stack

                second = attempt_stack(compute, resource_manager, second_stack, state, run_id)
                if second["classification"]["complete"]:
                    mark_complete(second["classification"], f"Created {second_stack.name}; A1 allocation full")
                return 0

            # Exactly one 1/6 instance: never try a 2/12 stack. Try one eligible
            # 1/6 stack, preferably in another AD.
            if len(classification["small"]) == 1 and not classification["large"]:
                small_stack = choose_small_stack(state, classification["small"])
                result = attempt_stack(compute, resource_manager, small_stack, state, run_id)
                advance_rotation(state)
                if result["classification"]["complete"]:
                    mark_complete(result["classification"], f"Created {small_stack.name}; A1 allocation full")
                return 0

            append_event(
                {
                    "event_type": "system_error",
                    "run_id": run_id,
                    "stage": "unhandled_inventory_state",
                    "classification": classification,
                }
            )
            return 1
        except SERVICE_ERROR_TYPE as exc:
            if exc.status == 429 or str(exc.code).lower() == "toomanyrequests":
                set_throttle_cooldown(exc, run_id)
                # Throttling is temporary and expected. Return success so systemd
                # records a clean one-shot run; the cooldown prevents more API calls.
                return 0
            append_event(
                {
                    "event_type": "system_error",
                    "run_id": run_id,
                    "stage": "oci_service",
                    "status": exc.status,
                    "code": exc.code,
                    "message": exc.message,
                    "opc_request_id": exc.request_id,
                }
            )
            return 1
        except Exception as exc:  # noqa: BLE001
            append_event(
                {
                    "event_type": "system_error",
                    "run_id": run_id,
                    "stage": "launcher",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            return 1
        finally:
            append_event(
                {
                    "event_type": "run_end",
                    "run_id": run_id,
                    "run_started": utc_iso(run_started),
                    "run_finished": utc_iso(),
                }
            )


def doctor(send_test: bool) -> int:
    resize_cfg = get_resize_config()
    if resize_cfg["is_resize_only"]:
        if not resize_cfg["is_configured"]:
            print("ERROR: PROVISIONING_MODE=RESIZE_ONLY is set, but RESIZE_INSTANCE_OCID or RESIZE_STACK_OCID is missing or uses placeholder values.")
            print("Please edit /etc/oci-a1-launcher/launcher.env and populate RESIZE_INSTANCE_OCID and RESIZE_STACK_OCID.")
            return 1

        print(f"Provisioning Mode: {resize_cfg['mode']}")
        print(f"Region: {REGION}")
        print(f"Compartment: {COMPARTMENT_OCID}")
        print(f"Control instance: {CONTROL_INSTANCE_NAME} ({CONTROL_INSTANCE_OCID})")
        print(f"Dry run: {DRY_RUN}")

        compute, resource_manager = create_oci_clients()
        control = get_control_instance(compute)
        print("Control instance API access: OK")
        print(json.dumps(control, indent=2))

        try:
            target_inst = compute.get_instance(resize_cfg["instance_ocid"]).data
            serialized_target = serialize_instance(target_inst)
            print(f"Resize target instance visible: OK ({instance_summary(serialized_target)})")
        except Exception as exc:
            print(f"ERROR: Target instance access failed ({resize_cfg['instance_ocid']}): {exc}")
            return 1

        try:
            fetched_stack = resource_manager.get_stack(resize_cfg["stack_ocid"]).data
            display_name = getattr(fetched_stack, "display_name", "resize-stack")
            print(f"Resize stack access OK: {display_name} ({resize_cfg['stack_ocid']})")
        except Exception as exc:
            print(f"ERROR: Resize stack access failed ({resize_cfg['stack_ocid']}): {exc}")
            return 1

        if send_test:
            timestamp = local_timestamp()
            send_email(
                f"OCI A1 Launcher - {timestamp} - TEST",
                f"{timestamp} | {CONTROL_INSTANCE_NAME} | hostname={socket.gethostname()}\n\n"
                "OCI instance-principal authentication, resize instance/stack access, and Gmail SMTP are working.\n\n"
                "RESULT: TEST SUCCEEDED",
            )
            print("Test email sent.")
        return 0

    unconfigured = [
        stack.name
        for stack in STACKS
        if not stack.ocid
        or "example" in stack.ocid.lower()
        or "replace" in stack.ocid.lower()
    ]
    if unconfigured:
        print(f"ERROR: Stack OCIDs are not configured for: {', '.join(unconfigured)}")
        print("Please edit /etc/oci-a1-launcher/launcher.env and set STACK_OCID_AD1 through STACK_OCID_AD3E.")
        return 1

    print(f"Region: {REGION}")
    print(f"Compartment: {COMPARTMENT_OCID}")
    print(f"Control instance: {CONTROL_INSTANCE_NAME} ({CONTROL_INSTANCE_OCID})")
    print(f"Dry run: {DRY_RUN}")
    compute, resource_manager = create_oci_clients()
    control = get_control_instance(compute)
    print("Control instance API access: OK")
    print(json.dumps(control, indent=2))
    a1_instances = list_a1_instances(compute)
    print(f"A1 instances visible: {len(a1_instances)}")
    for item in a1_instances:
        print(f"  - {instance_summary(item)}")
    for stack in STACKS:
        fetched = resource_manager.get_stack(stack.ocid).data
        print(f"Stack access OK: {getattr(fetched, 'display_name', stack.name)} ({stack.ocid})")
    if send_test:
        timestamp = local_timestamp()
        send_email(
            f"OCI A1 Launcher - {timestamp} - TEST",
            f"{timestamp} | {CONTROL_INSTANCE_NAME} | hostname={socket.gethostname()}\n\n"
            "OCI instance-principal authentication, stack access, compute inventory, and Gmail SMTP are working.\n\n"
            "RESULT: TEST SUCCEEDED",
        )
        print("Test email sent.")
    return 0


def get_candidate_plan() -> dict[str, Any]:
    """Inspect current state and inventory, returning candidate analysis without modifying anything."""
    state = load_state()
    used_stack_ocids = set(state.get("successful_stack_ocids", []))

    inventory_summary: dict[str, Any] = {"small": [], "large": [], "complete": False, "total": 0, "unexpected": []}
    control_instance_status = "UNKNOWN"
    active_jobs: list[dict[str, Any]] = []
    oci_error: str | None = None

    try:
        compute, resource_manager = create_oci_clients()
        try:
            control = get_control_instance(compute)
            control_instance_status = control.get("lifecycle_state", "UNKNOWN")
        except Exception as exc:
            control_instance_status = f"ERROR ({exc})"

        try:
            active_jobs = active_resource_manager_jobs(resource_manager)
        except Exception as exc:
            oci_error = f"Failed to list active RM jobs: {exc}"

        try:
            a1_instances = list_a1_instances(compute)
            inventory_summary = classify_a1_instances(a1_instances)
        except Exception as exc:
            if not oci_error:
                oci_error = f"Failed to list A1 instances: {exc}"
    except Exception as exc:
        oci_error = f"Failed to initialize OCI client: {exc}"

    stack_details: list[dict[str, Any]] = []
    eligible_large: list[dict[str, Any]] = []
    eligible_small: list[dict[str, Any]] = []

    rotation_index = int(state.get("next_ad_index", 0))
    rotation_ad = rotation_index + 1

    for stack in STACKS:
        is_used = stack.ocid in used_stack_ocids
        is_placeholder = not stack.ocid or "example" in stack.ocid.lower() or "replace" in stack.ocid.lower()

        reasons_excluded: list[str] = []
        if is_used:
            reasons_excluded.append("ALREADY_SUCCESSFULLY_PROVISIONED (stack OCID in successful_stack_ocids)")
        if is_placeholder:
            reasons_excluded.append("UNCONFIGURED_OR_PLACEHOLDER_OCID")

        is_eligible = len(reasons_excluded) == 0

        detail = {
            "name": stack.name,
            "ocid": stack.ocid,
            "ad": stack.ad,
            "ocpus": stack.ocpus,
            "memory_gb": stack.memory_gb,
            "is_used": is_used,
            "is_eligible": is_eligible,
            "reasons_excluded": reasons_excluded,
        }
        stack_details.append(detail)

        if is_eligible:
            if stack.ocpus == 2:
                eligible_large.append(detail)
            elif stack.ocpus == 1:
                eligible_small.append(detail)

    ad_rotation = [((rotation_index + offset) % 3) + 1 for offset in range(3)]

    next_large_candidate: dict[str, Any] | None = None
    next_small_candidate: dict[str, Any] | None = None

    for ad in ad_rotation:
        if not next_large_candidate:
            for s in eligible_large:
                if s["ad"] == ad:
                    next_large_candidate = s
                    break
        if not next_small_candidate:
            for s in eligible_small:
                if s["ad"] == ad:
                    next_small_candidate = s
                    break

    return {
        "paused": PAUSE_FILE.exists(),
        "complete": COMPLETE_FILE.exists() or inventory_summary.get("complete", False),
        "control_instance_status": control_instance_status,
        "active_jobs": active_jobs,
        "oci_error": oci_error,
        "inventory": inventory_summary,
        "next_ad_index": rotation_index,
        "current_rotation_ad": rotation_ad,
        "ad_rotation": ad_rotation,
        "next_large_candidate": next_large_candidate,
        "next_small_candidate": next_small_candidate,
        "all_stacks": stack_details,
        "eligible_large_stacks": [s["name"] for s in eligible_large],
        "eligible_small_stacks": [s["name"] for s in eligible_small],
        "consumed_stacks": [s["name"] for s in stack_details if s["is_used"]],
    }


def print_candidates() -> int:
    plan = get_candidate_plan()
    print("=== OCI A1 Launcher Candidate Plan (Read-Only) ===")
    print(f"Paused: {plan['paused']}")
    print(f"Complete: {plan['complete']}")
    print(f"Control Instance Status: {plan['control_instance_status']}")
    print(f"Current Rotation AD: AD-{plan['current_rotation_ad']} (next_ad_index={plan['next_ad_index']})")
    print(f"AD Rotation Sequence: {plan['ad_rotation']}")
    print()

    if plan["oci_error"]:
        print(f"NOTE: {plan['oci_error']}")
        print()

    print("--- Current A1 Inventory ---")
    inv = plan["inventory"]
    print(f"Total A1 Instances: {inv['total']} (Complete: {inv['complete']})")
    for inst in inv.get("large", []):
        print(f"  [2/12] {inst.get('display_name')} | {inst.get('availability_domain')} | {inst.get('id')}")
    for inst in inv.get("small", []):
        print(f"  [1/6]  {inst.get('display_name')} | {inst.get('availability_domain')} | {inst.get('id')}")
    print()

    print("--- Candidate Stacks Analysis ---")
    for s in plan["all_stacks"]:
        status = "CONSUMED" if s["is_used"] else ("ELIGIBLE" if s["is_eligible"] else "EXCLUDED")
        ex_text = f" -> Excluded: {', '.join(s['reasons_excluded'])}" if s["reasons_excluded"] else ""
        print(f"  [{status}] {s['name']} (AD-{s['ad']}, {s['ocpus']:g} OCPU / {s['memory_gb']:g} GB){ex_text}")
    print()

    print("--- Next Candidate Evaluation ---")
    if plan["next_large_candidate"]:
        nl = plan["next_large_candidate"]
        print(f"  Next 2/12 candidate: {nl['name']} (AD-{nl['ad']})")
    else:
        print("  Next 2/12 candidate: None eligible")

    if plan["next_small_candidate"]:
        ns = plan["next_small_candidate"]
        print(f"  Next 1/6 candidate:  {ns['name']} (AD-{ns['ad']})")
    else:
        print("  Next 1/6 candidate:  None eligible")
    print()
    print("NOTE: This diagnostic command is strictly READ-ONLY. No jobs were submitted and no OCI resources were modified.")
    return 0


def get_resize_plan() -> dict[str, Any]:
    """Inspect current state and resize configuration, returning read-only diagnostic plan.

    NO COMPUTE CAPACITY REPORT IS EVER CALLED.
    """
    resize_cfg = get_resize_config()
    is_paused = PAUSE_FILE.exists()
    is_complete = COMPLETE_FILE.exists()

    instance_info: dict[str, Any] | None = None
    stack_accessible = False
    stack_name = "UNKNOWN"
    active_jobs: list[dict[str, Any]] = []
    oci_error: str | None = None

    if not resize_cfg["is_configured"]:
        return {
            "mode": resize_cfg["mode"],
            "is_configured": False,
            "paused": is_paused,
            "complete": is_complete,
            "error": "RESIZE_INSTANCE_OCID or RESIZE_STACK_OCID is missing or uses placeholder values.",
            "status_text": "FAILSAFE - CONFIGURATION MISSING OR PLACEHOLDER",
            "next_action": "REFUSE TO SUBMIT JOBS (SAFE PAUSE)",
            "instance": None,
            "stack": {
                "ocid": resize_cfg["stack_ocid"],
                "name": stack_name,
                "accessible": False,
            },
            "active_jobs": [],
        }

    instance_ocid = resize_cfg["instance_ocid"]
    stack_ocid = resize_cfg["stack_ocid"]
    target_ocpus = resize_cfg["target_ocpus"]
    target_memory_gb = resize_cfg["target_memory_gb"]

    try:
        compute, resource_manager = create_oci_clients()
        try:
            inst_data = compute.get_instance(instance_ocid).data
            instance_info = serialize_instance(inst_data)
        except Exception as exc:
            oci_error = f"Instance API error: {exc}"

        try:
            stk_data = resource_manager.get_stack(stack_ocid).data
            stack_name = getattr(stk_data, "display_name", "resize-stack")
            stack_accessible = True
        except Exception as exc:
            if not oci_error:
                oci_error = f"Stack API error: {exc}"

        try:
            all_active = active_resource_manager_jobs(resource_manager)
            active_jobs = [j for j in all_active if j["stack_ocid"] == stack_ocid]
        except Exception as exc:
            if not oci_error:
                oci_error = f"Job listing error: {exc}"

    except Exception as exc:
        oci_error = f"OCI Client initialization error: {exc}"

    current_ocpus = instance_info["ocpus"] if instance_info else 0.0
    current_memory_gb = instance_info["memory_gb"] if instance_info else 0.0

    target_reached = (
        current_ocpus >= target_ocpus and current_memory_gb >= target_memory_gb
    )

    if is_complete or target_reached:
        status_text = "RESIZE COMPLETE (Target reached)"
        next_action = "NONE (Provisioning complete)"
    elif active_jobs:
        status_text = f"APPLY JOB ALREADY ACTIVE ({active_jobs[0].get('job_id')})"
        next_action = f"WAIT FOR ACTIVE JOB ({active_jobs[0].get('job_id')})"
    elif is_paused:
        status_text = "RESIZE REQUIRED (LAUNCHER PAUSED)"
        next_action = "PAUSED (No action will be taken until resumed)"
    elif DRY_RUN:
        status_text = "RESIZE REQUIRED (DRY RUN MODE)"
        next_action = f"LOG DRY RUN ONLY (Would submit APPLY to {stack_name} / {stack_ocid})"
    else:
        status_text = "RESIZE REQUIRED"
        next_action = f"SUBMIT APPLY TO {stack_name} ({stack_ocid})"

    return {
        "mode": resize_cfg["mode"],
        "is_configured": True,
        "paused": is_paused,
        "complete": is_complete or target_reached,
        "target_reached": target_reached,
        "instance": instance_info,
        "target_shape": {"ocpus": target_ocpus, "memory_gb": target_memory_gb},
        "stack": {
            "name": stack_name,
            "ocid": stack_ocid,
            "accessible": stack_accessible,
        },
        "active_jobs": active_jobs,
        "oci_error": oci_error,
        "status_text": status_text,
        "next_action": next_action,
    }


def print_resize_plan() -> int:
    plan = get_resize_plan()
    print("=== OCI A1 Launcher Resize Plan (Read-Only) ===")
    print(f"Provisioning Mode: {plan['mode']}")
    print(f"Paused: {plan['paused']}")
    print(f"Complete: {plan['complete']}")
    print()

    if not plan["is_configured"]:
        print("--- Configuration Status ---")
        print(f"Status: {plan['status_text']}")
        print(f"Error:  {plan['error']}")
        print(f"Next action: {plan['next_action']}")
        print()
        print("NOTE: This diagnostic command is strictly READ-ONLY. No capacity report was requested and no OCI resources were modified.")
        return 0

    if plan["oci_error"]:
        print(f"NOTE: {plan['oci_error']}")
        print()

    print("--- Target Instance ---")
    inst = plan["instance"]
    if inst:
        print(f"  Name:    {inst.get('display_name')}")
        print(f"  OCID:    {inst.get('id')}")
        print(f"  Current: {inst.get('ocpus'):g} OCPU / {inst.get('memory_gb'):g} GB RAM")
    else:
        print("  Instance details unavailable (OCI API offline or invalid OCID)")
    t_shape = plan["target_shape"]
    print(f"  Target:  {t_shape['ocpus']:g} OCPU / {t_shape['memory_gb']:g} GB RAM")
    print()

    print("--- Resize Stack ---")
    stk = plan["stack"]
    print(f"  Name:       {stk['name']}")
    print(f"  OCID:       {stk['ocid']}")
    print(f"  Accessible: {stk['accessible']}")
    print()

    print("--- Current Status & Action ---")
    print(f"  Current status: {plan['status_text']}")
    print(f"  Next cycle:     {plan['next_action']}")
    print()
    print("NOTE: This diagnostic command is strictly READ-ONLY. No capacity report was requested and no OCI resources were modified.")
    return 0


def print_status() -> int:
    state = load_state()
    resize_cfg = get_resize_config()
    print(f"Provisioning Mode: {resize_cfg['mode']}")
    print(json.dumps(state, indent=2, sort_keys=True))
    print(f"Paused: {PAUSE_FILE.exists()}")
    print(f"Complete: {COMPLETE_FILE.exists()}")
    cooldown = load_throttle_cooldown()
    if cooldown:
        until = cooldown["until_datetime"]
        print(f"Throttled: True (until {local_timestamp(until)} / {utc_iso(until)})")
    else:
        print("Throttled: False")
    if COMPLETE_FILE.exists():
        print(COMPLETE_FILE.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run one scheduled launcher cycle")
    doctor_parser = subparsers.add_parser("doctor", help="Validate OCI access and optional SMTP")
    doctor_parser.add_argument("--send-test-email", action="store_true")
    subparsers.add_parser("status", help="Print local launcher state")
    subparsers.add_parser("candidates", help="Inspect candidate stack selection (read-only)")
    subparsers.add_parser("plan", help="Inspect candidate stack selection (read-only)")
    subparsers.add_parser("resize-plan", help="Inspect resize-only candidate plan (read-only)")
    args = parser.parse_args()
    if args.command == "run":
        return run_once()
    if args.command == "doctor":
        return doctor(args.send_test_email)
    if args.command == "status":
        return print_status()
    if args.command in ("candidates", "plan"):
        return print_candidates()
    if args.command == "resize-plan":
        return print_resize_plan()
    return 2


if __name__ == "__main__":
    sys.exit(main())
