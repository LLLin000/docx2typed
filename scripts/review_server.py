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
from urllib.parse import urlparse

try:
    from .review_console import render_html
    from .review_queue import dispatch, snapshot, upsert_event
except ImportError:  # pragma: no cover - direct script invocation fallback
    from review_console import render_html  # type: ignore[no-redef]
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
            path = urlparse(self.path).path
            try:
                if path == "/":
                    self._send(200, "text/html; charset=utf-8", render_html(workdir, server_mode=True).encode("utf-8"))
                elif path == "/api/reviews":
                    self._send_json(200, snapshot(workdir))
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
                    event = upsert_event(workdir, self._read_json())
                    self._send_json(200, {"event": event, "counts": snapshot(workdir)["counts"]})
                elif path == "/api/reviews/dispatch":
                    events = dispatch(workdir)
                    self._send_json(200, {"events": events, "counts": snapshot(workdir)["counts"]})
                else:
                    self._send_json(404, {"error": "not-found"})
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
