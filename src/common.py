#!/usr/bin/env python3
"""Shared helpers for the OCI A1 Resource Manager launcher."""

from __future__ import annotations

import fcntl
import json
import os
import smtplib
import socket
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterator, Sequence
from zoneinfo import ZoneInfo

import oci

LOCAL_TZ = ZoneInfo(os.getenv("LOCAL_TIMEZONE", "America/New_York"))
REGION = os.getenv("OCI_REGION", "us-ashburn-1")
DATA_DIR = Path(os.getenv("DATA_DIR", "/var/lib/oci-a1-launcher"))
STATE_FILE = DATA_DIR / "state.json"
EVENT_FILE = DATA_DIR / "events.jsonl"
LOCK_FILE = DATA_DIR / "launcher.lock"
PAUSE_FILE = DATA_DIR / "PAUSED"
COMPLETE_FILE = DATA_DIR / "COMPLETE.json"
THROTTLE_FILE = DATA_DIR / "THROTTLED.json"

ACTIVE_JOB_STATES = {"ACCEPTED", "IN_PROGRESS", "CANCELING"}
TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "CANCELED"}
NON_TERMINATED_INSTANCE_STATES = {
    "CREATING_IMAGE",
    "MOVING",
    "PROVISIONING",
    "RUNNING",
    "STARTING",
    "STOPPED",
    "STOPPING",
    "TERMINATING",
}


@dataclass(frozen=True)
class StackSpec:
    name: str
    ocid: str
    ad: int
    ocpus: float
    memory_gb: float


STACKS: tuple[StackSpec, ...] = (
    StackSpec(
        "purgatory02-ad1",
        "ocid1.ormstack.oc1.iad.amaaaaaa5gdmbsaaxhcnhb3egyphv4xkr3kcmlughtnio5vcubgsqllnaisq",
        1,
        2,
        12,
    ),
    StackSpec(
        "purgatory02-ad1e",
        "ocid1.ormstack.oc1.iad.amaaaaaa5gdmbsaagusww37uvku5n32n5t6ypcel727f5bwsl37zr6qbhrpq",
        1,
        1,
        6,
    ),
    StackSpec(
        "purgatory02-ad2",
        "ocid1.ormstack.oc1.iad.amaaaaaa5gdmbsaag34ogwafrkcp7bj675vxsh333o4q3weyw6kibvhfuquq",
        2,
        2,
        12,
    ),
    StackSpec(
        "purgatory02-ad2e",
        "ocid1.ormstack.oc1.iad.amaaaaaa5gdmbsaabdgmlwuql7yem2axve4rzfbg3em7fecyhpox7tjbmroa",
        2,
        1,
        6,
    ),
    StackSpec(
        "purgatory02-ad3",
        "ocid1.ormstack.oc1.iad.amaaaaaa5gdmbsaaavipqidc6by6yf4owczhl5bxdlljdvjdzd7ikd4rnlta",
        3,
        2,
        12,
    ),
    StackSpec(
        "purgatory02-ad3e",
        "ocid1.ormstack.oc1.iad.amaaaaaa5gdmbsaalp2rajyorphv4oag5h5pd4sz5tygpvtuoerhrnha7kuq",
        3,
        1,
        6,
    ),
)

STACK_BY_OCID = {stack.ocid: stack for stack in STACKS}
STACK_BY_NAME = {stack.name: stack for stack in STACKS}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    value = dt or now_utc()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def local_timestamp(dt: datetime | None = None) -> str:
    value = dt or now_utc()
    return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)


@contextmanager
def exclusive_lock(blocking: bool) -> Iterator[bool]:
    """Acquire the shared launcher/report lock."""
    ensure_data_dir()
    with LOCK_FILE.open("a+", encoding="utf-8") as handle:
        flags = fcntl.LOCK_EX
        if not blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "next_ad_index": 0,
            "successful_stack_ocids": [],
            "successful_instance_ids": [],
            "updated_at": utc_iso(),
        }
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        corrupt = STATE_FILE.with_suffix(f".corrupt-{int(time.time())}.json")
        STATE_FILE.replace(corrupt)
        return {
            "next_ad_index": 0,
            "successful_stack_ocids": [],
            "successful_instance_ids": [],
            "updated_at": utc_iso(),
        }
    data.setdefault("next_ad_index", 0)
    data.setdefault("successful_stack_ocids", [])
    data.setdefault("successful_instance_ids", [])
    return data


def save_state(state: dict[str, Any]) -> None:
    ensure_data_dir()
    state["updated_at"] = utc_iso()
    fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, STATE_FILE)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_event(event: dict[str, Any]) -> None:
    ensure_data_dir()
    event.setdefault("timestamp", utc_iso())
    event.setdefault("hostname", socket.gethostname())
    line = json.dumps(event, sort_keys=True, default=str)
    with EVENT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(EVENT_FILE, 0o600)
    print(line, flush=True)


def read_events() -> list[dict[str, Any]]:
    if not EVENT_FILE.exists():
        return []
    events: list[dict[str, Any]] = []
    with EVENT_FILE.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def create_oci_clients() -> tuple[oci.core.ComputeClient, oci.resource_manager.ResourceManagerClient]:
    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    config = {"region": REGION}
    compute = oci.core.ComputeClient(config=config, signer=signer)
    resource_manager = oci.resource_manager.ResourceManagerClient(config=config, signer=signer)
    return compute, resource_manager


def all_results(method: Any, *args: Any, **kwargs: Any) -> list[Any]:
    response = oci.pagination.list_call_get_all_results(method, *args, **kwargs)
    return list(response.data)


def serialize_instance(instance: Any) -> dict[str, Any]:
    shape_config = getattr(instance, "shape_config", None)
    return {
        "id": getattr(instance, "id", None),
        "display_name": getattr(instance, "display_name", None),
        "shape": getattr(instance, "shape", None),
        "ocpus": float(getattr(shape_config, "ocpus", 0) or 0),
        "memory_gb": float(getattr(shape_config, "memory_in_gbs", 0) or 0),
        "availability_domain": getattr(instance, "availability_domain", None),
        "lifecycle_state": getattr(instance, "lifecycle_state", None),
        "time_created": (
            getattr(instance, "time_created", None).isoformat()
            if getattr(instance, "time_created", None)
            else None
        ),
    }


def is_close(value: float, expected: float, tolerance: float = 0.01) -> bool:
    return abs(value - expected) <= tolerance


def classify_a1_instances(instances: Sequence[dict[str, Any]]) -> dict[str, Any]:
    small: list[dict[str, Any]] = []
    large: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    for instance in instances:
        if is_close(instance["ocpus"], 1) and is_close(instance["memory_gb"], 6):
            small.append(instance)
        elif is_close(instance["ocpus"], 2) and is_close(instance["memory_gb"], 12):
            large.append(instance)
        else:
            unexpected.append(instance)
    complete = (len(large) >= 1) or (len(small) >= 2)
    return {
        "small": small,
        "large": large,
        "unexpected": unexpected,
        "complete": complete,
        "total": len(instances),
    }


def infer_ad_number(ad_name: str | None) -> int | None:
    if not ad_name:
        return None
    upper = ad_name.upper()
    for ad in (1, 2, 3):
        if upper.endswith(f"AD-{ad}"):
            return ad
    return None


def mark_complete(classification: dict[str, Any], reason: str) -> None:
    ensure_data_dir()
    payload = {
        "completed_at": utc_iso(),
        "completed_at_local": local_timestamp(),
        "reason": reason,
        "classification": classification,
    }
    COMPLETE_FILE.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(COMPLETE_FILE, 0o600)
    append_event({"event_type": "completion", "reason": reason, "classification": classification})


def get_env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set")
    return value


def send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    port = int(os.getenv("SMTP_PORT", "465"))
    username = get_env_required("SMTP_USER")
    password = get_env_required("SMTP_APP_PASSWORD").replace(" ", "")
    sender = os.getenv("SMTP_FROM", username).strip()
    recipients = [address.strip() for address in get_env_required("SMTP_TO").split(",") if address.strip()]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
                smtp.login(username, password)
                smtp.send_message(message)
            return
        except Exception as exc:  # noqa: BLE001 - preserve SMTP error in event log
            last_error = exc
            if attempt < 3:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Email delivery failed after 3 attempts: {last_error}")


def instance_summary(instance: dict[str, Any]) -> str:
    return (
        f"{instance.get('display_name') or '<unnamed>'} | {instance['ocpus']:g} OCPU / "
        f"{instance['memory_gb']:g} GB | {instance.get('availability_domain')} | "
        f"{instance.get('lifecycle_state')} | {instance.get('id')}"
    )


def stack_as_dict(stack: StackSpec) -> dict[str, Any]:
    return asdict(stack)
