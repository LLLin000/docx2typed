"""docx2typed-mcp — MCP server exposing the typed-workdir engine as span-free tools.

The agent-facing surface is visible plain text plus a per-paragraph style
region map. Style ownership is decided by the engine with zero guessing:

- unchanged characters keep their exact style;
- rewritten text inherits the style of the baseline text it replaces — only
  when that range covers a single style region (single-region atomic edit);
- a rewrite covering multiple style regions is rejected with the region
  boundaries and a split-edit suggestion (``mixed-replacement-requires-
  unchanged-text`` / ``unanchored-mixed-rewrite``);
- insertions follow the caret context (left neighbor, paragraph-start right
  neighbor, ``insertion_style`` for empty paragraphs).

Workflow:

    workdir_open -> get_paragraph (style regions are shown by default)
    -> replace_text / insert_paragraph / delete_paragraph (write the draft)
    -> diff_preview (per-hunk style ownership, read-only)
    -> commit_sync (apply the draft, publish canonical state)
    -> build_docx -> verify_output

``revert`` discards the uncommitted draft.

Run as stdio MCP server:

    python -m docx2typed.mcp_server
"""
from __future__ import annotations

import json
import re
import threading
import zipfile
from pathlib import Path
from typing import Any, Callable

try:
    from .edit import (
        PROJECTION_FILE,
        classify_edit_state,
        refresh_edit_projection,
        sync_edit_projection,
    )
    from .edit_sync import (
        _validate_escaped_prose,
        flatten_paragraph,
        plan_sync,
        render_regions_md,
    )
    from .typed_core import (
        InlineNode,
        OpaqueNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedError,
        parse_typed,
    )
    from .typed_docx import ValidationError, build_workdir, validate_workdir, verify_workdir
    from .review_collab import CollaborationError, document_state, external_write_guard, preflight, publish_current, settle_decisions, settlement_plan
    from .review_queue import acknowledge as acknowledge_review, snapshot as review_snapshot, update_event as update_review_event
    from .protocol import (
        ProtocolMismatch,
        base_evidence_payload,
        canonical_operation_input,
        derived_workdir_manifest,
        diagnostic,
        domain_code_from_message,
        domain_diagnostic,
        engine_descriptor,
        file_sha256,
        mcp_result,
        negotiate,
        new_operation_id,
        operation_ledger,
        operation_ledger_path,
        publish_run_evidence,
        result_envelope,
        run_evidence,
        schema_bundle,
        semantic_sha256,
        typed_path,
    )
    from .store import Store, StoreError, has_store, read_root
except ImportError:  # direct script execution has no package context.
    from edit import (
        PROJECTION_FILE,
        classify_edit_state,
        refresh_edit_projection,
        sync_edit_projection,
    )
    from edit_sync import (
        _validate_escaped_prose,
        flatten_paragraph,
        plan_sync,
        render_regions_md,
    )
    from typed_core import (
        InlineNode,
        OpaqueNode,
        RangeNode,
        StyleRegistry,
        TextNode,
        TypedError,
        parse_typed,
    )
    from typed_docx import ValidationError, build_workdir, validate_workdir, verify_workdir
    from review_collab import CollaborationError, document_state, external_write_guard, preflight, publish_current, settle_decisions, settlement_plan  # type: ignore[no-redef]
    from review_queue import acknowledge as acknowledge_review, snapshot as review_snapshot, update_event as update_review_event
    from protocol import (
        ProtocolMismatch,
        base_evidence_payload,
        canonical_operation_input,
        derived_workdir_manifest,
        diagnostic,
        domain_code_from_message,
        domain_diagnostic,
        engine_descriptor,
        file_sha256,
        mcp_result,
        negotiate,
        new_operation_id,
        operation_ledger,
        operation_ledger_path,
        publish_run_evidence,
        result_envelope,
        run_evidence,
        schema_bundle,
        semantic_sha256,
        typed_path,
    )
    from store import Store, StoreError, has_store, read_root  # type: ignore[no-redef]
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult


class ToolError(TypedError):
    """Structured tool failure: ``code: message``; code is a stable diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.detail = message


# --------------------------------------------------------------------------
# Session
# --------------------------------------------------------------------------

class WorkdirSession:
    def __init__(self) -> None:
        self.workdir: Path | None = None
        self.lock = threading.RLock()
        self.author: str | None = None
        self.track_override: bool | None = None
        self.mode: str | None = None

    def require(self) -> Path:
        if self.workdir is None:
            raise ToolError("workdir-not-open", "no workdir open; call workdir_open first")
        return self.workdir


def _agent_preflight(workdir: Path) -> dict[str, Any]:
    result = preflight(workdir)
    if not result["ready"]:
        raise ToolError(
            "agent-preflight-required",
            json.dumps({"reasons": result["reasons"], "queued_events": result["queued_events"]}, ensure_ascii=False),
        )
    return result


def _domain_code(message: str) -> str:
    """Stable diagnostic code from a ValidationError message prefix
    (``kebab-code: detail``); falls back to ``workdir-invalid`` when the
    prefix is not a registered code. Shared with the CLI seam (issue #53)."""
    return domain_code_from_message(message)


def _failure_result(operation: str, code: str, message: str) -> CallToolResult:
    envelope = result_envelope(
        operation,
        "failure",
        data={},
        diagnostics=[domain_diagnostic(code, message)],
    )
    return mcp_result(envelope, is_error=True)


def _evidence_publish_failed(
    operation: str, operation_id: str, evidence_path: Path, exc: OSError
) -> CallToolResult:
    """Structured ``evidence-publish-failed`` Result.

    The diagnostic detail names the exception class and the fixed evidence
    path — never the transient mkstemp temp filename embedded in
    ``str(exc)`` — so every independently-built attempt (first run or
    pending-repair retry) reports the byte-identical diagnostic."""
    envelope = result_envelope(
        operation,
        "failure",
        data={"operation_id": operation_id},
        diagnostics=[
            domain_diagnostic(
                "evidence-publish-failed",
                f"required run evidence could not be published: {type(exc).__name__}: {evidence_path}",
            )
        ],
    )
    return mcp_result(envelope, is_error=True)


def _mutation_tool(
    operation_id: str,
    operation: str,
    canonical_args: dict[str, Any],
    anchor: Path,
    *,
    directory: bool,
    evidence_path: Path,
    run: Callable[..., tuple[str, dict[str, Any], str, dict[str, Any], list[dict[str, Any]]]],
    store_workdir: Path | None = None,
    store_generation: bool = True,
) -> CallToolResult:
    """Run one mutating tool under the Operation-ID/Evidence contract and
    return the common Result envelope as structuredContent.

    ``run`` returns (outcome, data, kind, payload, diagnostics); domain
    failures become ``isError`` Results carrying Diagnostics (no exception
    escapes the public tool seam). Replay with the identical operation_id +
    canonical input returns the original Result without a second effect; a
    changed input fails ``operation-id-reused``. With ``store_workdir`` the
    mutation runs through the immutable-generation store (Writer lane, CAS,
    durable journals, startup recovery, atomic external publication) and
    ``run`` receives the fresh generation directory (or the pinned generation
    for external-only publication)."""
    if not operation_id:
        return _failure_result(
            operation, "operation-id-required", "mutating calls require a caller-supplied operation_id"
        )
    op_id = str(operation_id)
    canonical = canonical_operation_input(operation, canonical_args)
    store = None
    if store_workdir is not None:
        if not has_store(store_workdir):
            try:
                Store.ensure(store_workdir, operation_id=op_id, input_sha256=canonical)
            except (StoreError, OSError) as exc:
                return _failure_result(
                    operation, getattr(exc, "code", None) or "workdir-unreadable", str(exc)
                )
        try:
            store = Store.open(store_workdir)
        except (StoreError, OSError) as exc:
            return _failure_result(
                operation, getattr(exc, "code", None) or "workdir-unreadable", str(exc)
            )
    ledger_anchor = anchor
    ledger_directory = directory
    if store is not None:
        # Replay lookup must hit the generation the record was written under:
        # the pointer may have advanced past the committing generation, so
        # search every generation, not just the current pin.
        record, corrupt_path = store.lookup_ledger(
            op_id, generation=store_generation, anchor=anchor, directory=directory
        )
    else:
        record = operation_ledger.lookup_persisted(op_id, ledger_anchor, directory=ledger_directory)
        corrupt_path = None
        if record is None:
            corrupt_path = operation_ledger.corrupt_persisted(
                op_id, ledger_anchor, directory=ledger_directory
            )
    if record is None and corrupt_path is not None:
        # Corrupt persisted row for this operation_id: the mutation may have
        # completed (e.g. a lost pending marker), so never rerun. Fail closed
        # with a structured Result naming the exact ledger file; the corrupt
        # row stays for inspection.
        return _failure_result(
            operation,
            "operation-ledger-invalid",
            f"ledger record for operation_id {op_id!r} is corrupt; "
            f"repair or remove {corrupt_path}",
        )
    if record is not None:
        if record["input_sha256"] == canonical:
            envelope = record.get("envelope")
            if isinstance(envelope, dict) and envelope.get("outcome") in (
                "success",
                "failure",
                "partial",
            ):
                if record.get("pending") is not True:
                    return mcp_result(envelope, is_error=(envelope["outcome"] != "success"))
                # Pending record carrying the prepared exact envelope: the
                # effect already completed (run() returns only after the
                # effect landed), so never rerun. Repair the required
                # evidence sidecar from the envelope's sole evidence, then
                # upgrade the record without changing the envelope so every
                # replay stays byte-exact. A repair failure keeps the record
                # pending and reports evidence-publish-failed.
                stored_evidence = envelope.get("evidence") or []
                if len(stored_evidence) != 1:
                    return _failure_result(
                        operation,
                        "operation-ledger-invalid",
                        f"ledger record for operation_id {op_id!r} carries a prepared "
                        f"envelope without exactly one evidence record; repair or "
                        f"remove {operation_ledger_path(ledger_anchor, directory=ledger_directory)}",
                    )
                try:
                    candidate = json.loads(evidence_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    candidate = None
                if candidate != stored_evidence[0]:
                    try:
                        publish_run_evidence(evidence_path, stored_evidence[0])
                    except OSError as exc:
                        return _evidence_publish_failed(
                            operation, op_id, evidence_path, exc
                        )
                operation_ledger.record(op_id, canonical, envelope, ledger_anchor, directory=ledger_directory)
                return mcp_result(envelope, is_error=(envelope["outcome"] != "success"))
            # Missing/pending envelope: the operation never completed. Fall
            # through and rerun the idempotent operation (records are shape-
            # validated at read time, so a corrupt record can never replay).
        return _failure_result(
            operation,
            "operation-id-reused",
            f"operation_id {op_id!r} was already used with different canonical input",
        )
    if store is not None:
        return _store_mutation_tool(
            operation,
            op_id,
            canonical,
            store,
            run,
            evidence_path,
            generation=store_generation,
            anchor=anchor,
            directory=directory,
        )
    try:
        outcome, data, kind, payload, diagnostics = run(Path(anchor))
    except ToolError as exc:
        return _failure_result(operation, exc.code, exc.detail)
    except CollaborationError as exc:
        return _failure_result(operation, exc.code, exc.detail)
    except (TypedError, ValidationError) as exc:
        return _failure_result(operation, _domain_code(str(exc)), str(exc))
    except zipfile.BadZipFile as exc:
        return _failure_result(operation, "workdir-invalid", str(exc))
    except OSError as exc:
        return _failure_result(operation, "workdir-unreadable", str(exc))
    except (KeyError, ValueError) as exc:
        return _failure_result(operation, "workdir-invalid", str(exc))
    evidence = run_evidence(
        operation, outcome, kind=kind, operation_id=op_id, payload=payload
    )
    data = {"operation_id": op_id, **data}
    envelope = result_envelope(
        operation,
        outcome,
        data=data,
        diagnostics=diagnostics,
        evidence=[evidence],
    )
    # Persist the exact prepared envelope as the pending record BEFORE the
    # sidecar publish: a crash between here and the completed upgrade leaves
    # a retryable record whose envelope is the byte-exact original Result.
    operation_ledger.record(
        op_id, canonical, envelope, ledger_anchor, directory=ledger_directory, pending=True
    )
    try:
        publish_run_evidence(evidence_path, evidence)
    except OSError as exc:
        # Keep the pending record carrying the prepared envelope unchanged: a
        # retry republishes the evidence and upgrades; the prepared envelope
        # is never replaced by this failure Result.
        return _evidence_publish_failed(operation, op_id, evidence_path, exc)
    operation_ledger.record(op_id, canonical, envelope, ledger_anchor, directory=ledger_directory)
    return mcp_result(envelope, is_error=(outcome != "success"))


def _store_mutation_tool(
    operation: str,
    op_id: str,
    canonical: str,
    store: "Store",
    run: Callable[..., tuple[str, dict[str, Any], str, dict[str, Any], list[dict[str, Any]]]],
    evidence_path: Path,
    *,
    generation: bool,
    anchor: Path,
    directory: bool,
) -> CallToolResult:
    """Run one mutating tool through the immutable-generation store. The store
    owns evidence, ledger, journal, pointer, and external publication
    durability; the caller only wraps the committed envelope."""
    try:
        pin = store.pin()
        expected_generation = pin["generation"]
        expected_manifest = pin["manifest_sha256"]

        def adapter(target: Path, tx: Any) -> tuple[Any, ...]:
            result = run(target, tx)
            outcome, data, kind, payload, diagnostics = result
            return outcome, data, kind, payload, diagnostics

        envelope = store.mutate(
            operation=operation,
            operation_id=op_id,
            canonical=canonical,
            input_sha256=expected_manifest or canonical,
            expected_generation=expected_generation,
            run=adapter,
            generation=generation,
            ledger_anchor=None if generation else anchor,
            ledger_directory=directory if not generation else True,
            evidence_path=None if generation else evidence_path,
        )
    except StoreError as exc:
        return _failure_result(operation, exc.code, str(exc))
    except ToolError as exc:
        return _failure_result(operation, exc.code, exc.detail)
    except CollaborationError as exc:
        return _failure_result(operation, exc.code, exc.detail)
    except (TypedError, ValidationError) as exc:
        return _failure_result(operation, _domain_code(str(exc)), str(exc))
    except zipfile.BadZipFile as exc:
        return _failure_result(operation, "workdir-invalid", str(exc))
    except OSError as exc:
        return _failure_result(operation, "workdir-unreadable", str(exc))
    except (KeyError, ValueError) as exc:
        return _failure_result(operation, "workdir-invalid", str(exc))
    return mcp_result(envelope, is_error=(envelope["outcome"] != "success"))


def _workdir_manifest_sha256(workdir: Path) -> str:
    return semantic_sha256(derived_workdir_manifest(workdir))

session = WorkdirSession()
mcp = FastMCP("docx2typed")

_ESC_LBRACKET = "\\u27E6"
_ESC_RBRACKET = "\\u27E7"
_ESC_BACKSLASH = "\\\\"


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _escape_prose(text: str) -> str:
    out: list[str] = []
    for char in text:
        if char == "\\":
            out.append(_ESC_BACKSLASH)
        elif char == "\u27e6":
            out.append(_ESC_LBRACKET)
        elif char == "\u27e7":
            out.append(_ESC_RBRACKET)
        else:
            out.append(char)
    return "".join(out)


def _split_chunks(body: str) -> list[tuple[str, str]]:
    """Split a draft body into ("text", raw-prose) and ("token", placeholder)
    chunks. Text chunks keep their escaped form; tokens are atomic."""
    chunks: list[tuple[str, str]] = []
    cursor = 0
    while True:
        start = body.find("\u27e6", cursor)
        if start < 0:
            if body[cursor:]:
                chunks.append(("text", body[cursor:]))
            return chunks
        if start > cursor:
            chunks.append(("text", body[cursor:start]))
        end = body.find("\u27e7", start + 1)
        if end < 0:
            raise ToolError("edit-grammar-invalid", "unclosed placeholder in draft")
        chunks.append(("token", body[start : end + 1]))
        cursor = end + 1


def _token_marker(kind: str) -> str:
    return {"tab": "\u21b9", "br": "\u21b5", "cr": "\u21b5"}.get(kind, f"\u27e6{kind}\u27e7")


def _visible_text(body: str) -> str:
    out: list[str] = []
    for kind, raw in _split_chunks(body):
        if kind == "token":
            match = re.match(r"\u27e6(token|range-start|range-end)(.*)", raw)
            if match:
                keyword, attrs = match.group(1), match.group(2)
                kind_match = re.search(r'kind="([^"]+)"', attrs)
                if keyword == "range-end":
                    out.append("\u27e7")
                elif kind_match:
                    out.append(_token_marker(kind_match.group(1)))
                else:
                    out.append("\u27e6?\u27e7")
            else:
                out.append("\u27e6?\u27e7")
        else:
            out.append(_validate_escaped_prose(raw))
    return "".join(out)


def _replace_in_body(
    body: str,
    old: str,
    new: str,
    paragraph_id: str,
    *,
    start_offset: int | None = None,
) -> str:
    """Replace one visible range, optionally anchored by paragraph offset."""
    if "\u27e6" in old or "\u27e7" in old:
        raise ToolError(
            "text-not-found",
            f"{paragraph_id}: old must be visible text without placeholder markers",
        )
    matches = 0
    cursor = 0
    out: list[str] = []
    for kind, raw in _split_chunks(body):
        if kind == "token":
            out.append(raw)
            continue
        visible = _validate_escaped_prose(raw)
        if start_offset is None:
            if old in visible:
                matches += 1
                out.append(_escape_prose(visible.replace(old, new, 1)))
            else:
                out.append(raw)
        else:
            local_start = start_offset - cursor
            anchored = (
                matches == 0
                and 0 <= local_start <= len(visible)
                and visible[local_start:local_start + len(old)] == old
            )
            if anchored:
                matches = 1
                out.append(_escape_prose(
                    visible[:local_start] + new + visible[local_start + len(old):]
                ))
            else:
                out.append(raw)
        cursor += len(visible)
    if matches == 0:
        raise ToolError("text-not-found", f"{paragraph_id}: text {old!r} not found at the target offset")
    if start_offset is None and matches > 1:
        raise ToolError(
            "text-ambiguous",
            f"{paragraph_id}: text {old!r} appears {matches} times; provide a longer unique context",
        )
    return "".join(out)


def _paragraph_blocks(text: str) -> tuple[str, list[str]]:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines or not lines[0].startswith("<!--@edit"):
        raise ToolError("edit-header-missing", "edit.md must start with an @edit header")
    header = lines[0]
    blocks: list[str] = []
    current: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("<!--@p") or stripped.startswith("<!--@new") or stripped.startswith("<!--@delete"):
            if current:
                blocks.append("\n".join(current))
                current = []
            current.append(line)
        elif stripped:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return header, blocks


def _read_edit(workdir: Path) -> tuple[str, list[str]]:
    return _paragraph_blocks((workdir / PROJECTION_FILE).read_text(encoding="utf-8"))


def _write_edit(workdir: Path, header: str, blocks: list[str]) -> None:
    (workdir / PROJECTION_FILE).write_text(
        header + "\n\n" + "\n\n".join(blocks) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _find_block(blocks: list[str], prefix: str, paragraph_id: str) -> int:
    for index, block in enumerate(blocks):
        if block.startswith(f'<!--@{prefix} id="{paragraph_id}"'):
            return index
    raise ToolError("paragraph-not-found", f"paragraph {paragraph_id} not found in the draft")


def _block_body(block: str) -> str:
    return "\n".join(block.splitlines()[1:]) if "\n" in block else ""


def _draft_paragraph_state(workdir: Path, paragraph_id: str, mode: str | None = None) -> tuple[list[str], list[str]]:
    """Current (visible-unit texts, styles) of a draft paragraph.

    Uses the sync engine's dry-run so the regions reflect any uncommitted
    edits, not just the committed typed state. ``mode`` carries the session
    edit mode (track/direct); without it the engine re-infers from the
    source signals, which turns ambiguous for documents with pending
    revisions but trackChanges off and wrongly rejects dirty-draft edits.
    """
    typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
    state = classify_edit_state(workdir)
    if state["state"] == "clean":
        paragraph = next((p for p in typed.paragraphs if p.paragraph_id == paragraph_id), None)
        if paragraph is None:
            raise ToolError("paragraph-not-found", f"paragraph {paragraph_id} not in typed.md")
        units = flatten_paragraph(paragraph)
    else:
        from .edit import parse_edit_projection

        projection = parse_edit_projection((workdir / PROJECTION_FILE).read_text(encoding="utf-8"))
        format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
        try:
            revision_ctx = None
            if mode == "track":
                from .edit import _build_revision_context

                revision_ctx = _build_revision_context(
                    typed, format_data, workdir,
                    mode="track", author=session.author or "Unknown", author_source="session",
                )
            plan = plan_sync(typed, projection, format_data, mode=mode, revision_ctx=revision_ctx)
        except ValidationError as exc:
            raise ToolError("draft-invalid", f"current draft cannot be applied: {exc}") from exc
        paragraph = next((p for p in plan.document.paragraphs if p.paragraph_id == paragraph_id), None)
        if paragraph is None:
            raise ToolError("paragraph-not-found", f"paragraph {paragraph_id} not in the draft")
        units = flatten_paragraph(paragraph)
    texts = [unit.value[1] for unit in units if not unit.token]
    styles = [unit.style for unit in units if not unit.token]
    return texts, styles


def _check_single_region(
    workdir: Path,
    paragraph_id: str,
    old: str,
    texts: list[str],
    styles: list[str],
) -> tuple[int, int]:
    """Locate ``old`` in the paragraph's visible units and require it to cover
    exactly one style region. Returns the unit index range."""
    text = "".join(texts)
    count = text.count(old)
    if count == 0:
        raise ToolError("text-not-found", f"{paragraph_id}: text {old!r} not found in paragraph")
    if count > 1:
        raise ToolError(
            "text-ambiguous",
            f"{paragraph_id}: text {old!r} appears {count} times; provide a longer unique context",
        )
    start = text.index(old)
    end = start + len(old)
    offsets: list[int] = []
    cursor = 0
    for unit_text in texts:
        offsets.append(cursor)
        cursor += len(unit_text)
    offsets.append(cursor)
    i1 = max(i for i, offset in enumerate(offsets) if offset <= start)
    i2 = max(i for i, offset in enumerate(offsets) if offset < end)
    covered = set(styles[i1 : i2 + 1])
    if len(covered) > 1:
        regions = _region_labels(texts, styles)
        raise ToolError(
            "cross-region-text",
            f"{paragraph_id}: {old!r} covers multiple style regions: "
            + " / ".join(regions)
            + ". Edit each region separately (see get_paragraph styles), "
            f"e.g. replace_text({paragraph_id}, '<region-a text>', ...) then "
            f"replace_text({paragraph_id}, '<region-b text>', ...).",
        )
    return i1, i2
def _fnv1a_utf16(text: str) -> str:
    """Match the browser's UTF-16 FNV-1a selection fingerprint."""
    digest = 2166136261
    encoded = text.encode("utf-16-le")
    for index in range(0, len(encoded), 2):
        digest ^= encoded[index] | (encoded[index + 1] << 8)
        digest = (digest * 16777619) & 0xFFFFFFFF
    return f"fnv1a-{digest:08x}"


def _validate_collab_patch_target(workdir: Path, event: dict[str, Any]) -> None:
    """Fail closed unless a semantic patch still addresses the exact text."""
    paragraph_id = str(event.get("paragraph_id") or event.get("target", {}).get("paragraph_id") or "")
    target = event.get("target")
    if not paragraph_id or not isinstance(target, dict):
        raise ToolError("patch-target", "semantic patch needs a paragraph and target")
    texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
    paragraph_text = "".join(texts)
    start = target.get("start_offset")
    end = target.get("end_offset")
    before = str(event.get("before", ""))
    expected = str(target.get("expected_text", ""))
    if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
        raise ToolError("patch-range", "semantic patch offsets are invalid")
    if before != expected or paragraph_text[start:end] != before:
        raise ToolError("patch-precondition", f"{paragraph_id}: expected text no longer matches the current snapshot")
    if paragraph_text[max(0, start - 100):start] != str(target.get("left_context", "")):
        raise ToolError("patch-context-mismatch", f"{paragraph_id}: left context no longer matches the current snapshot")
    if paragraph_text[end:end + 100] != str(target.get("right_context", "")):
        raise ToolError("patch-context-mismatch", f"{paragraph_id}: right context no longer matches the current snapshot")
    paragraph_fingerprint = str(target.get("paragraph_fingerprint", ""))
    if paragraph_fingerprint and paragraph_fingerprint != _fnv1a_utf16(paragraph_text):
        raise ToolError("patch-fingerprint-mismatch", f"{paragraph_id}: paragraph fingerprint changed")
    region_fingerprint = str(target.get("region_fingerprint", ""))
    if region_fingerprint and region_fingerprint != _fnv1a_utf16(before):
        raise ToolError("patch-fingerprint-mismatch", f"{paragraph_id}: selected region fingerprint changed")
    style_region_ids = [str(item) for item in (target.get("style_region_ids") or [])]
    current_style_ids = list(dict.fromkeys(styles))
    if style_region_ids and style_region_ids != current_style_ids:
        raise ToolError("patch-style-mismatch", f"{paragraph_id}: style regions changed; re-read the paragraph")
    if before:
        offsets: list[int] = []
        cursor = 0
        for unit_text in texts:
            offsets.append(cursor)
            cursor += len(unit_text)
        offsets.append(cursor)
        i1 = max(i for i, offset in enumerate(offsets) if offset <= start)
        i2 = max(i for i, offset in enumerate(offsets) if offset < end)
        covered = set(styles[i1:i2 + 1])
        if len(covered) > 1:
            raise ToolError("cross-region-text", f"{paragraph_id}: patch covers multiple style regions")

def _apply_patch_to_draft(
    workdir: Path,
    event: dict[str, Any],
    *,
    validate: bool = True,
) -> None:
    if validate:
        _validate_collab_patch_target(workdir, event)
    paragraph_id = str(event["paragraph_id"])
    header, blocks = _read_edit(workdir)
    index = _find_block(blocks, "p", paragraph_id)
    marker = blocks[index].splitlines()[0]
    body = _block_body(blocks[index])
    target = event["target"]
    new_body = _replace_in_body(
        body,
        str(event["before"]),
        str(event["after"]),
        paragraph_id,
        start_offset=int(target["start_offset"]),
    )
    blocks[index] = marker + ("\n" + new_body if new_body else "")
    _write_edit(workdir, header, blocks)
    _refresh_regions(workdir)



def _style_info(workdir: Path, style_id: str) -> dict[str, Any]:
    registry = StyleRegistry.from_json(
        json.loads((workdir / "styles.json").read_text(encoding="utf-8"))
    )
    style = registry.styles.get(style_id)
    return {
        "style_id": style_id,
        "description": style.label if style else style_id,
        "rpr": style.rpr if style else None,
    }


def _merge_regions(texts: list[str], styles: list[str]) -> list[tuple[str, str]]:
    regions: list[tuple[str, str]] = []
    for text, style in zip(texts, styles):
        if regions and regions[-1][1] == style:
            regions[-1] = (regions[-1][0] + text, style)
        else:
            regions.append((text, style))
    return regions


def _resolve_region(edit: dict[str, Any], regions: list[tuple[str, str]], edit_no: int) -> int:
    if "region" in edit:
        index = edit["region"]
        if not isinstance(index, int) or index < 0 or index >= len(regions):
            raise ToolError(
                "region-out-of-range",
                f"edit {edit_no}: region {index} out of range (paragraph has {len(regions)} "
                "regions); re-read regions.md",
            )
        return index
    if "text" in edit:
        anchor = edit["text"]
        style_id = edit.get("style_id")
        matches = [
            index
            for index, region in enumerate(regions)
            if region[0] == anchor and (style_id is None or region[1] == style_id)
        ]
        if not matches:
            raise ToolError(
                "text-not-found",
                f"edit {edit_no}: region text {anchor!r} not found; re-read regions.md",
            )
        if len(matches) > 1:
            raise ToolError(
                "text-ambiguous",
                f"edit {edit_no}: region text {anchor!r} appears {len(matches)} times; "
                "add style_id to disambiguate",
            )
        return matches[0]
    raise ToolError("invalid-edit", f"edit {edit_no}: must specify 'region' index or 'text' anchor")


def _apply_batch_to_body(
    body: str,
    regions: list[tuple[str, str]],
    resolved: list[tuple[int, str | None, str]],
) -> str:
    """Apply one edit per region to a draft body.

    Text chunks and regions must tile the same text; each text chunk is
    rebuilt from the (possibly edited) region texts it covers, in order.
    Tokens keep their positions.
    """
    chunks = _split_chunks(body)
    chunk_texts = [_validate_escaped_prose(raw) for kind, raw in chunks if kind == "text"]
    if "".join(chunk_texts) != "".join(region[0] for region in regions):
        raise ToolError(
            "draft-invalid",
            "draft text structure does not match the region view; re-read regions.md",
        )
    region_bounds: list[tuple[int, int]] = []
    cursor = 0
    for region_text, _ in regions:
        region_bounds.append((cursor, cursor + len(region_text)))
        cursor += len(region_text)
    chunk_bounds: list[tuple[int, int]] = []
    cursor = 0
    for chunk_text in chunk_texts:
        chunk_bounds.append((cursor, cursor + len(chunk_text)))
        cursor += len(chunk_text)

    def chunk_regions(start: int, end: int) -> tuple[int, int]:
        first = next((i for i, (rs, _) in enumerate(region_bounds) if rs < end), 0)
        last = next((i for i in range(len(region_bounds) - 1, -1, -1) if region_bounds[i][1] > start), 0)
        return first, last

    new_region_texts = [region[0] for region in regions]
    for region_index, old, new in resolved:
        if old is None:
            new_region_texts[region_index] = new
            continue
        visible = new_region_texts[region_index]
        count = visible.count(old)
        if count == 0:
            raise ToolError(
                "text-not-found",
                f"edit on region {region_index}: text {old!r} not found in that region",
            )
        if count > 1:
            raise ToolError(
                "text-ambiguous",
                f"edit on region {region_index}: text {old!r} appears {count} times in "
                "the region; provide a longer context",
            )
        new_region_texts[region_index] = visible.replace(old, new, 1)
    out: list[str] = []
    text_index = 0
    for kind, raw in chunks:
        if kind == "token":
            out.append(raw)
            continue
        first, last = chunk_regions(*chunk_bounds[text_index])
        text_index += 1
        out.append(_escape_prose("".join(new_region_texts[first : last + 1])))
    return "".join(out)


def _refresh_regions(workdir: Path) -> None:
    """Best-effort rewrite of regions.md from the current draft (dry-run)."""
    try:
        typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
        state = classify_edit_state(workdir)
        if state["state"] == "clean":
            document = typed
        else:
            from .edit import parse_edit_projection

            projection = parse_edit_projection((workdir / PROJECTION_FILE).read_text(encoding="utf-8"))
            format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
            plan = plan_sync(typed, projection, format_data)
            document = plan.document
        styles = StyleRegistry.from_json(
            json.loads((workdir / "styles.json").read_text(encoding="utf-8"))
        )
        (workdir / "regions.md").write_text(
            render_regions_md(document, styles), encoding="utf-8", newline="\n"
        )
    except Exception:
        pass  # derived view; edit.md remains the source of truth


def _region_labels(texts: list[str], styles: list[str]) -> list[str]:
    regions: list[tuple[str, str]] = []
    for unit_text, style in zip(texts, styles):
        if regions and regions[-1][1] == style:
            regions[-1] = (regions[-1][0] + unit_text, style)
        else:
            regions.append((unit_text, style))
    return [f"{text}[{style[:8]}]" for text, style in regions]


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@mcp.tool()
def engine_info() -> dict[str, Any]:
    """Return the Protocol-major-1 engine descriptor before any workdir opens."""
    return engine_descriptor()


def workdir_open(workdir: str, author: str | None = None, track: bool | None = None) -> str:
    """Open a typed workdir as the session document; validates it and reports
    its freshness state and effective edit mode. Call once before any other
    tool. ``author`` sets the session revision author (fallback:
    $DOCX2TYPED_AUTHOR, then "Unknown"). ``track`` explicitly selects
    tracked (True) or direct (False) edits; None infers from the three-field
    state (source_track_enabled + pending revisions)."""
    with session.lock:
        path = Path(workdir).resolve()
        if not path.is_dir() or not (path / "typed.md").exists():
            raise ToolError("workdir-not-found", f"not a typed workdir: {path}")
        validate_workdir(path)
        state = classify_edit_state(path)
        format_data = json.loads((path / "format.json").read_text(encoding="utf-8"))
        typed = parse_typed((path / "typed.md").read_text(encoding="utf-8"))
        from .edit_sync import _document_has_revisions
        from .typed_core import effective_edit_mode

        mode = effective_edit_mode(
            source_track_enabled=bool(format_data.get("source_track_enabled")),
            has_pending_revisions=_document_has_revisions(typed),
            explicit=("track" if track else "direct") if track is not None else None,
        )
        collaboration = document_state(path)
        session.workdir = path
        session.author = author
        session.track_override = track
        session.mode = mode
        return _json(
            {
                "workdir": str(path),
                "state": state["state"],
                "edit_mode": mode,
                "author": author,
                "paragraphs": len(typed.paragraphs),
                "current_snapshot": collaboration["current_snapshot"],
                "staged_snapshot": collaboration["staged_snapshot"],
                "current_matches_filesystem": collaboration["current_matches_filesystem"],
            }
        )


@mcp.tool(name="workdir_open")
def _workdir_open_result(
    workdir: str,
    author: str | None = None,
    track: bool | None = None,
    contract_ranges: dict[str, dict[str, int]] | None = None,
    supported_features: list[str] | None = None,
    required_features: list[str] | None = None,
) -> dict[str, Any]:
    """Negotiate Protocol major 1, then open one validated workdir for this connection."""
    try:
        negotiate(contract_ranges, supported_features, required_features)
    except ProtocolMismatch as exc:
        envelope = result_envelope(
            "workdir_open",
            "failure",
            diagnostics=[
                diagnostic(
                    exc.code,
                    str(exc),
                    details=exc.details,
                    next_actions=["upgrade the incompatible client or engine"],
                )
            ],
        )
        return mcp_result(envelope, is_error=True)  # type: ignore[return-value]
    with session.lock:
        if session.workdir is not None:
            envelope = result_envelope(
                "workdir_open",
                "failure",
                diagnostics=[
                    diagnostic(
                        "workdir-already-open",
                        "this MCP connection already has an open workdir",
                    )
                ],
            )
            return mcp_result(envelope, is_error=True)  # type: ignore[return-value]
        try:
            manifest = derived_workdir_manifest(workdir)
            opened = json.loads(workdir_open(workdir, author=author, track=track))
        except ToolError as exc:
            failure = diagnostic(exc.code, exc.detail)
        except FileNotFoundError as exc:
            failure = diagnostic("workdir-not-found", str(exc))
        except PermissionError as exc:
            failure = diagnostic("workdir-unreadable", str(exc))
        except (zipfile.BadZipFile, ValidationError, TypedError) as exc:
            failure = diagnostic(_domain_code(str(exc)), str(exc))
        except OSError as exc:
            failure = diagnostic("workdir-unreadable", str(exc))
        else:
            data = {
                "session": {
                    "schema": "docx2typed-session-descriptor-1",
                    "workdir": typed_path(opened["workdir"]),
                    "workdir_manifest_sha256": semantic_sha256(manifest),
                    "freshness": opened["state"],
                    "effective_mode": opened["edit_mode"],
                    "author": opened["author"],
                    "paragraphs": opened["paragraphs"],
                    "snapshot": {
                        "current": opened["current_snapshot"],
                        "staged": opened["staged_snapshot"],
                    },
                    "cas": {
                        "current_matches_filesystem": opened["current_matches_filesystem"],
                    },
                    "supported_tools": engine_descriptor()["tools"],
                }
            }
            envelope = result_envelope("workdir_open", "success", data=data)
            return mcp_result(envelope)  # type: ignore[return-value]
        envelope = result_envelope(
            "workdir_open",
            "failure",
            diagnostics=[failure],
        )
        return mcp_result(envelope, is_error=True)  # type: ignore[return-value]


@mcp.tool()
def workdir_status() -> str:
    """Freshness state of the opened workdir: clean, dirty, stale-clean, or conflict."""
    with session.lock:
        workdir = session.require()
        state = classify_edit_state(workdir)
        return _json({"state": state["state"], "edit_body_sha256": state["edit_body_sha256"]})


@mcp.tool()
def list_paragraphs() -> str:
    """List draft paragraphs: id, visible-text summary, token count, deletions."""
    with session.lock:
        workdir = session.require()
        header, blocks = _read_edit(workdir)
        paragraphs: list[dict[str, Any]] = []
        for block in blocks:
            marker = block.splitlines()[0].strip()
            match = re.match(r'<!--@(p|new) (?:id|temp)="([^"]+)"', marker)
            if match:
                body = _block_body(block)
                visible = _visible_text(body)
                paragraphs.append(
                    {
                        "id": match.group(2),
                        "kind": match.group(1),
                        "summary": visible[:60],
                        "chars": len(visible),
                        "tokens": body.count("\u27e6"),
                        "deleted": False,
                    }
                )
                continue
            match = re.match(r'<!--@delete id="([^"]+)"', marker)
            if match:
                paragraphs.append(
                    {"id": match.group(1), "kind": "delete", "summary": "[deleted]", "chars": 0, "tokens": 0, "deleted": True}
                )
        return _json({"paragraphs": paragraphs})


@mcp.tool()
def get_paragraph(paragraph_id: str) -> str:
    """Read one paragraph: the draft text and its style regions. Editing is
    region-scoped — replace_text rejects old text spanning regions, and
    batch_edit addresses regions by index — so use the styles array (or
    regions.md) to plan separate edits per region. style_id is authoritative:
    equal style_id = identical formatting; rpr holds the full canonical XML
    (translate it with docs/rpr-reference.md)."""
    with session.lock:
        workdir = session.require()
        header, blocks = _read_edit(workdir)
        index = _find_block(blocks, "p", paragraph_id)
        body = _block_body(blocks[index])
        texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
        regions: list[dict[str, Any]] = []
        for unit_text, style in zip(texts, styles):
            if regions and regions[-1]["style_id"] == style:
                regions[-1]["text"] += unit_text
            else:
                regions.append({"text": unit_text, **_style_info(workdir, style)})
        return _json(
            {
                "paragraph_id": paragraph_id,
                "text": body,
                "plain": _visible_text(body),
                "tokens": body.count("\u27e6"),
                "styles": regions,
            }
        )


@mcp.tool()
def replace_text(paragraph_id: str, old: str, new: str, *, operation_id: str) -> CallToolResult:
    """Replace exactly one occurrence of visible text in a paragraph draft.

    Contract: ``old`` must be unique in the paragraph AND cover a single
    style region (see get_paragraph styles). Text crossing style regions is
    rejected with the region boundaries — edit each region separately, e.g.
    replace_text(P0, '智能响应', '新词') then replace_text(P0, 'ABC', 'XYZ').
    Style ownership is decided by the engine with zero guessing: the region's
    style is preserved, insertions follow the caret context.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused.
    Writes the draft only — run diff_preview then commit_sync."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("replace_text", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            _agent_preflight(target)
            header, blocks = _read_edit(target)
            index = _find_block(blocks, "p", paragraph_id)
            marker = blocks[index].splitlines()[0]
            body = _block_body(blocks[index])
            texts, styles = _draft_paragraph_state(target, paragraph_id, mode=session.mode)
            _check_single_region(target, paragraph_id, old, texts, styles)
            new_body = _replace_in_body(body, old, new, paragraph_id)
            blocks[index] = marker + ("\n" + new_body if new_body else "")
            _write_edit(target, header, blocks)
            _refresh_regions(target)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "draft-replaced", "status": "pass"}],
            }
            return (
                "success",
                {
                    "paragraph_id": paragraph_id,
                    "draft": "dirty",
                    "next": "diff_preview to inspect style ownership, then commit_sync",
                },
                "mutation",
                payload,
                [],
            )

        return _mutation_tool(
            operation_id,
            "replace_text",
            {
                "workdir": str(workdir),
                "paragraph_id": paragraph_id,
                "old": old,
                "new": new,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def batch_edit(paragraph_id: str, edits: list[dict], *, operation_id: str) -> CallToolResult:
    """Edit several style regions of one paragraph atomically and immediately.

    Each edit targets exactly one region, addressed either by index
    (recommended, from regions.md / get_paragraph styles) or by text anchor:
      {"region": 1, "new": "..."}                 replace whole region
      {"region": 2, "old": "...", "new": "..."}   replace text inside region
      {"text": "...", "style_id": "...", "new": "..."}   text-anchor addressing
    Edits are applied sequentially, each as a single-region sync (the engine
    needs no style inference because the region is explicit). If any edit
    fails the whole batch is rolled back; on success all edits are committed
    and the workdir is clean. A region may be edited at most once per call.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("batch_edit", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            workdir = target  # store mode: mutate the generation snapshot
            parent_snapshot = _agent_preflight(workdir)["current_snapshot"]["id"]
            texts, styles = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
            regions = _merge_regions(texts, styles)
            resolved: list[tuple[int, str | None, str]] = []
            seen: set[int] = set()
            for edit_no, edit in enumerate(edits, start=1):
                if not isinstance(edit, dict):
                    raise ToolError("invalid-edit", f"edit {edit_no}: must be an object")
                region_index = _resolve_region(edit, regions, edit_no)
                if region_index in seen:
                    raise ToolError(
                        "invalid-edit",
                        f"edit {edit_no}: region {region_index} is edited twice; merge the edits",
                    )
                seen.add(region_index)
                new = edit.get("new")
                if not isinstance(new, str):
                    raise ToolError("invalid-edit", f"edit {edit_no}: missing 'new' text")
                old = edit.get("old")
                if old is not None and not isinstance(old, str):
                    raise ToolError("invalid-edit", f"edit {edit_no}: 'old' must be text")
                if old is not None and ("\u27e6" in old or "\u27e7" in old):
                    raise ToolError(
                        "text-not-found",
                        f"edit {edit_no}: old must be visible text without placeholder markers",
                    )
                resolved.append((region_index, old, new))
            protected = [
                workdir / name
                for name in ("typed.md", PROJECTION_FILE, "edit.state.json", "format.json", "regions.md")
            ]
            backup = {path: path.read_bytes() for path in protected if path.exists()}
            try:
                for region_index, old, new in resolved:
                    texts_i, styles_i = _draft_paragraph_state(workdir, paragraph_id, mode=session.mode)
                    regions_i = _merge_regions(texts_i, styles_i)
                    if region_index >= len(regions_i):
                        raise ToolError(
                            "region-out-of-range",
                            f"region {region_index} no longer exists in the paragraph; re-read regions.md",
                        )
                    header, blocks = _read_edit(workdir)
                    index = _find_block(blocks, "p", paragraph_id)
                    marker = blocks[index].splitlines()[0]
                    body = _block_body(blocks[index])
                    new_body = _apply_batch_to_body(body, regions_i, [(region_index, old, new)])
                    blocks[index] = marker + ("\n" + new_body if new_body else "")
                    _write_edit(workdir, header, blocks)
                    sync_edit_projection(workdir)
            except BaseException:
                for path, data in backup.items():
                    path.write_bytes(data)
                raise
            collaboration = document_state(workdir)
            published = None
            if not collaboration["current_matches_filesystem"]:
                published = publish_current(
                    workdir,
                    expected_parent_snapshot=parent_snapshot,
                    origin="agent",
                    changed_paragraph_ids=[paragraph_id],
                )
            _refresh_regions(workdir)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
                "checks": [{"name": "batch-edit", "status": "pass"}],
            }
            return (
                "success",
                {
                    "paragraph_id": paragraph_id,
                    "edits_applied": len(resolved),
                    "state": "clean",
                    "current_snapshot": published["current_snapshot"] if published else collaboration["current_snapshot"],
                    "next": "build_docx to export, or continue editing",
                },
                "mutation",
                payload,
                [],
            )

        return _mutation_tool(
            operation_id,
            "batch_edit",
            {
                "workdir": str(session.workdir),
                "paragraph_id": paragraph_id,
                "edits": edits,
            },
            session.workdir,
            directory=True,
            evidence_path=session.workdir / "run.evidence.json",
            run=run,
            store_workdir=session.workdir,
        )


@mcp.tool()
def insert_paragraph(after_id: str, text: str, inherit: str | None = None, *, operation_id: str) -> CallToolResult:
    """Insert a new paragraph after ``after_id`` in the draft. ``inherit``
    copies the referenced paragraph's insertion style (defaults to
    ``after_id``). Text is visible plain text; structural tokens are not
    allowed in new paragraphs. In track mode the new paragraph carries a
    paragraph-mark insertion revision (R2.5).

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("insert_paragraph", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        if after_id.startswith(("T", "B")) or ("." in after_id) or (inherit or "").startswith(("T", "B")) or "." in (inherit or ""):
            return _failure_result(
                "insert_paragraph",
                "table-structure-immutable",
                "paragraphs cannot be inserted into tables, text boxes, or "
                "header/footer/note parts; container structure operations are out of scope",
            )
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            _agent_preflight(target)
            header, blocks = _read_edit(target)
            index = _find_block(blocks, "p", after_id)
            resolved_inherit = inherit or after_id
            temps = [
                int(m.group(1))
                for block in blocks
                for m in [re.match(r'<!--@new temp="N(\d+)"', block)]
                if m
            ]
            temp = f"N{max(temps, default=0) + 1}"
            block = f'<!--@new temp="{temp}" inherit="{resolved_inherit}"-->\n{_escape_prose(text)}'
            blocks.insert(index + 1, block)
            _write_edit(target, header, blocks)
            _refresh_regions(target)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "draft-inserted", "status": "pass"}],
            }
            return (
                "success",
                {
                    "temp_id": temp,
                    "inherit": resolved_inherit,
                    "draft": "dirty",
                    "next": "commit_sync allocates the formal paragraph ID",
                },
                "mutation",
                payload,
                [],
            )

        return _mutation_tool(
            operation_id,
            "insert_paragraph",
            {
                "workdir": str(workdir),
                "after_id": after_id,
                "text": text,
                "inherit": inherit,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def delete_paragraph(paragraph_id: str, *, operation_id: str) -> CallToolResult:
    """Delete a paragraph from the draft. Paragraphs with protected structure
    (tokens, section boundaries) are rejected by commit_sync. In track mode
    the paragraph stays in the document with a paragraph-mark deletion
    revision (R2.5 merge semantics).

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("delete_paragraph", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        if paragraph_id.startswith(("T", "B")) or ("." in paragraph_id and not paragraph_id.startswith("P")):
            return _failure_result(
                "delete_paragraph",
                "table-structure-immutable",
                "container and part paragraphs cannot be deleted; container "
                "structure operations are out of scope",
            )
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            _agent_preflight(target)
            header, blocks = _read_edit(target)
            index = _find_block(blocks, "p", paragraph_id)
            blocks.pop(index)
            blocks.append(f'<!--@delete id="{paragraph_id}"-->')
            _write_edit(target, header, blocks)
            _refresh_regions(target)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "draft-deleted", "status": "pass"}],
            }
            return (
                "success",
                {"paragraph_id": paragraph_id, "draft": "dirty", "next": "commit_sync"},
                "mutation",
                payload,
                [],
            )

        return _mutation_tool(
            operation_id,
            "delete_paragraph",
            {
                "workdir": str(workdir),
                "paragraph_id": paragraph_id,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def diff_preview() -> str:
    """Dry-run of commit_sync: per changed paragraph, the hunks with style
    ownership (source_style_set -> assigned_styles), warnings, or the
    rejection reason. Read-only; never mutates the workdir. Tracked hunks
    report their generated revisions."""
    with session.lock:
        workdir = session.require()
        state = classify_edit_state(workdir)
        if state["state"] != "dirty":
            return _json({"state": state["state"], "changes": []})
        from .edit import _build_revision_context, parse_edit_projection
        from .edit_sync import _document_has_revisions
        from .typed_core import effective_edit_mode

        text = (workdir / PROJECTION_FILE).read_text(encoding="utf-8")
        projection = parse_edit_projection(text)
        typed = parse_typed((workdir / "typed.md").read_text(encoding="utf-8"))
        format_data = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
        try:
            mode = session.mode or effective_edit_mode(
                source_track_enabled=bool(format_data.get("source_track_enabled")),
                has_pending_revisions=_document_has_revisions(typed),
            )
            revision_ctx = (
                _build_revision_context(
                    typed, format_data, workdir, mode=mode,
                    author=session.author or "", author_source="session",
                )
                if mode == "track"
                else None
            )
            plan = plan_sync(
                typed, projection, format_data,
                mode=mode, revision_ctx=revision_ctx,
            )
            return _json(
                {
                    "state": "dirty",
                    "edit_mode": mode,
                    "changed_paragraph_ids": plan.changed_ids,
                    "hunks": plan.hunks,
                    "warnings": plan.warnings,
                }
            )
        except ValidationError as exc:
            return _json({"state": "dirty", "edit_mode": mode, "rejected": str(exc), "changes": []})


def _commit_sync_impl(
    workdir: Path,
    *,
    origin: str,
    batch_id: str | None = None,
) -> dict[str, Any]:
    parent_snapshot = _agent_preflight(workdir)["current_snapshot"]["id"]
    _, warnings, changed = sync_edit_projection(
        workdir, track=session.track_override, author=session.author
    )
    collaboration = document_state(workdir)
    published = None
    if changed and not collaboration["current_matches_filesystem"]:
        try:
            published = publish_current(
                workdir,
                expected_parent_snapshot=parent_snapshot,
                origin=origin,
                changed_paragraph_ids=changed,
                batch_id=batch_id,
            )
        except CollaborationError as exc:
            raise ToolError(exc.code, exc.detail) from exc
    return {
        "changed_paragraph_ids": changed,
        "warnings": warnings,
        "edit_mode": session.mode,
        "state": "clean",
        "current_snapshot": published["current_snapshot"] if published else collaboration["current_snapshot"],
    }


@mcp.tool()
def commit_sync(*, operation_id: str) -> CallToolResult:
    """Apply the draft to the canonical typed AST and publish one CAS snapshot.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("commit_sync", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            result = _commit_sync_impl(target, origin="agent")
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "commit-sync", "status": "pass"}],
            }
            return "success", result, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "commit_sync",
            {"workdir": str(workdir)},
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def accept_revision(revision_key: str, expected_fingerprint: str, *, operation_id: str) -> CallToolResult:
    """Accept one tracked revision addressed by its revision_key
    (part|kind|w:id|fingerprint, from revisions.json) plus the expected
    fingerprint. Accept insert = unwrap its text; accept delete = remove it.
    Publish transactionally and regenerate all derived views. Requires a
    clean workdir.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("accept_revision", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            from .decisions import _decide_single

            decision = _decide_single(
                target, revision_key, action="accept", author=session.author,
                expected_fingerprint=expected_fingerprint,
            )
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "decision": {"action": "accept", "w_id": decision["w_id"], "paragraph_id": decision["paragraph_id"]},
                "checks": [{"name": "revision-accepted", "status": "pass"}],
            }
            return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "accept_revision",
            {
                "workdir": str(workdir),
                "revision_key": revision_key,
                "expected_fingerprint": expected_fingerprint,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def reject_revision(revision_key: str, expected_fingerprint: str, *, operation_id: str) -> CallToolResult:
    """Reject one tracked revision addressed by revision_key + fingerprint.
    Reject insert = remove its text; reject delete = restore its text.
    Publish transactionally; requires a clean workdir.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("reject_revision", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            from .decisions import _decide_single

            decision = _decide_single(
                target, revision_key, action="reject", author=session.author,
                expected_fingerprint=expected_fingerprint,
            )
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "decision": {"action": "reject", "w_id": decision["w_id"], "paragraph_id": decision["paragraph_id"]},
                "checks": [{"name": "revision-rejected", "status": "pass"}],
            }
            return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "reject_revision",
            {
                "workdir": str(workdir),
                "revision_key": revision_key,
                "expected_fingerprint": expected_fingerprint,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def reinsert_deleted_text(
    revision_key: str,
    expected_fingerprint: str,
    text: str | None = None,
    *,
    operation_id: str,
) -> CallToolResult:
    """Create a NEW insertion revision after an existing deletion (key +
    fingerprint), without touching the original deletion. ``text`` defaults
    to the deleted text.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("reinsert_deleted_text", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            from .decisions import _decide_single

            decision = _decide_single(
                target, revision_key, action="reinsert",
                author=session.author, text=text,
                expected_fingerprint=expected_fingerprint,
            )
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "decision": {"action": "reinsert", "w_id": decision["w_id"], "paragraph_id": decision["paragraph_id"]},
                "checks": [{"name": "revision-reinserted", "status": "pass"}],
            }
            return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "reinsert_deleted_text",
            {
                "workdir": str(workdir),
                "revision_key": revision_key,
                "expected_fingerprint": expected_fingerprint,
                "text": text,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def delete_comment(comment_id: str, *, operation_id: str) -> CallToolResult:
    """Delete one Word comment by its w:id: the comments.xml entry, every
    commentRangeStart/End anchor and commentReference in the document are
    removed. Publishes transactionally; requires a clean workdir.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("delete_comment", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            from .decisions import _delete_comment

            decision = _delete_comment(target, comment_id)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "decision": {"action": "comment-delete", "comment_id": decision["comment_id"]},
                "checks": [{"name": "comment-deleted", "status": "pass"}],
            }
            return "success", {"decision": decision, "state": "clean"}, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "delete_comment",
            {
                "workdir": str(workdir),
                "comment_id": comment_id,
            },
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


def _table_op_tool(operation: str, table_ref: str, output: str, workdir_out: str, *numbers: int, operation_id: str, discard_content: bool = False) -> CallToolResult:
    from .decisions import _apply_table_op

    with session.lock:
        if session.workdir is None:
            return _failure_result(f"table_{operation}", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        new_workdir = Path(workdir_out).resolve()
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            if tx is not None:
                output_staged = tx.staging("decided.docx")
                created = _apply_table_op(
                    target, table_ref, operation, list(numbers),
                    output_staged, Path(workdir_out),
                    discard_content=discard_content,
                )
                tx.stage_external(Path(output).resolve(), output_staged, mode="create")
                # The final path does not exist yet: publish happens after the
                # prepared journal. Hash the staged artifact and record the
                # final path in the evidence payload.
                output_real = Path(output).resolve()
                docx_evidence = {"sha256": file_sha256(output_staged), "path": str(output_real)}
            else:
                created = _apply_table_op(
                    target, table_ref, operation, list(numbers),
                    Path(output), Path(workdir_out),
                    discard_content=discard_content,
                )
                output_real = Path(output).resolve()
                docx_evidence = {"sha256": file_sha256(output_real)}
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": docx_evidence,
                    "workdir": {"manifest_sha256": _workdir_manifest_sha256(created)},
                },
                "table": table_ref,
                "checks": [{"name": f"table-{operation}", "status": "pass"}],
            }
            return (
                "success",
                {"operation": operation, "table": table_ref, "workdir": str(created)},
                "mutation",
                payload,
                [],
            )

        return _mutation_tool(
            operation_id,
            f"table_{operation}",
            {
                "workdir": str(workdir),
                "table_ref": table_ref,
                "output": output,
                "workdir_out": workdir_out,
                "args": list(numbers),
                "discard_content": discard_content,
            },
            new_workdir,
            directory=True,
            evidence_path=new_workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
            store_generation=False,
        )


@mcp.tool()
def table_insert_row(table_ref: str, after: int, output: str, workdir_out: str, *, operation_id: str) -> CallToolResult:
    """Insert an empty row after ``after`` (0-based) in ``table_ref`` (T0).
    Produces a new DOCX and clean-baseline workdir; the source is untouched.
    Mutating: requires a caller-supplied ``operation_id``."""
    return _table_op_tool("insert-row", table_ref, output, workdir_out, after, operation_id=operation_id)


@mcp.tool()
def table_delete_row(table_ref: str, row: int, output: str, workdir_out: str, *, operation_id: str) -> CallToolResult:
    """Delete row ``row`` (0-based) from ``table_ref``.
    Mutating: requires a caller-supplied ``operation_id``."""
    return _table_op_tool("delete-row", table_ref, output, workdir_out, row, operation_id=operation_id)


@mcp.tool()
def table_insert_col(table_ref: str, after: int, output: str, workdir_out: str, *, operation_id: str) -> CallToolResult:
    """Insert an empty column after ``after`` (0-based) in every row.
    Mutating: requires a caller-supplied ``operation_id``."""
    return _table_op_tool("insert-col", table_ref, output, workdir_out, after, operation_id=operation_id)


@mcp.tool()
def table_delete_col(table_ref: str, col: int, output: str, workdir_out: str, *, operation_id: str) -> CallToolResult:
    """Delete column ``col`` (0-based) from every row.
    Mutating: requires a caller-supplied ``operation_id``."""
    return _table_op_tool("delete-col", table_ref, output, workdir_out, col, operation_id=operation_id)


@mcp.tool()
def table_merge_cells(table_ref: str, row: int, col: int, span: int, output: str, workdir_out: str, discard_content: bool = False, *, operation_id: str) -> CallToolResult:
    """Merge ``span`` cells horizontally starting at (row, col) via gridSpan.

    Fail-closed: when a spanned cell (beyond the first) carries text, the
    merge is refused with ``merge-would-discard-content`` unless
    ``discard_content=true`` explicitly drops it. The first cell's content
    is always kept. Mutating: requires a caller-supplied ``operation_id``."""
    return _table_op_tool("merge-cells", table_ref, output, workdir_out, row, col, span, operation_id=operation_id, discard_content=discard_content)


@mcp.tool()
def table_split_cells(table_ref: str, row: int, col: int, span: int, output: str, workdir_out: str, *, operation_id: str) -> CallToolResult:
    """Split the cell at (row, col) into ``span`` cells.
    Mutating: requires a caller-supplied ``operation_id``."""
    return _table_op_tool("split-cells", table_ref, output, workdir_out, row, col, span, operation_id=operation_id)


@mcp.tool()
def decide_all(
    action: str,
    output: str,
    workdir_out: str,
    *,
    operation_id: str,
) -> CallToolResult:
    """Accept or reject every revision and produce a new clean-baseline
    project: build a decided DOCX at ``output`` and re-extract it into a new
    workdir at ``workdir_out`` (normalization governance). The original
    workdir is never mutated. ``action``: accept | reject.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("decide_all", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        if action not in ("accept", "reject"):
            return _failure_result("decide_all", "invalid-action", "action must be accept or reject")
        new_workdir = Path(workdir_out).resolve()
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            from .decisions import _decide_all

            if tx is not None:
                output_staged = tx.staging("decided.docx")
                created = _decide_all(target, action, output_staged, Path(workdir_out))
                tx.stage_external(Path(output).resolve(), output_staged, mode="create")
                # Publish happens after the prepared journal: the final path
                # does not exist yet, so hash the staged artifact.
                output_real = Path(output).resolve()
                docx_evidence = {"sha256": file_sha256(output_staged), "path": str(output_real)}
            else:
                created = _decide_all(target, action, Path(output), Path(workdir_out))
                output_real = Path(output).resolve()
                docx_evidence = {"sha256": file_sha256(output_real)}
            report = json.loads((created / "decisions.json").read_text(encoding="utf-8"))
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": docx_evidence,
                    "workdir": {"manifest_sha256": _workdir_manifest_sha256(created)},
                },
                "action": action,
                "revision_count": report["revision_count"],
                "checks": [{"name": "decide-all", "status": "pass"}],
            }
            return (
                "success",
                {
                    "action": action,
                    "output": str(output_real),
                    "workdir": str(created),
                    "note": "original workdir untouched; decisions.json in the new workdir",
                },
                "mutation",
                payload,
                [],
            )

        return _mutation_tool(
            operation_id,
            "decide_all",
            {
                "workdir": str(workdir),
                "action": action,
                "output": output,
                "workdir_out": workdir_out,
            },
            new_workdir,
            directory=True,
            evidence_path=new_workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
            store_generation=False,
        )


def _comments_listing(workdir: Path) -> list[dict[str, str]]:
    """Comment inventory for one workdir: id, author, date, text, anchors.
    Lock-free; callers hold the session lock."""
    import re as _re
    import zipfile

    fmt = json.loads((workdir / "format.json").read_text(encoding="utf-8"))
    comments: list[dict[str, str]] = []
    for record in fmt.get("paragraphs", []):
        if record.get("part_key") == "comments" and not record.get("deleted"):
            entry_id = record.get("part_entry_id")
            if entry_id is not None:
                comments.append({
                    "id": str(entry_id),
                    "paragraph_id": record["id"],
                })
    # anchor mapping: body paragraph records carry token ids; the token
    # table records comment-start anchors with their w:id
    anchors: dict[str, list[str]] = {}
    tokens = fmt.get("tokens", {})
    for record in fmt.get("paragraphs", []):
        if record.get("part_key"):
            continue
        for token_id, _kind in record.get("token_ids", []) or []:
            token = tokens.get(token_id) or {}
            if token.get("kind") == "comment-start":
                attrs = token.get("attrs", {}) or {}
                anchors.setdefault(str(attrs.get("w:id")), []).append(record["id"])
    # author/date/text from the template's comments.xml (read-only)
    template = workdir / fmt.get("template", "_template.docx")
    meta: dict[str, dict[str, str]] = {}
    try:
        with zipfile.ZipFile(template) as archive:
            comments_xml = archive.read("word/comments.xml").decode("utf-8")
        for match in _re.finditer(
            r'<w:comment\s+[^>]*?w:id="(\d+)"[^>]*>.*?</w:comment>',
            comments_xml, _re.S,
        ):
            tag = match.group(0)
            author = _re.search(r'w:author="([^"]*)"', tag)
            date = _re.search(r'w:date="([^"]*)"', tag)
            text = "".join(_re.findall(r"<w:t[^>]*>([^<]*)</w:t>", tag))
            meta[match.group(1)] = {
                "author": author.group(1) if author else "",
                "date": date.group(1) if date else "",
                "text": text,
            }
    except Exception:  # noqa: BLE001 - metadata is best-effort
        pass
    result = []
    for comment in comments:
        comment.update(meta.get(comment["id"], {"author": "", "date": "", "text": ""}))
        comment["anchor_paragraphs"] = anchors.get(comment["id"], [])
        result.append(comment)
    return result


@mcp.tool()
def list_comments() -> str:
    """List every comment in the opened workdir: id, author, date, text,
    and the body paragraphs carrying its anchors. The comment workflow
    (delete_comment, decide_all) addresses comments by id."""
    with session.lock:
        workdir = session.require()
        return _json({"comments": _comments_listing(workdir)})


@mcp.tool()
def get_comment(comment_id: str) -> str:
    """Read one comment: id, author, date, text, and the body paragraphs
    carrying its anchors."""
    with session.lock:
        workdir = session.require()
        for comment in _comments_listing(workdir):
            if comment["id"] == str(comment_id):
                return _json(comment)
        raise ToolError("comment-not-found", f"comment {comment_id} not in the workdir")

@mcp.tool()
def review_preflight() -> str:
    """Return the agent gate, current snapshot, staged snapshot, and wake queue."""
    with session.lock:
        workdir = session.require()
        return _json(preflight(workdir))


@mcp.tool()
def review_state() -> str:
    """Read the collaboration session state without consuming review events."""
    with session.lock:
        return _json(document_state(session.require()))
@mcp.tool()
def review_external_preflight(expected_parent_snapshot: str, operation: str = "import") -> CallToolResult:
    """Issue a CAS guard for an external import or rollback writer.

    Mutating (writes the guard): returns the common Result envelope as
    structuredContent; domain failures are isError Results carrying stable
    Diagnostics."""
    with session.lock:
        try:
            data = external_write_guard(
                session.require(),
                expected_parent_snapshot=expected_parent_snapshot,
                operation=operation,
            )
        except CollaborationError as exc:
            return _failure_result("review_external_preflight", exc.code, exc.detail)
        envelope = result_envelope("review_external_preflight", "success", data=data)
        return mcp_result(envelope)
@mcp.tool()
def review_settlement_plan(event_ids: list[str] | None = None) -> str:
    """Return mixed accept/reject/defer decisions, patches, and carry-forward guards."""
    with session.lock:
        return _json(settlement_plan(session.require(), [str(item) for item in event_ids] if event_ids else None))

@mcp.tool()
def review_settle(event_ids: list[str] | None = None, *, operation_id: str) -> CallToolResult:
    """Atomically settle accept/reject decisions and carry deferred items.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused. No
    stable default exists (an empty event list means "whatever is actionable
    now", which changes between rounds), so the id is mandatory."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("review_settle", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)
        wanted = [str(item) for item in event_ids] if event_ids else None

        def run(target, tx=None):
            data = settle_decisions(target, wanted)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "review-settled", "status": "pass"}],
            }
            return "success", data, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "review_settle",
            {"workdir": str(workdir), "event_ids": wanted},
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


def _review_apply_batch(workdir: Path, batch_id: str, requested_event_id: str | None = None) -> dict[str, Any]:
    events = review_snapshot(workdir)["events"]
    requested = next(
        (item for item in events if str(item.get("event_id")) == str(requested_event_id)),
        None,
    ) if requested_event_id else None
    if requested_event_id and requested is None:
        raise ToolError("review-event-not-found", f"review event {requested_event_id} not found")
    if requested and requested.get("type") != "patch":
        raise ToolError("not-a-patch", f"review event {requested_event_id} is not a semantic patch")
    if requested and requested.get("delivery_state") == "applied":
        return {"event": requested, "state": "already-applied"}
    batch_events = [
        item for item in events
        if item.get("type") == "patch"
        and item.get("status") == "queued"
        and (not batch_id or str(item.get("batch_id")) == batch_id)
    ]
    batch_events.sort(
        key=lambda item: int(str(item.get("staged_snapshot", "H0.0")).rsplit(".", 1)[-1])
    )
    if requested and requested not in batch_events:
        raise ToolError("patch-not-queued", f"review event {requested_event_id} is not queued")
    if not batch_events:
        raise ToolError("patch-batch-empty", f"no queued patches in batch {batch_id or '<none>'}")
    state = document_state(workdir)
    if not state["current_matches_filesystem"]:
        raise ToolError("current-snapshot-drift", "typed.md differs from the canonical snapshot")
    expected_parent = state["current_snapshot"]["id"]
    for patch in batch_events:
        if patch.get("parent_snapshot") != expected_parent:
            raise ToolError(
                "patch-parent-mismatch",
                f"patch parent {patch.get('parent_snapshot')} does not match {expected_parent}",
            )
        expected_parent = str(patch.get("staged_snapshot") or expected_parent)
        _validate_collab_patch_target(workdir, patch)
    ranges: dict[str, list[tuple[int, int]]] = {}
    for patch in batch_events:
        target = patch["target"]
        start, end = int(target["start_offset"]), int(target["end_offset"])
        paragraph_id = str(patch["paragraph_id"])
        ranges.setdefault(paragraph_id, []).append((start, end))
    for paragraph_id, paragraph_ranges in ranges.items():
        previous_end = -1
        previous_start = -1
        for start, end in sorted(paragraph_ranges):
            if start < previous_end or (start == previous_start and start == end):
                raise ToolError("patch-overlap", f"{paragraph_id}: overlapping patches require a new selection")
            previous_start, previous_end = start, end
    claimed: list[dict[str, Any]] = []
    try:
        for patch in batch_events:
            claimed.append(update_review_event(workdir, str(patch["event_id"]), {"delivery_state": "in_progress"}))
        for patch in sorted(
            batch_events,
            key=lambda item: (str(item["paragraph_id"]), -int(item["target"]["start_offset"])),
        ):
            _apply_patch_to_draft(workdir, patch, validate=False)
        committed = _commit_sync_impl(workdir, origin="human_ui", batch_id=batch_id or None)
        if not committed["changed_paragraph_ids"]:
            raise ToolError("patch-noop", "human patch batch produced no canonical change")
        updated = [
            update_review_event(
                workdir,
                str(patch["event_id"]),
                {
                    "delivery_state": "applied",
                    "review_decision": "adjusted",
                    "applied_snapshot": committed["current_snapshot"]["id"],
                },
            )
            for patch in batch_events
        ]
        result = {"events": updated, "commit": committed, "state": "applied"}
        if requested_event_id:
            result["event"] = next(item for item in updated if str(item["event_id"]) == str(requested_event_id))
        return result
    except Exception as exc:  # noqa: BLE001 - restore draft and keep the batch queued
        try:
            refresh_edit_projection(workdir, discard=True)
        finally:
            for patch in claimed:
                update_review_event(
                    workdir,
                    str(patch["event_id"]),
                    {"delivery_state": "queued", "last_error": str(exc)},
                )
        raise ToolError("patch-apply-failed", str(exc)) from exc


@mcp.tool()
def review_apply_patch(event_id: str, *, operation_id: str | None = None) -> CallToolResult:
    """Apply the queued human patch batch containing ``event_id`` atomically.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused. When
    ``operation_id`` is omitted the stable event-derived id
    ``review-apply-patch-<event_id>`` is used — the event uniquely names the
    one-shot apply, so a retry still replays byte-exact."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("review_apply_patch", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        op_id = operation_id or f"review-apply-patch-{event_id}"
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            event = next(
                (item for item in review_snapshot(target)["events"] if str(item.get("event_id")) == str(event_id)),
                None,
            )
            if event is None:
                raise ToolError("review-event-not-found", f"review event {event_id} not found")
            if event.get("delivery_state") == "applied":
                data = {"event": event, "state": "already-applied"}
            else:
                data = _review_apply_batch(target, str(event.get("batch_id") or ""), str(event_id))
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "review-patch-applied", "status": "pass"}],
            }
            return "success", data, "mutation", payload, []

        return _mutation_tool(
            op_id,
            "review_apply_patch",
            {"workdir": str(workdir), "event_id": event_id},
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def review_apply_batch(batch_id: str, *, operation_id: str | None = None) -> CallToolResult:
    """Apply one queued human patch batch as one canonical transaction.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused. When
    ``operation_id`` is omitted the stable batch-derived id
    ``review-apply-batch-<batch_id>`` is used — the batch uniquely names the
    one-shot apply, so a retry still replays byte-exact."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("review_apply_batch", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        op_id = operation_id or f"review-apply-batch-{batch_id}"
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            data = _review_apply_batch(target, str(batch_id))
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "review-batch-applied", "status": "pass"}],
            }
            return "success", data, "mutation", payload, []

        return _mutation_tool(
            op_id,
            "review_apply_batch",
            {"workdir": str(workdir), "batch_id": batch_id},
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )
@mcp.tool()
def review_inbox(include_acknowledged: bool = False) -> str:
    """Read queued review events together with the mandatory agent preflight."""
    with session.lock:
        workdir = session.require()
        queue = review_snapshot(workdir)
        gate = preflight(workdir)
        allowed = {"queued", "acknowledged"} if include_acknowledged else {"queued"}
        events = [event for event in queue["events"] if event.get("status") in allowed]
        batches = sorted({str(event["batch_id"]) for event in events if event.get("batch_id")})
        return _json(
            {
                "preflight": gate,
                "events": events,
                "counts": queue["counts"],
                "wake": {"required": bool(events), "batch_ids": batches, "event_count": len(events)},
            }
        )


@mcp.tool()
def review_ack(event_ids: list[str], *, operation_id: str) -> CallToolResult:
    """Acknowledge review events after the agent has consumed them.

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused. No
    stable default exists (acking is a caller-scoped consumption round), so
    the id is mandatory."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("review_ack", "workdir-not-open", "no workdir open; call workdir_open first")
        if not event_ids:
            return _failure_result("review_ack", "event-ids-required", "provide at least one review event id")
        workdir = session.workdir
        wanted = [str(item) for item in event_ids]
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            acknowledged = acknowledge_review(target, wanted)
            data = {"acknowledged": acknowledged, "counts": review_snapshot(target)["counts"]}
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "review-acknowledged", "status": "pass"}],
            }
            return "success", data, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "review_ack",
            {"workdir": str(workdir), "event_ids": wanted},
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def revert(*, operation_id: str) -> CallToolResult:
    """Discard the uncommitted draft and regenerate the projection from the
    canonical typed source (equivalent to edit refresh --discard).

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("revert", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)

        def run(target, tx=None):
            refresh_edit_projection(target, discard=True)
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(target)}},
                "checks": [{"name": "draft-reverted", "status": "pass"}],
            }
            return "success", {"state": "clean", "message": "draft discarded"}, "mutation", payload, []

        return _mutation_tool(
            operation_id,
            "revert",
            {"workdir": str(workdir)},
            workdir,
            directory=True,
            evidence_path=workdir / "run.evidence.json",
            run=run,
            store_workdir=workdir,
        )


@mcp.tool()
def build_docx(output: str | None = None, *, operation_id: str) -> CallToolResult:
    """Build the DOCX from the committed workdir (requires clean state).

    Mutating: requires a caller-supplied ``operation_id``; identical retries
    replay the original result, changed input fails operation-id-reused."""
    with session.lock:
        if session.workdir is None:
            return _failure_result("build_docx", "workdir-not-open", "no workdir open; call workdir_open first")
        workdir = session.workdir
        manifest_before = _workdir_manifest_sha256(workdir)
        resolved_output = (
            Path(output).resolve()
            if output
            else workdir.resolve().parent / f"{workdir.resolve().name}.docx"
        )

        def run(target, tx=None):
            if tx is not None:
                staged = tx.staging("build.docx")
                built = build_workdir(target, staged)
                tx.stage_external(resolved_output, staged, mode="replace")
                published = resolved_output
            else:
                built = build_workdir(target, output)
                published = built
            payload = {
                **base_evidence_payload(),
                "inputs": {"workdir": {"manifest_sha256": manifest_before}},
                "outputs": {
                    "docx": {"sha256": file_sha256(built), "bytes": built.stat().st_size}
                },
                "checks": [{"name": "build", "status": "pass"}],
            }
            return "success", {"output": str(published)}, "build", payload, []

        return _mutation_tool(
            operation_id,
            "build_docx",
            {
                "workdir": str(workdir),
                "output": output,
            },
            resolved_output,
            directory=False,
            evidence_path=Path(str(resolved_output) + ".evidence.json"),
            run=run,
            store_workdir=workdir,
            store_generation=False,
        )


@mcp.tool()
def verify_output(output: str) -> CallToolResult:
    """Independently verify a built DOCX against the workdir.

    Returns the common Result envelope as structuredContent: the
    verification result plus revision and comment summaries read from the
    output package, so the caller does not need to unzip the DOCX to confirm
    tracked edits or comment state. The verification outcome publishes
    ``docx2typed-run-evidence-1`` beside the output; an evidence publish
    failure reports the tool as failed."""
    import re as _re

    with session.lock:
        workdir = session.require()
        try:
            verify_workdir(workdir, output)
        except (OSError, zipfile.BadZipFile, TypedError) as exc:
            return _failure_result("verify_output", _domain_code(str(exc)), str(exc))
        evidence: dict[str, object] = {"verified": str(output)}
        try:
            import zipfile

            with zipfile.ZipFile(output) as archive:
                names = {
                    name for name in archive.namelist()
                    if _re.match(rb"word/.*\.xml$", name.encode())
                }
                xml = b"".join(archive.read(name) for name in sorted(names))
                comments_xml = archive.read("word/comments.xml") if "word/comments.xml" in names else b""
            ins = len(_re.findall(rb"<w:ins[ >]", xml))
            dels = len(_re.findall(rb"<w:del[ >]", xml))
            authors = sorted(
                {value.decode("utf-8", errors="replace") for value in _re.findall(rb'w:author="([^"]*)"', xml)}
            )
            comment_ids = [
                value.decode("utf-8", errors="replace")
                for value in _re.findall(rb'<w:comment w:id="(\d+)"', comments_xml)
            ]
            evidence["checks"] = {
                "text": "pass", "styles": "pass", "structure": "pass",
                "package": "pass", "revisions": "pass", "comments": "pass",
            }
            evidence["revisions"] = {
                "insert": ins, "delete": dels, "authors": authors,
            }
            evidence["comments"] = {"ids": comment_ids}
        except Exception as exc:  # noqa: BLE001 - evidence is best-effort
            evidence["evidence_error"] = str(exc)
        payload = {
            **base_evidence_payload(),
            "inputs": {"workdir": {"manifest_sha256": _workdir_manifest_sha256(workdir)}},
            "outputs": {"docx": {"sha256": file_sha256(Path(output).resolve())}},
            "verdict": "pass",
            "checks": evidence.get("checks", {}),
            "revisions": evidence.get("revisions", {}),
            "comments": {"count": len(evidence.get("comments", {}).get("ids", []))},
        }
        run_ev = run_evidence(
            "verify_output", "success", kind="verify", operation_id=new_operation_id(), payload=payload
        )
        evidence_path = Path(str(output) + ".verify.evidence.json")
        try:
            publish_run_evidence(evidence_path, run_ev)
        except OSError as exc:
            # Same deterministic detail convention as _evidence_publish_failed:
            # exception class + the stable evidence path — never the transient
            # mkstemp temp filename embedded in str(exc) — so every retry
            # reports the byte-identical diagnostic.
            return _failure_result(
                "verify_output",
                "evidence-publish-failed",
                f"required run evidence could not be published: {type(exc).__name__}: {evidence_path}",
            )
        envelope = result_envelope(
            "verify_output",
            "success",
            data=evidence,
            evidence=[run_ev],
        )
        return mcp_result(envelope)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
