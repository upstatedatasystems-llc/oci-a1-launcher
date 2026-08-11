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
    """Find active jobs for the six managed stacks with one compartment-wide API call."""
    managed_stack_ids = set(STACK_BY_OCID)
    jobs = resource_manager.list_jobs(
        compartment_id=COMPARTMENT_OCID,
        sort_by="TIMECREATED",
        sort_order="DESC",
        limit=100,
        # Fail fast on throttling so the launcher can enter its one-hour cooldown
        # instead of making repeated SDK retry attempts.
        retry_strategy=oci.retry.NoneRetryStrategy(),
    ).data
    active: list[dict[str, Any]] = []
    for job in jobs:
        stack_id = getattr(job, "stack_id", None)
        state = getattr(job, "lifecycle_state", None)
        if stack_id not in managed_stack_ids or state not in ACTIVE_JOB_STATES:
            continue
        stack = STACK_BY_OCID[stack_id]
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

        get_env_required("COMPARTMENT_OCID")
        get_env_required("CONTROL_INSTANCE_OCID")

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
        except oci.exceptions.ServiceError as exc:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run", help="Run one scheduled launcher cycle")
    doctor_parser = subparsers.add_parser("doctor", help="Validate OCI access and optional SMTP")
    doctor_parser.add_argument("--send-test-email", action="store_true")
    subparsers.add_parser("status", help="Print local launcher state")
    subparsers.add_parser("candidates", help="Inspect candidate stack selection (read-only)")
    subparsers.add_parser("plan", help="Inspect candidate stack selection (read-only)")
    args = parser.parse_args()
    if args.command == "run":
        return run_once()
    if args.command == "doctor":
        return doctor(args.send_test_email)
    if args.command == "status":
        return print_status()
    if args.command in ("candidates", "plan"):
        return print_candidates()
    return 2


if __name__ == "__main__":
    sys.exit(main())
