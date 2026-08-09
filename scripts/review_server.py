"""Serve the document review console and its agent handoff API.

The server binds to localhost by default. The browser writes review drafts to
the workdir inbox, and an explicit dispatch moves them to the MCP-visible
``queued`` state.

Usage: python -m scripts.review_server WORKDIR --port 8876
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .review_collab import CollaborationError, document_state, external_write_guard, publish_current, settle_decisions, stage_patch
    from .review_console import render_document_fragment, render_html, review_history
    from .review_queue import dispatch, snapshot, upsert_event
except ImportError:  # pragma: no cover - direct script invocation fallback
    from review_collab import CollaborationError, document_state, external_write_guard, publish_current, settle_decisions, stage_patch  # type: ignore[no-redef]
    from review_console import render_document_fragment, render_html, review_history  # type: ignore[no-redef]
    from review_queue import dispatch, snapshot, upsert_event  # type: ignore[no-redef]


_MAX_BODY = 256 * 1024


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


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
                self._send_json(500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path = urlparse(self.path).path
            try:
                if path == "/api/reviews":
                    payload = self._read_json()
                    event = stage_patch(workdir, payload) if payload.get("type") == "patch" else upsert_event(workdir, payload)
                    self._send_json(200, {"event": event, "counts": snapshot(workdir)["counts"], "session": document_state(workdir)})
                elif path == "/api/reviews/patch":
                    event = stage_patch(workdir, self._read_json())
                    self._send_json(200, {"event": event, "session": document_state(workdir)})
                elif path == "/api/reviews/dispatch":
                    events = dispatch(workdir)
                    self._send_json(200, {"events": events, "counts": snapshot(workdir)["counts"], "session": document_state(workdir)})
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
                    result = settle_decisions(workdir, event_ids)
                    self._send_json(200, {**result, "session": document_state(workdir)})
                elif path == "/api/reviews/publish":
                    payload = self._read_json()
                    changed = payload.get("changed_paragraph_ids", [])
                    if not isinstance(changed, list) or not all(isinstance(item, str) for item in changed):
                        raise ValueError("changed_paragraph_ids must be a string array")
                    result = publish_current(
                        workdir,
                        expected_parent_snapshot=str(payload.get("expected_parent_snapshot", "")),
                        origin=str(payload.get("origin", "human_ui")),
                        changed_paragraph_ids=changed,
                        batch_id=str(payload["batch_id"]) if payload.get("batch_id") else None,
                    )
                    self._send_json(200, {**result, "session": document_state(workdir)})
                else:
                    self._send_json(404, {"error": "not-found"})
            except CollaborationError as exc:
                self._send_json(409, {"error": str(exc), "code": exc.code})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - turn local errors into API diagnostics
                self._send_json(500, {"error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            print(f"[review-server] {self.address_string()} - {format % args}")

    return ReviewHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args(argv)
    workdir = args.workdir.resolve()
    if not (workdir / "typed.md").exists():
        parser.error(f"not a typed workdir: {workdir}")
    server = ThreadingHTTPServer((args.host, args.port), _handler_for(workdir))
    print(f"review server: http://{args.host}:{args.port}/")
    print(f"workdir: {workdir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nreview server stopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
