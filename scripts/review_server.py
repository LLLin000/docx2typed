"""Serve the document review console and its agent handoff API.

The server binds to localhost by default. ``--tailscale`` binds only to the
machine's Tailscale IPv4 address so a phone on the same tailnet can use the
review console without exposing it on every network interface. The browser
writes review drafts to the workdir inbox, and an explicit dispatch moves them
to the MCP-visible ``queued`` state.

Usage: python -m docx2typed review WORKDIR --tailscale --port 8876
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .review_collab import CollaborationError, document_state, external_write_guard, publish_current, settle_decisions, stage_patch
    from .review_console import render_document_fragment, render_html, review_history
    from .review_queue import dispatch, snapshot, upsert_event
    from .protocol import canonical_operation_input, result_envelope
    from .store import (
        GENERATION_CONFLICT,
        NEEDS_RECOVERY,
        OPERATION_JOURNAL_CONFLICT,
        RESERVE_DEPLETED,
        STORE_INVALID,
        UNSUPPORTED_BY_DESIGN,
        WRITER_BUSY,
        WRITER_TIMEOUT,
        Store,
        StoreError,
    )
except ImportError:  # pragma: no cover - direct script invocation fallback
    from review_collab import CollaborationError, document_state, external_write_guard, publish_current, settle_decisions, stage_patch  # type: ignore[no-redef]
    from review_console import render_document_fragment, render_html, review_history  # type: ignore[no-redef]
    from review_queue import dispatch, snapshot, upsert_event  # type: ignore[no-redef]
    from protocol import canonical_operation_input, result_envelope  # type: ignore[no-redef]
    from store import (  # type: ignore[no-redef]
        GENERATION_CONFLICT,
        NEEDS_RECOVERY,
        OPERATION_JOURNAL_CONFLICT,
        RESERVE_DEPLETED,
        STORE_INVALID,
        UNSUPPORTED_BY_DESIGN,
        WRITER_BUSY,
        WRITER_TIMEOUT,
        Store,
        StoreError,
    )


_MAX_BODY = 256 * 1024
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def _review_mutation(
    target: Path,
    run: Callable[[Path], dict[str, object]],
    *,
    extra: Callable[[Path], dict[str, object]],
) -> tuple[str, dict[str, object], str, dict[str, object], list[dict[str, object]]]:
    """Adapt one review mutation to the store's run contract. ``run(target)``
    performs the review state change against the generation snapshot; the
    response data merges the mutation result with extras computed AFTER the
    mutation lands, so success payloads carry fresh session/count state and
    the next review POST needs no extra GET (issue #50 finding 1)."""
    result = run(target)
    data: dict[str, object] = {}
    if isinstance(result, dict):
        data.update(result)
    data.update(extra(target))
    return (
        "success",
        data,
        "mutation",
        {"checks": [{"name": "review-mutation", "status": "pass"}]},
        [],
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")

# Browser surfaces never receive store-failure internals: absolute paths,
# Windows drive letters, or transient temp filenames embedded in StoreError
# messages. The detail is the stable per-code message — identical on every
# machine and run — so the review console shows a deterministic diagnostic.
_STORE_ERROR_DETAIL: dict[str, str] = {
    STORE_INVALID: "store state is invalid; inspect the workdir for recovery status",
    WRITER_BUSY: "another writer is active; retry after it finishes",
    WRITER_TIMEOUT: "writer lane timed out; retry after the current writer finishes",
    GENERATION_CONFLICT: "workdir changed since planning; re-read the workdir and retry",
    NEEDS_RECOVERY: "workdir needs recovery; run the recovery pass before retrying",
    RESERVE_DEPLETED: "workdir recovery reserve is depleted; the workdir is read-only",
    UNSUPPORTED_BY_DESIGN: "filesystem does not meet the store durability requirements",
    OPERATION_JOURNAL_CONFLICT: "operation journal conflict; retry with a fresh operation id",
    "workdir-unreadable": "workdir cannot be opened as a store-backed workdir",
}

def _error_payload(exc: Exception) -> dict[str, str]:
    code = str(getattr(exc, "code", "") or "")
    detail = str(getattr(exc, "detail", "") or str(exc))
    if not code:
        match = re.match(r"^([a-z][a-z0-9-]*):\s*(.*)$", detail)
        if match:
            code, detail = match.groups()
    elif code in _STORE_ERROR_DETAIL:
        detail = _STORE_ERROR_DETAIL[code]
    return {"error": detail, "code": code or "server-error"}

def _tailscale_ipv4() -> str:
    command = shutil.which("tailscale")
    if not command:
        raise RuntimeError("tailscale-not-installed: install Tailscale and add it to PATH")
    try:
        result = subprocess.run(
            [command, "ip", "-4"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"tailscale-unavailable: could not query Tailscale ({exc})") from exc
    for token in result.stdout.split():
        try:
            address = ipaddress.ip_address(token)
        except ValueError:
            continue
        if address.version == 4:
            return str(address)
    raise RuntimeError("tailscale-no-ipv4: Tailscale returned no IPv4 address")


def _handler_for(workdir: Path) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _send(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, value: object) -> None:
            self._send(status, "application/json; charset=utf-8", _json_bytes(value))

        def _read_json(self) -> dict[str, object]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > _MAX_BODY:
                raise ValueError("request body is empty or too large")
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("request body must be an object")
            return value

        def _idempotency_key(self) -> str:
            """Every mutating review POST requires an Idempotency-Key with the
            same syntax and ledger behavior as CLI/MCP operation IDs (issue
            #34): identical key + canonical payload replays the original
            response; a changed payload is rejected as operation-id-reused."""
            key = self.headers.get("Idempotency-Key", "")
            if not key or not _IDEMPOTENCY_RE.match(key):
                raise ValueError(
                    "idempotency-key-required: Idempotency-Key header required for "
                    "mutating POSTs (8-128 chars of [A-Za-z0-9_-])"
                )
            return key

        def _post_mutation(self, path: str, payload: dict[str, object], run) -> dict[str, object]:
            """Run one review mutation through the immutable-generation store
            (Writer lane, CAS, durable journal, recovery) and return the
            response data. Replay returns the original committed data."""
            key = self._idempotency_key()
            canonical = canonical_operation_input("review_post", {"path": path, "payload": payload})
            try:
                store = Store.ensure(workdir, operation_id=key, input_sha256=canonical)
            except (StoreError, OSError) as exc:
                raise CollaborationError(
                    getattr(exc, "code", None) or "workdir-unreadable", str(exc)
                ) from exc
            pin = store.pin()

            def adapter(target, tx):
                outcome, data, kind, evidence_payload, diagnostics = run(target)
                return outcome, data, kind, evidence_payload, diagnostics

            envelope = store.mutate(
                operation="review_post",
                operation_id=key,
                canonical=canonical,
                input_sha256=pin["manifest_sha256"],
                expected_generation=pin["generation"],
                run=adapter,
            )
            return dict(envelope["data"])

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            request = urlparse(self.path)
            path = request.path
            query = parse_qs(request.query)
            try:
                if path == "/":
                    self._send(200, "text/html; charset=utf-8", render_html(workdir, server_mode=True).encode("utf-8"))
                elif path == "/api/reviews":
                    review = snapshot(workdir)
                    self._send_json(200, {**review, "session": document_state(workdir)})
                elif path == "/api/document-state":
                    self._send_json(200, document_state(workdir))
                elif path == "/api/document-fragment":
                    fragment = render_document_fragment(workdir)
                    self._send_json(
                        200,
                        {
                            **fragment,
                            "history": review_history(workdir, include_fragments=False),
                            "review": snapshot(workdir),
                            "session": document_state(workdir),
                        },
                    )
                elif path == "/api/review-history":
                    selected = query.get("snapshot", [None])[0]
                    history = review_history(workdir, snapshot_id=selected, include_fragments=True)
                    self._send_json(200, {"history": history})
                elif path == "/health":
                    self._send_json(200, {"ok": True, "service": "docx2typed-review", "workdir": str(workdir)})
                else:
                    self._send_json(404, {"error": "not-found"})
            except Exception as exc:  # noqa: BLE001 - turn local errors into API diagnostics
                self._send_json(500, _error_payload(exc))

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlparse(self.path).path
            try:
                if path == "/api/reviews":
                    payload = self._read_json()
                    data = self._post_mutation(
                        path,
                        payload,
                        lambda target: _review_mutation(
                            target,
                            lambda wd: stage_patch(wd, payload)
                            if payload.get("type") == "patch"
                            else upsert_event(wd, payload),
                            extra=lambda target: {
                                "counts": snapshot(target)["counts"],
                                "session": document_state(target),
                            },
                        ),
                    )
                    self._send_json(200, data)
                elif path == "/api/reviews/patch":
                    payload = self._read_json()
                    data = self._post_mutation(
                        path,
                        payload,
                        lambda target: _review_mutation(
                            target,
                            lambda wd: stage_patch(wd, payload),
                            extra=lambda target: {
                                "counts": snapshot(target)["counts"],
                                "session": document_state(target),
                            },
                        ),
                    )
                    self._send_json(200, data)
                elif path == "/api/reviews/dispatch":
                    data = self._post_mutation(
                        path,
                        {},
                        lambda target: _review_mutation(
                            target,
                            lambda wd: dispatch(wd),
                            extra=lambda target: {
                                "counts": snapshot(target)["counts"],
                                "session": document_state(target),
                            },
                        ),
                    )
                    self._send_json(200, data)
                elif path == "/api/reviews/external-preflight":
                    payload = self._read_json()
                    guard = external_write_guard(
                        workdir,
                        expected_parent_snapshot=str(payload.get("expected_parent_snapshot", "")),
                        operation=str(payload.get("operation", "import")),
                    )
                    self._send_json(200, guard)
                elif path == "/api/reviews/settle":
                    payload = self._read_json()
                    event_ids = payload.get("event_ids")
                    if event_ids is not None and (
                        not isinstance(event_ids, list) or not all(isinstance(item, str) for item in event_ids)
                    ):
                        raise ValueError("event_ids must be a string array")
                    data = self._post_mutation(
                        path,
                        payload,
                        lambda target: _review_mutation(
                            target,
                            lambda wd: settle_decisions(wd, event_ids),
                            extra=lambda target: {
                                "counts": snapshot(target)["counts"],
                                "session": document_state(target),
                            },
                        ),
                    )
                    self._send_json(200, data)
                elif path == "/api/reviews/publish":
                    payload = self._read_json()
                    changed = payload.get("changed_paragraph_ids", [])
                    if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
                        raise ValueError("changed_paragraph_ids must be a string array")
                    data = self._post_mutation(
                        path,
                        payload,
                        lambda target: _review_mutation(
                            target,
                            lambda wd: publish_current(
                                wd,
                                expected_parent_snapshot=str(payload.get("expected_parent_snapshot", "")),
                                origin=str(payload.get("origin", "human_ui")),
                                changed_paragraph_ids=changed,
                                batch_id=str(payload["batch_id"]) if payload.get("batch_id") else None,
                            ),
                            extra=lambda target: {
                                "counts": snapshot(target)["counts"],
                                "session": document_state(target),
                            },
                        ),
                    )
                    self._send_json(200, data)
                else:
                    self._send_json(404, {"error": "not-found"})
            except CollaborationError as exc:
                self._send_json(409, _error_payload(exc))
            except StoreError as exc:
                self._send_json(409, _error_payload(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, _error_payload(exc))
            except Exception as exc:  # noqa: BLE001 - turn local errors into API diagnostics
                self._send_json(500, _error_payload(exc))

        def log_message(self, format: str, *args: object) -> None:
            print(f"[review-server] {self.address_string()} - {format % args}")

    return ReviewHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    network = parser.add_mutually_exclusive_group()
    network.add_argument("--host", help="bind address (default: 127.0.0.1)")
    network.add_argument(
        "--tailscale",
        action="store_true",
        help="bind only to the local Tailscale IPv4 address",
    )
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args(argv)
    workdir = args.workdir.resolve()
    if not (workdir / "typed.md").exists():
        parser.error(f"not a typed workdir: {workdir}")
    if args.tailscale:
        try:
            host = _tailscale_ipv4()
        except RuntimeError as exc:
            parser.error(str(exc))
    else:
        host = args.host or "127.0.0.1"
    server = ThreadingHTTPServer((host, args.port), _handler_for(workdir))
    print(f"review server: http://{host}:{args.port}/")
    print(f"workdir: {workdir}")
    if args.tailscale:
        print("access: Tailscale tailnet only; open the printed URL on a phone in the same tailnet")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nreview server stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
