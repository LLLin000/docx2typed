"""File-backed review events shared by the browser server and MCP agent.

The queue uses one JSON file per event instead of a shared mutable index. That
keeps browser and MCP processes from clobbering each other's writes and makes
the handoff auditable:

    draft -> queued -> acknowledged

The browser saves drafts. The reviewer explicitly dispatches them. The agent
reads queued events through MCP and acknowledges them after consuming them.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

QUEUE_DIR = ".review/inbox"
SCHEMA = "docx2typed-review-event-1"
_ALLOWED_TYPES = {"decision", "comment"}
_ALLOWED_STATUSES = {"draft", "queued", "acknowledged"}
_MAX_TEXT = 8_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root(workdir: Path) -> Path:
    path = workdir / QUEUE_DIR
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
    if event_type not in _ALLOWED_TYPES:
        raise ValueError("type must be decision or comment")
    normalized = dict(event)
    normalized["type"] = event_type
    normalized["client_id"] = _bounded(event.get("client_id"), "client_id", 200)
    if not normalized["client_id"]:
        raise ValueError("client_id is required")
    normalized["paragraph_id"] = _bounded(event.get("paragraph_id"), "paragraph_id", 120)
    if event_type == "decision":
        decision = _bounded(event.get("decision"), "decision", 32)
        if decision not in {"accept", "reject", "comment"}:
            raise ValueError("decision must be accept, reject, or comment")
        normalized["decision"] = decision
        normalized["revision_key"] = _bounded(event.get("revision_key"), "revision_key", 500)
        normalized["revision_id"] = _bounded(event.get("revision_id"), "revision_id", 120)
        normalized["selected_text"] = _bounded(event.get("selected_text"), "selected_text")
        normalized["comment"] = _bounded(event.get("comment"), "comment")
    else:
        normalized["selected_text"] = _bounded(event.get("selected_text"), "selected_text")
        normalized["note"] = _bounded(event.get("note"), "note")
        if not normalized["selected_text"] or not normalized["note"]:
            raise ValueError("comment requires selected_text and note")
        normalized["before_context"] = _bounded(event.get("before_context"), "before_context", 2_000)
        normalized["after_context"] = _bounded(event.get("after_context"), "after_context", 2_000)
    return normalized


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
            events.append(event)
    return sorted(events, key=lambda event: (str(event.get("created_at", "")), str(event.get("event_id", ""))))


def upsert_event(workdir: Path, event: dict[str, Any]) -> dict[str, Any]:
    normalized = _validate_event(event)
    existing = next(
        (item for item in list_events(workdir) if item.get("client_id") == normalized["client_id"] and item.get("status") != "acknowledged"),
        None,
    )
    now = _now()
    event_id = str(existing.get("event_id")) if existing else str(event.get("event_id") or uuid.uuid4().hex)
    record = {
        **normalized,
        "schema": SCHEMA,
        "event_id": event_id,
        "status": "draft",
        "created_at": str(existing.get("created_at", now)) if existing else now,
        "updated_at": now,
    }
    if existing and existing.get("status") == "queued":
        record["status"] = "draft"
        record["reopened_at"] = now
    _atomic_write(_event_path(workdir, event_id), record)
    return record


def dispatch(workdir: Path) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []
    for event in list_events(workdir):
        if event.get("status") != "draft":
            continue
        event["status"] = "queued"
        event["queued_at"] = _now()
        event["updated_at"] = event["queued_at"]
        _atomic_write(_event_path(workdir, str(event["event_id"])), event)
        queued.append(event)
    return queued


def acknowledge(workdir: Path, event_ids: list[str]) -> list[dict[str, Any]]:
    wanted = set(event_ids)
    acknowledged: list[dict[str, Any]] = []
    for event in list_events(workdir):
        if str(event.get("event_id")) not in wanted or event.get("status") != "queued":
            continue
        event["status"] = "acknowledged"
        event["acknowledged_at"] = _now()
        event["updated_at"] = event["acknowledged_at"]
        _atomic_write(_event_path(workdir, str(event["event_id"])), event)
        acknowledged.append(event)
    return acknowledged


def snapshot(workdir: Path) -> dict[str, Any]:
    events = list_events(workdir)
    return {
        "schema": SCHEMA,
        "events": events,
        "counts": {
            "draft": sum(event.get("status") == "draft" for event in events),
            "queued": sum(event.get("status") == "queued" for event in events),
            "acknowledged": sum(event.get("status") == "acknowledged" for event in events),
        },
    }
