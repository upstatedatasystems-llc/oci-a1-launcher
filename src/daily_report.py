#!/usr/bin/env python3
"""Email the previous local calendar day's OCI A1 launcher job log."""

from __future__ import annotations

import os
import socket
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Any

from common import (
    COMPLETE_FILE,
    LOCAL_TZ,
    append_event,
    exclusive_lock,
    local_timestamp,
    parse_datetime,
    read_events,
    send_email,
)

CONTROL_INSTANCE_NAME = os.getenv("CONTROL_INSTANCE_NAME", "purgatory01-vm").strip()


def event_datetime(event: dict[str, Any]) -> datetime | None:
    for key in ("job_time_created", "timestamp", "run_started"):
        parsed = parse_datetime(event.get(key))
        if parsed:
            return parsed
    return None


def format_job(event: dict[str, Any]) -> list[str]:
    stack = event.get("stack", {})
    started = event_datetime(event)
    local_started = started.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if started else "unknown time"
    job_id = event.get("job_id") or "no-job-id"
    lines = [f"[{local_started} | {stack.get('name', 'unknown-stack')} | {job_id}]"]
    errors = event.get("error_lines") or []
    if errors:
        lines.extend(errors)
    else:
        lines.append("No ERROR-level log entries.")
    state = event.get("job_state", "UNKNOWN")
    result = event.get("result", state)
    if event.get("capacity_error"):
        result = f"{result} (OUT OF HOST CAPACITY)"
    if event.get("timed_out"):
        result = f"{result} (POLL TIMEOUT; JOB MAY STILL BE ACTIVE)"
    lines.append(f"RESULT: {result} | Resource Manager state={state}")
    for instance in event.get("new_instances", []):
        lines.append(
            "CREATED: "
            f"{instance.get('display_name') or '<unnamed>'} | "
            f"{instance.get('ocpus')} OCPU / {instance.get('memory_gb')} GB | "
            f"{instance.get('availability_domain')} | {instance.get('id')}"
        )
    return lines


def format_system_error(event: dict[str, Any]) -> list[str]:
    timestamp = event_datetime(event)
    local_time = timestamp.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if timestamp else "unknown time"
    return [
        f"[{local_time} | SYSTEM | stage={event.get('stage', 'unknown')}]",
        f"ERROR - {event.get('message') or event.get('code') or 'Unknown system error'}",
        "RESULT: SYSTEM ERROR",
    ]


def format_throttle(event: dict[str, Any]) -> list[str]:
    timestamp = event_datetime(event)
    local_time = timestamp.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z") if timestamp else "unknown time"
    until = parse_datetime(event.get("until"))
    until_text = local_timestamp(until) if until else "unknown"
    return [
        f"[{local_time} | OCI API THROTTLE]",
        f"ERROR - HTTP {event.get('status', 429)} {event.get('code', 'TooManyRequests')}: "
        f"{event.get('message', 'Too many requests')}",
        f"COOLDOWN UNTIL: {until_text}",
        "RESULT: THROTTLED",
    ]


def main() -> int:
    with exclusive_lock(blocking=True) as acquired:
        if not acquired:
            return 0
        now_local = datetime.now(LOCAL_TZ)
        report_date = now_local.date() - timedelta(days=1)
        start_local = datetime.combine(report_date, time.min, tzinfo=LOCAL_TZ)
        end_local = start_local + timedelta(days=1)
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        # After completion, send the final report for the completion day, then
        # stop sending empty daily emails on later days.
        if COMPLETE_FILE.exists():
            try:
                import json

                complete = json.loads(COMPLETE_FILE.read_text(encoding="utf-8"))
                completed_at = parse_datetime(complete.get("completed_at"))
                if completed_at and completed_at.astimezone(LOCAL_TZ).date() < report_date:
                    return 0
            except Exception:
                pass

        selected: list[dict[str, Any]] = []
        for event in read_events():
            timestamp = event_datetime(event)
            if timestamp and start_utc <= timestamp.astimezone(timezone.utc) < end_utc:
                if event.get("event_type") in {"job", "system_error", "throttle", "completion", "dry_run"}:
                    selected.append(event)
        selected.sort(key=lambda item: event_datetime(item) or start_utc)

        job_events = [event for event in selected if event.get("event_type") == "job"]
        system_errors = [event for event in selected if event.get("event_type") == "system_error"]
        throttle_events = [event for event in selected if event.get("event_type") == "throttle"]
        created_count = sum(1 for event in job_events if event.get("new_instances"))
        successful_job_count = sum(1 for event in job_events if event.get("job_state") == "SUCCEEDED")
        capacity_failure_count = sum(1 for event in job_events if event.get("capacity_error"))

        if created_count:
            day_result = "SUCCESS"
        elif system_errors:
            day_result = "FAILED"
        elif job_events or throttle_events:
            day_result = "NO SUCCESS"
        else:
            day_result = "NO JOBS"

        subject = f"OCI A1 Launcher Daily Log - {report_date.isoformat()} - {day_result}"
        lines = [
            f"{local_timestamp()} | {CONTROL_INSTANCE_NAME} | hostname={socket.gethostname()}",
            f"REPORTING PERIOD: {report_date.isoformat()} 00:00:00-23:59:59 {LOCAL_TZ.key}",
            "",
        ]

        if not selected:
            lines.append("No Resource Manager apply jobs or launcher system errors were recorded.")
        else:
            for event in selected:
                event_type = event.get("event_type")
                if event_type == "job":
                    lines.extend(format_job(event))
                elif event_type == "system_error":
                    lines.extend(format_system_error(event))
                elif event_type == "throttle":
                    lines.extend(format_throttle(event))
                elif event_type == "completion":
                    lines.extend(
                        [
                            f"[{local_timestamp(event_datetime(event))} | COMPLETION]",
                            f"RESULT: COMPLETE - {event.get('reason', 'A1 allocation full')}",
                        ]
                    )
                elif event_type == "dry_run":
                    stack = event.get("stack", {})
                    lines.extend(
                        [
                            f"[{local_timestamp(event_datetime(event))} | {stack.get('name')} | DRY RUN]",
                            "RESULT: DRY_RUN_NOT_SUBMITTED",
                        ]
                    )
                lines.append("")

        lines.extend(
            [
                "SUMMARY:",
                f"Apply jobs: {len(job_events)}",
                f"Resource Manager SUCCEEDED jobs: {successful_job_count}",
                f"Actual new A1 instances detected: {created_count}",
                f"Out-of-host-capacity failures: {capacity_failure_count}",
                f"OCI API throttle events: {len(throttle_events)}",
                f"Launcher/system errors: {len(system_errors)}",
                "",
                f"DAY RESULT: {day_result}",
            ]
        )

        try:
            send_email(subject, "\n".join(lines))
            append_event(
                {
                    "event_type": "email",
                    "email_kind": "daily_report",
                    "report_date": report_date.isoformat(),
                    "subject": subject,
                    "result": "SENT",
                }
            )
            return 0
        except Exception as exc:  # noqa: BLE001
            append_event(
                {
                    "event_type": "system_error",
                    "stage": "daily_report_email",
                    "message": f"{type(exc).__name__}: {exc}",
                    "report_date": report_date.isoformat(),
                }
            )
            return 1


if __name__ == "__main__":
    sys.exit(main())
