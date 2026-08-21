"""File-backed review events shared by the browser server and MCP agent.

Delivery and review are separate state machines:

    delivery: draft -> queued -> acknowledged
    review:   pending -> accept/reject/defer/adjusted

The queue keeps one JSON file per event.  A dispatch assigns one immutable
``batch_id`` to all events moved to ``queued``.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

QUEUE_DIR = ".review/inbox"
SCHEMA = "docx2typed-review-event-1"
_ALLOWED_TYPES = {"decision", "comment", "patch"}
_ALLOWED_STATUSES = {"draft", "queued", "acknowledged"}
_ALLOWED_DELIVERY_STATES = {"staged", "queued", "in_progress", "applied", "acknowledged"}
_ALLOWED_REVIEW_DECISIONS = {"pending", "accept", "reject", "defer", "adjusted", "comment"}
_MAX_TEXT = 8_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root(workdir: Path) -> Path:
    path = Path(workdir) / QUEUE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _event_path(workdir: Path, event_id: str) -> Path:
    if not event_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in event_id):
        raise ValueError("invalid event_id")
    return _root(workdir) / f"{event_id}.json"


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _bounded(value: Any, name: str, limit: int = _MAX_TEXT) -> str:
    if value is None:
        return ""
    result = str(value).strip()
    if len(result) > limit:
        raise ValueError(f"{name} is too long")
    return result


def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = _bounded(event.get("type"), "type", 32)
    if event_type == "patch":
        from .review_collab import _validate_patch

        return _validate_patch(event)
    if event_type not in {"decision", "comment"}:
        # Keep the original error contract for callers that probe unsupported
        # event kinds while adding patch as the collaboration event.
        raise ValueError("type must be decision or comment")
    normalized = dict(event)
    normalized["type"] = event_type
    normalized["client_id"] = _bounded(event.get("client_id"), "client_id", 200)
    if not normalized["client_id"]:
        raise ValueError("client_id is required")
    normalized["review_item_id"] = _bounded(
        event.get("review_item_id") or f"{event_type}:{normalized['client_id']}",
        "review_item_id",
        300,
    )
    normalized["paragraph_id"] = _bounded(event.get("paragraph_id"), "paragraph_id", 120)
    if event_type == "decision":
        decision = _bounded(event.get("decision"), "decision", 32)
        if decision not in {"accept", "reject", "defer", "comment"}:
            raise ValueError("decision must be accept, reject, defer, or comment")
        normalized["decision"] = decision
        normalized["review_decision"] = decision
        normalized["revision_key"] = _bounded(event.get("revision_key"), "revision_key", 500)
        normalized["revision_id"] = _bounded(event.get("revision_id"), "revision_id", 120)
        normalized["selected_text"] = _bounded(event.get("selected_text"), "selected_text")
        normalized["comment"] = _bounded(event.get("comment"), "comment")
    else:
        normalized["review_decision"] = "pending"
        normalized["selected_text"] = _bounded(event.get("selected_text"), "selected_text")
        normalized["note"] = _bounded(event.get("note"), "note")
        if not normalized["selected_text"] or not normalized["note"]:
            raise ValueError("comment requires selected_text and note")
        normalized["before_context"] = _bounded(event.get("before_context"), "before_context", 2_000)
        normalized["after_context"] = _bounded(event.get("after_context"), "after_context", 2_000)
    normalized["delivery_state"] = "staged"
    return normalized

@contextmanager
def _queue_lane(workdir: Path):
    """Serialize queue read/modify/write transactions across processes.

    Uses the store's OS-advisory lane (flock/msvcrt, fixed inode) instead of
    an O_EXCL sentinel: the lock dies with its holder process, so a crash
    mid-write can never leave the queue permanently busy. The lock file is
    never deleted or reclaimed by PID/age."""
    lock_path = _root(workdir) / ".queue.lock"
    try:
        from .store import WriterBusy, advisory_lane
    except ImportError:  # pragma: no cover - direct script invocation fallback
        from store import WriterBusy, advisory_lane  # type: ignore[no-redef]
    try:
        with advisory_lane(lock_path):
            yield
    except WriterBusy as exc:
        raise RuntimeError("review queue is busy") from exc


def _queue_transaction(function):
    @wraps(function)
    def wrapped(workdir: Path, *args: Any, **kwargs: Any):
        with _queue_lane(workdir):
            return function(workdir, *args, **kwargs)

    return wrapped



def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_events(workdir: Path) -> list[dict[str, Any]]:
    root = _root(workdir)
    events: list[dict[str, Any]] = []
    for path in root.glob("*.json"):
        event = _read(path)
        if event and event.get("schema") == SCHEMA and event.get("status") in _ALLOWED_STATUSES:
            event.setdefault("delivery_state", "acknowledged" if event.get("status") == "acknowledged" else event["status"])
            event.setdefault("review_decision", event.get("decision", "pending"))
            events.append(event)
    return sorted(events, key=lambda event: (str(event.get("created_at", "")), str(event.get("event_id", ""))))


@_queue_transaction
def upsert_event(workdir: Path, event: dict[str, Any]) -> dict[str, Any]:
    normalized = _validate_event(event)
    existing = next(
        (
            item
            for item in list_events(workdir)
            if item.get("client_id") == normalized["client_id"] and item.get("status") == "draft"
        ),
        None,
    )
    now = _now()
    event_id = str(existing.get("event_id")) if existing else str(event.get("event_id") or uuid.uuid4().hex)
    record = {
        **normalized,
        "schema": SCHEMA,
        "event_id": event_id,
        "status": "draft",
        "delivery_state": "staged",
        "created_at": str(existing.get("created_at", now)) if existing else now,
        "updated_at": now,
    }
    _atomic_write(_event_path(workdir, event_id), record)
    return record


@_queue_transaction
def update_event(workdir: Path, event_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    path = _event_path(workdir, event_id)
    event = _read(path)
    if not event or event.get("schema") != SCHEMA:
        raise ValueError("event not found")
    next_event = {**event, **updates, "event_id": event_id, "schema": SCHEMA, "updated_at": _now()}
    if next_event.get("status") not in _ALLOWED_STATUSES:
        raise ValueError("invalid event status")
    if next_event.get("delivery_state") not in _ALLOWED_DELIVERY_STATES:
        raise ValueError("invalid delivery state")
    if next_event.get("review_decision") not in _ALLOWED_REVIEW_DECISIONS:
        raise ValueError("invalid review decision")
    _atomic_write(path, next_event)
    return next_event


@_queue_transaction
def dispatch(workdir: Path) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []
    batch_id = f"batch-{uuid.uuid4().hex}"
    for event in list_events(workdir):
        if event.get("status") != "draft":
            continue
        event["status"] = "queued"
        event["delivery_state"] = "queued"
        event["batch_id"] = batch_id
        event["queued_at"] = _now()
        event["updated_at"] = event["queued_at"]
        _atomic_write(_event_path(workdir, str(event["event_id"])), event)
        queued.append(event)
    return queued


@_queue_transaction
def acknowledge(workdir: Path, event_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(event_ids)
    acknowledged: list[dict[str, Any]] = []
    for event in list_events(workdir):
        if str(event.get("event_id")) not in wanted:
            continue
        if event.get("status") == "acknowledged":
            acknowledged.append(event)
            continue
        if event.get("status") != "queued":
            continue
        event["status"] = "acknowledged"
        event["delivery_state"] = "acknowledged"
        event["acknowledged_at"] = _now()
        event["updated_at"] = event["acknowledged_at"]
        _atomic_write(_event_path(workdir, str(event["event_id"])), event)
        acknowledged.append(event)
    return acknowledged


def _snapshot_dict(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "events": events,
        "counts": {
            "draft": sum(event.get("status") == "draft" for event in events),
            "queued": sum(event.get("status") == "queued" for event in events),
            "acknowledged": sum(event.get("status") == "acknowledged" for event in events),
        },
        "review_counts": {
            decision: sum(event.get("review_decision") == decision for event in events)
            for decision in sorted(_ALLOWED_REVIEW_DECISIONS)
        },
    }


def snapshot(workdir: Path) -> dict[str, Any]:
    events = list_events(workdir)
    return _snapshot_dict(events)


def snapshot_readonly(workdir: Path) -> dict[str, Any]:
    """Snapshot without any side effect: never creates the inbox directory.
    A review session that has never received a write reads as empty."""
    inbox = Path(workdir).resolve() / QUEUE_DIR
    if not inbox.is_dir():
        return _snapshot_dict([])
    events: list[dict[str, Any]] = []
    for path in inbox.glob("*.json"):
        event = _read(path)
        if event and event.get("schema") == SCHEMA and event.get("status") in _ALLOWED_STATUSES:
            event.setdefault("delivery_state", "acknowledged" if event.get("status") == "acknowledged" else event["status"])
            event.setdefault("review_decision", event.get("decision", "pending"))
            events.append(event)
    return _snapshot_dict(sorted(events, key=lambda event: (str(event.get("created_at", "")), str(event.get("event_id", "")))))
