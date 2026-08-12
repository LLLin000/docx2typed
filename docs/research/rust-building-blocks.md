# Research: Rust building blocks for byte-preserving DOCX editing

Status: research note for ticket #35 (wayfinder:research) · 2026-08-12 · branch `research/rust-building-blocks`
Scope: candidate crates and std facilities vs. the project's actual contracts, verified from official docs/source.
This note does **not** choose a final crate layout or module design — it feeds tickets #34, #36, #39, #41.

## Question

Which maintained Rust crates and standard-library facilities can satisfy the project's contracts for ZIP
metadata and byte replay, streaming namespace-aware XML tokenization with exact byte ranges, hashing and
atomic filesystem publication, Unicode grapheme segmentation pinned to a version, MCP stdio, and the local
review HTTP server? Where does no crate satisfy the byte-fidelity need, and project-owned code is required?

## Verified contracts (from this repo)

| Contract | Repo source |
|---|---|
| Byte-preserving document patch: copy template `word/document.xml` bytes, replace only explicitly located direct-body paragraph slices; untouched bytes verbatim; whole-document serializers cannot provide this boundary | `docs/adr/0013-byte-preserving-document-patch.md` |
| Template package integrity: every non-editable ZIP part must hash-unchanged; fingerprint mismatch rejects build | `docs/adr/0004-template-package-integrity.md`, `CONTEXT.md` ("Template package integrity") |
| Transactional build: temp DOCX → validate → patch → manifest check → independent verify → only then atomic replace | `docs/adr/0022-transactional-build-output.md` |
| No-op build gate: "byte-identical to the input DOCX — `document.xml` content hash equal, every untouched part replayed verbatim" (part-content level; the container is rebuilt today) | `verification.md` ("Byte-fidelity checks") |
| Walker discipline: token-level walker over raw part bytes yielding (tag, open/close/self-close, byte range) + nesting stack; owns bytes-vs-str offsets (CJK), namespace-prefix handling, self-closing detection | `docs/prd/byte-surgery-layer.md` |
| SHA-256: per-part manifest hashes, whole-file source/template fingerprints, content-addressed style IDs (`sha256(canonical rPr)[:16]`) | `scripts/typed_docx.py:182-193`, `scripts/typed_core.py:167-168`; `docs/adr/0009-conservative-rpr-canonicalization.md` |
| Conservative rPr canonicalization is project-defined logic (lexical equivalence only), not a standards canonicalizer | `docs/adr/0009-conservative-rpr-canonicalization.md` |
| Pinned Unicode data: catalog generated from a fixed Unicode version; runtime never re-derives from the platform | `docs/adr/0034-pinned-unicode-catalog.md`, `docs/adr/0027-versioned-unicode-vertical-catalog.md` |
| MCP tools today: Python `FastMCP` (mcp SDK ≥1.25), stdio transport | `pyproject.toml`, `scripts/mcp_server.py:87` |
| Review HTTP today: stdlib `ThreadingHTTPServer`, binds `127.0.0.1` by default or a Tailscale IPv4 with `--tailscale`; 256 KiB body cap; **no auth token today** | `scripts/review_server.py:19,211` |
| Python ZIP rebuild today: `zipfile.ZipFile(output, "w")`, `writestr(info, data)` per entry in source order — ZipInfo metadata (name, timestamps, attrs, `compress_type`, extra) is carried, but the deflate stream is **re-encoded**, so container bytes differ from the source | `scripts/typed_docx.py:2598-2626`; https://docs.python.org/3/library/zipfile.html#zipfile.ZipFile.writestr |

## Verified candidate facts

### ZIP container

- **`zip` crate is the maintained choice; `zips` is an unrelated macro crate.** `zip` latest stable 8.6.0
  (2026-04-25), newest release 9.0.0-pre3 (2026-08-11), MIT, MSRV 1.88, ~237M downloads, very active
  (repo `zip-rs/zip2`, renamed from `zip-rs/zip`; crate name unchanged, no deprecation notice).
  `zips` 0.1.7 is a macro utility ("wrap Options/Results"), NOT a fork/rename. https://crates.io/crates/zip
  https://crates.io/crates/zips https://raw.githubusercontent.com/zip-rs/zip2/master/Cargo.toml
- Deflate backend: default `deflate` feature = flate2 with **zlib-rs** backend (quality 1..=9) + zopfli
  (10..=264); `deflate-miniz` was dropped in 3.0.0. https://raw.githubusercontent.com/zip-rs/zip2/v8.6.0/Cargo.toml
- Per-entry metadata: `FileOptions` offers `compression_method`, `compression_level`, `last_modified_time`,
  `unix_permissions`, `system` (added 8.1.0 "so byte-for-byte identical archives can be created across
  platforms"), `large_file`, `with_alignment`, `with_file_comment`, `add_extra_data`, `clear_extra_data`.
  **No public general-purpose-flags setter** (UTF-8 bit 11, data-descriptor bit 3, encryption flags are
  auto-managed). https://docs.rs/zip/latest/zip/write/struct.FileOptions.html
  https://github.com/zip-rs/zip2/blob/master/CHANGELOG.md
- **Compressed-bytes passthrough exists.** Read side `ZipArchive::by_index_raw` ("without decompressing
  it"); write side `ZipWriter::raw_copy_file` / `raw_copy_file_rename` / `raw_copy_file_to_path` /
  `raw_copy_file_touch` ("no need to decompress and compress it again"). Headers are regenerated from
  parsed metadata; **extra fields are NOT auto-carried** (re-add via `extra_data_fields()` +
  `add_extra_data`). https://docs.rs/zip/latest/zip/read/struct.ZipArchive.html
  https://docs.rs/zip/latest/zip/write/struct.ZipWriter.html
  https://raw.githubusercontent.com/zip-rs/zip2/v8.6.0/src/read.rs
- `merge_archive` copies another archive's local-file region (bytes 0..central-dir-start) verbatim in one
  `io::copy`, regenerating the central directory with shifted offsets. https://docs.rs/zip/latest/zip/write/struct.ZipWriter.html
- Data descriptors: written only in stream mode (`new_stream`), ZipCrypto, or large-file cases; seekable
  writes seek back and patch the local header. Entry order = writing order (streaming design; central
  directory written at `finish()`). https://raw.githubusercontent.com/zip-rs/zip2/master/src/write.rs
- Normal writing re-encodes (deflate through zlib-rs/zopfli); `CompressionMethod::Stored` available to
  avoid compression. No official promise of byte-identity for an edit round-trip (raw copy preserves entry
  data; headers rebuilt). https://docs.rs/zip/latest/zip/enum.CompressionMethod.html
- Maintenance evidence: `raw_copy_file_touch` merged 2024-11-19 (PR #260); AES metadata in raw copy fixed
  2025-09-09 (PR #417); open issue #193 tracks a bulk raw-copy workstream. https://github.com/zip-rs/zip2/pull/260
  https://github.com/zip-rs/zip2/pull/417 https://github.com/zip-rs/zip2/issues/193
- `sevenz-rust` is 7z-only, not ZIP. https://crates.io/crates/sevenz-rust

### Namespace-aware streaming XML with exact byte ranges

- **`quick-xml` 0.41.0** (2026-06-29), MIT, MSRV 1.79, active (pushed 2026-08-03). Streaming, near-zero-copy:
  `Reader::read_event()` borrows from `&[u8]`/`&str` input. **`buffer_position()` returns the u64 byte
  offset "just after the last emitted event"** — an official doctest slices the original input by it;
  `error_position()` points at the offending markup. `Span` = `std::ops::Range<u64>` returned by
  `read_to_end()` for content between `>` of the open tag and `<` of the close tag.
  https://docs.rs/quick-xml/latest/quick_xml/reader/struct.Reader.html
  https://docs.rs/quick-xml/latest/quick_xml/reader/type.Span.html
  https://raw.githubusercontent.com/tafia/quick-xml/master/src/reader/mod.rs
- Namespaces: `NsReader::read_resolved_event_into()` → `(ResolveResult, Event)`; `NamespaceResolver`
  scope push/pop, `resolve_element` (default namespace applies to unprefixed elements),
  `resolve_attribute`; `ResolveResult::{Bound, Unbound, Unknown}`; 256 xmlns declarations per element cap.
  **Documented limitation: prefix resolution does NOT change name matching** — `</b:name>` cannot close
  `<a:name>` even when both prefixes resolve to the same namespace.
  https://docs.rs/quick-xml/latest/quick_xml/reader/struct.NsReader.html
  https://docs.rs/quick-xml/latest/quick_xml/name/struct.NamespaceResolver.html
  https://raw.githubusercontent.com/tafia/quick-xml/master/src/reader/ns_reader.rs
- Config: `check_end_names` (default true), `expand_empty_elements` (default false; synthesizes the End
  event — "additional allocates"; effect on `buffer_position` undocumented), `trim_text_*`, etc.
  `ignore_comments`/`allow_unclosed_tags` do NOT exist in 0.41.0. DTD is not parsed (opaque `DocType`
  event); CDATA is an unescaped `CData` event; entities stay raw in events. UTF-8 by default; `encoding`
  feature covers ASCII-compatible encodings only (UTF-16 needs a `DecodingReader` wrapper).
  https://docs.rs/quick-xml/latest/quick_xml/reader/struct.Config.html
  https://docs.rs/quick-xml/latest/quick_xml/events/enum.Event.html https://docs.rs/quick-xml/latest/quick_xml/
- **`xmlparser` 0.13.6** (2023-09-30), MIT/Apache-2.0: zero-alloc tokenizer; every token carries a
  `StrSpan` (`range() -> Range<usize>`); element/attribute prefixes tokenized as separate spans but
  **NOT resolved** — namespace resolution is consumer-owned. Last release 2023 (repo commits through
  2025-12), i.e. dormant-ish but stable. https://docs.rs/xmlparser/latest/xmlparser/
  https://raw.githubusercontent.com/RazrFalcon/xmlparser/master/src/lib.rs
- **`roxmltree` 0.21.1** (2025-10-12), MIT OR Apache-2.0, MSRV 1.60, active: read-only DOM (non-streaming);
  `Node::range()` / `Attribute::range()` (default `positions` feature) plus `Document::input_text()` —
  exact original bytes recoverable as `&input[node.range()]`; full namespace support (`ExpandedName`,
  `lookup_namespace_uri`). Text content is unescaped/entity-resolved, so content differs from raw bytes
  even though ranges map to source. https://docs.rs/roxmltree/latest/roxmltree/struct.Node.html
  https://raw.githubusercontent.com/RazrFalcon/roxmltree/master/CHANGELOG.md
- **`xml-rs`** — crate renamed to **`xml`** 1.4.0 (2026-08-06), MIT, active (kornelski). Streaming pull
  parser with namespace resolution during parsing (unbound prefixes are errors), but **no byte offsets**
  and owned `String` events (copies). Not suitable for the byte-range contract.
  https://crates.io/crates/xml-rs https://docs.rs/crate/xml/latest/
  https://raw.githubusercontent.com/kornelski/xml-rs/main/src/reader/parser.rs
- `memchr` 2.8.3 (Unlicense OR MIT) — SIMD byte search, already a transitive dependency of quick-xml and
  roxmltree; the fast-scan primitive if any project-owned scanning is needed. https://crates.io/crates/memchr

### Hashing and deterministic serialization

- **`sha2` 0.11.0** (2026-03-25), RustCrypto, MIT OR Apache-2.0, pure Rust, MSRV 1.85 (edition 2024), on
  `digest` 0.11 (`Sha256::digest(...)` via the `Digest` trait). No FIPS validation claim anywhere in its
  README (fine for content fingerprinting; not a crypto-conformance requirement here).
  https://crates.io/crates/sha2 https://raw.githubusercontent.com/RustCrypto/hashes/master/sha2/CHANGELOG.md
- Deterministic serialization: `serde` 1.0.229 / `serde_json` 1.0.151 (2026-07-18/20), MIT OR Apache-2.0.
  `serde_json::Map` is **BTreeMap-backed by default** (sorted deterministic key order); the
  `preserve_order` feature switches to IndexMap (insertion order). Structs serialize in field-declaration
  order. `serde_canonical_json` 1.0.0 (gelvinp/rs-serde_canonical_json) exists as a small canonical-JSON
  option. https://docs.rs/serde_json/latest/serde_json/map/index.html https://crates.io/crates/serde_canonical_json
- The canonical-rPr → hash pipeline itself (ADR 0009 lexical canonicalization, prefix stripping, sorting)
  is project logic; sha2 only supplies the digest.

### Atomic replace, durability, locks

- **`tempfile` 3.27.0** (2026-03-11), MIT OR Apache-2.0, MSRV 1.63, active. `TempPath::persist` =
  `rename(2)` on Unix (via rustix) and `MoveFileExW`+`MOVEFILE_REPLACE_EXISTING` on Windows; official
  wording: **"neither the file contents nor the containing directory are synchronized"** — fsync is
  deliberately NOT included; `persist_noclobber` uses `renameat`/NOREPLACE. `Builder` prefix/suffix;
  temp dir defaults to `env::temp_dir()`. https://docs.rs/crate/tempfile/latest
  https://docs.rs/crate/tempfile/3.27.0/source/src/file/mod.rs
  https://docs.rs/crate/tempfile/3.27.0/source/src/file/imp/windows.rs
  https://docs.rs/crate/tempfile/3.27.0/source/src/file/imp/unix.rs
- Durability: std `File::sync_all` ("sync all OS-internal file content and metadata to disk") /
  `sync_data` (skips metadata) are the primitives; no crate needed.
  https://doc.rust-lang.org/std/fs/struct.File.html
- `atomic-write-file` 0.3.1 (2026-08-11), BSD-3-Clause: implements write → `sync_all` → rename with
  openat/linkat/renameat directory-fd hardening on Unix and crash tests; **Windows path is a TODO**
  (CreateFileW + MoveFileEx `MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH`).
  https://docs.rs/crate/atomic-write-file/latest https://docs.rs/crate/atomic-write-file/0.3.1/source/src/imp/mod.rs
- Locks: **`fs2` is effectively unmaintained** (last release 0.4.3 in 2018-01-06; repo not archived but
  dormant). **`fs4` 1.1.0** (2026-04-28, MIT/Apache-2.0, MSRV 1.75) is its active fork — "Original fs2,
  now ... replace libc by rustix"; `FileExt::{lock, lock_shared, try_lock, try_lock_shared, unlock}` on
  `flock(2)` + `LockFileEx`. **`fd-lock` 4.0.4** (2025-03-10, MIT OR Apache-2.0) is the guard-based
  advisory `RwLock` crate (read/write/try_* → guards). `rustix::fs::flock` exists but is **Unix-only**
  (rustix covers Windows only in `net`). https://crates.io/crates/fs2 https://docs.rs/crate/fs4/latest
  https://docs.rs/fd-lock/latest/fd_lock/struct.RwLock.html https://docs.rs/rustix/latest/rustix/fs/fn.flock.html
- Crash semantics (why advisory locks sidestep stale-lock files): `flock(2)` — locks released when all
  fds for the open file description are closed (kernel does this on process death);
  `LockFileEx` — locks are released by the OS on process termination, with the MSDN caveat that timing
  depends on system resources and explicit unlock is recommended.
  https://man7.org/linux/man-pages/man2/flock.2.html
  https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex
- Windows atomic-replace semantics: `MoveFileExW` `MOVEFILE_REPLACE_EXISTING` (and
  `MOVEFILE_WRITE_THROUGH` — "does not return until the file is actually moved on the disk");
  `ReplaceFileW` preserves creation time/DACLs/streams and requires same volume.
  https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
  https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew
- "Windows cannot fsync a directory": no explicit primary-source statement found in the checked MSDN
  pages (UNKNOWN); the practical Windows durability path is WRITE_THROUGH rename semantics above.

### Unicode grapheme segmentation

- **`unicode-segmentation` 1.13.3** (2026-06-01), MIT/Apache-2.0, active. Unicode version is **coupled to
  the crate version**: 1.11.0 → Unicode 15.1; 1.12.0 → Unicode 16.0.0; 1.13.x → **Unicode 17.0.0**
  (1.13.0/1.13.1 yanked). Exports a `UNICODE_VERSION` constant; UAX #29 grapheme/word/sentence
  segmentation (`graphemes`, `grapheme_indices`, ...); no_std. Pinning a Unicode version = pinning the
  crate version (Cargo.toml + Cargo.lock); an older Unicode version requires an older crate release.
  https://raw.githubusercontent.com/unicode-rs/unicode-segmentation/master/README.md
  https://docs.rs/unicode-segmentation/latest/unicode_segmentation/constant.UNICODE_VERSION.html

### MCP stdio

- **`rmcp` 3.1.2** (2026-08-07) is the **official** Rust SDK (modelcontextprotocol/rust-sdk), Apache-2.0
  (declared on crates.io), very active (same-day pushes, 3.8k stars). Implements the stable MCP
  **2026-07-28** spec, back-compatible with 2025-11-25. stdio transport: `rmcp::transport::stdio` under
  the `transport-io` feature (server + client) — "the standard way to launch local MCP servers as child
  processes"; server = `service.serve(stdio())` with `#[tool]`/`#[tool_router]`/`#[tool_handler]` macros.
  Streamable HTTP server is a Tower service mountable on any router (axum example provided); legacy
  HTTP+SSE is a deliberate non-goal. Tokio-based.
  https://github.com/modelcontextprotocol/rust-sdk#transports https://crates.io/crates/rmcp
- Old official crates are superseded: `mcp-server` 0.1.0 (2025-02-27) was an early publish of this SDK
  under the old name; `mcp-core` 0.1.50 (2025-05-01) now points at a stale fork; the mcp-rs org is empty.
  Third-party `rust-mcp-schema` 0.10.3 (2026-06-24) is maintained but not the official path.
  https://crates.io/api/v1/crates/mcp-core https://github.com/orgs/mcp-rs/repositories

### Local HTTP server and capability token

- **`axum` 0.8.9** (2026-04-14), MIT, MSRV 1.80, active (tokio/hyper stack); small-server usage is
  `Router::new().route(...)` + `axum::serve(listener, app)`. https://crates.io/crates/axum
  https://github.com/tokio-rs/axum/blob/main/axum/README.md
- `tiny_http` 0.12.0 (2022-10-06) — sync, no tokio, but **stale** (no release since 2022);
  `rouille` 3.6.2 (2023-04-24) — dormant (>3y no release, not archived); `warp` 0.4.3 (2026-05-04) —
  **revived/active** (0.4.0 landed 2025-08-05). https://crates.io/crates/tiny_http https://crates.io/crates/rouille
  https://crates.io/crates/warp https://github.com/seanmonstar/warp/blob/master/CHANGELOG.md
- Auth middleware: `tower-http` 0.7.0 (2026-06-15) `ValidateRequestHeaderLayer::{accept, has_header_value,
  custom}` rejects mismatches with 403; **no bearer-token example and no constant-time comparison claim**
  — constant-time compare is the caller's job. https://docs.rs/tower-http/latest/tower_http/validate_request/index.html
- Token primitives: **`getrandom` 0.4.3** (2026-06-17) — on Windows 10+ uses `ProcessPrng`
  (bcryptprimitives.dll, raw-dylib), documented as preferable to `BCryptGenRandom` (registry-access
  crash history, sandbox issues) and to deprecated `CryptGenRandom`/`RtlGenRandom`; Linux uses the
  getrandom syscall. `rand` 0.10.2 (2026-07-11) exposes `OsRng` (implementation provided by getrandom,
  available as `rand_core::OsRng` / `rand::rngs::OsRng`).
  https://raw.githubusercontent.com/rust-random/getrandom/master/src/backends/windows.rs
  https://docs.rs/rand_core/0.9.2/rand_core/struct.OsRng.html
- **`subtle` 2.6.1** (2024-06-24), MIT OR Apache-2.0: `ConstantTimeEq::ct_eq(&self, other) -> Choice`,
  "constant time provided that a) the bitwise operations are constant-time ...".
  https://docs.rs/subtle/latest/subtle/trait.ConstantTimeEq.html
  https://raw.githubusercontent.com/dalek-cryptography/subtle/main/README.md
- `zeroize` 1.9.0 (2026-06-12) / `secrecy` 0.10.3 (2024-10-09, iqlusioninc/crates): optional in-memory
  scrubbing for the token. https://crates.io/crates/zeroize https://crates.io/crates/secrecy

## Contract-to-candidate matrix

| Contract (repo source) | Candidate (verified) | Verified capability | Gap → project-owned code |
|---|---|---|---|
| Rebuild ZIP preserving per-entry metadata + order (ADR 0013, `typed_docx.py:2598`) | `zip` 8.6.0 `FileOptions` | compression method/level, timestamps, unix perms, `system`, extra data; writing order = entry order | No general-purpose-flags setter (auto); timestamps/flags preservation vs. corpus must be validated |
| Untouched parts replayed verbatim incl. compressed bytes (verification.md; ADR 0004) | `zip` `by_index_raw` + `raw_copy_file(_rename/_touch)`, `merge_archive` | raw compressed bytes copied without recompression | Extra fields not auto-carried → re-add path; header regeneration (not full-file byte identity) |
| No-op build byte-identical (verification.md) | copy-if-unchanged fast path (any crate: std fs::copy) | trivial | Container-level byte identity is NOT provided by any crate on the rebuild path — must define the gate (see #41) |
| Streaming namespace-aware XML, exact byte ranges (byte-surgery PRD) | `quick-xml` 0.41.0 (`NsReader`, `buffer_position`, `Span`) | streaming, borrowed events, byte offsets, prefix resolution | Walker discipline (nesting stack, self-close classification, splice ranges, CJK offsets) stays project-owned per PRD; quick-xml is the tokenizer primitive, not the walker |
| Byte-range tokenizer alternative (same contract) | `xmlparser` 0.13.6 (`StrSpan`) | zero-alloc ranges, no deps | No namespace resolution at all — project code would own it; dormant release cadence |
| DOM with byte ranges (verify-side only) | `roxmltree` 0.21.1 (`Node::range` + `input_text`) | exact original-byte slices, namespaces | Non-streaming; text content unescaped ≠ raw bytes |
| SHA-256 fingerprints (ADR 0004/0009, `typed_core.py:167`) | `sha2` 0.11.0 | `Sha256::digest` | Canonical rPr form is project logic (ADR 0009) |
| Deterministic serialization (manifest/format.json) | `serde`+`serde_json` | BTreeMap-backed Map = sorted deterministic keys; struct declaration order | Canonical JSON variant (if needed) — `serde_canonical_json` or project order policy |
| Atomic single-file replace (ADR 0022) | `tempfile` 3.27.0 `persist` | atomic rename/MoveFileEx replace; noclobber | fsync deliberately NOT included — ordering policy is project code |
| Durability (crash consistency, ticket #34) | std `sync_all`/`sync_data`; `atomic-write-file` 0.3.1 (Unix-hardened) | per-file fsync primitives | Multi-file workdir/queue publication order + journal is project-owned; Windows durable-rename via WRITE_THROUGH (atomic-write-file Windows is TODO) |
| File locks + stale-lock recovery (ticket #34) | `fd-lock` 4.0.4 / `fs4` 1.1.0; `fs2` 0.4.3 stale | advisory flock/LockFileEx, kernel-released on crash (man7, MSDN) | If lock *files* are used instead of advisory locks, stale detection is project code; avoid PID-file schemes |
| Grapheme segmentation pinned to a Unicode version (ADR 0034) | `unicode-segmentation` 1.13.x = Unicode 17.0.0 | graphemes/indices; `UNICODE_VERSION`; version-coupled UCD | Choose the pinned Unicode version (must match vertical catalog pin) and the crate version in Cargo.lock |
| MCP stdio server (pyproject, mcp_server.py) | `rmcp` 3.1.2 (`transport-io`) | official SDK, stdio server+client, tool macros | Tool/schema adapter mapping existing MCP surface; tokio pulled in |
| Local review HTTP server (review_server.py) | `axum` 0.8.9 (active); `warp` 0.4.3 (revived); `tiny_http`/`rouille` stale | routing + tower middleware; small-server patterns | Capability-token lifecycle/storage is project-owned (nothing exists in Python today) |
| Capability token: entropy + constant-time compare | `getrandom` 0.4.3 (ProcessPrng on Win10+); `subtle` 2.6.1 `ct_eq`; optional `zeroize`/`secrecy` | OS CSPRNG; ct comparison | Token issuance/rotation/storage; tower-http makes no ct claim — compare in project code or accept timing difference on localhost |

## Constraints and risks

- **MSRV floor**: `zip` 1.88 dominates (`sha2` 1.85, `axum` 1.80, `quick-xml` 1.79, `fs4` 1.75, `xml` 1.70,
  `tempfile` 1.63). Decide toolchain policy in #39.
- **Licensing**: all recommended candidates are MIT/Apache-2.0 family; `atomic-write-file` is BSD-3-Clause;
  `memchr` is Unlicense OR MIT. No copyleft observed. `rmcp` declares Apache-2.0.
- **tokio coupling**: `rmcp` (stdio) and `axum` both pull the tokio/hyper stack; a sync core with async
  transport shells needs a deliberate seam (#36). `tiny_http`/`warp` don't avoid tokio either (warp is
  tokio-based).
- **`zip` API churn**: 8.x → 9.0.0-pre3 in progress; 8.1.0+ is the current stable line for
  `raw_copy_file_touch` and `system`. Pin a stable 8.x for the start spec, revisit 9.x.
- **quick-xml caveats**: prefix resolution does not change element-name matching; `expand_empty_elements`
  synthesizes End events (offset effect undocumented); UTF-16 input needs a wrapper; DTD is opaque.
- **Windows-specific**: LockFileEx unlock timing depends on system resources; durable rename relies on
  WRITE_THROUGH/ReplaceFile semantics; no primary source for "cannot fsync a directory".
- **Grapheme version coupling**: upgrading `unicode-segmentation` silently changes segmentation results
  (e.g. 15.1 → 17.0 rule changes) — the pin must be deliberate and recorded in the audit trail like the
  vertical catalog version (ADR 0027/0034).

## Viable minimal dependency sets (options, not a decision)

- **Option A — lean sync core + async shells**: `zip` (raw copy), `quick-xml`, `sha2`, `tempfile`,
  `fd-lock` (or `fs4`), `unicode-segmentation`, `serde`/`serde_json`, plus `rmcp` (stdio) and `axum`
  confined behind transport adapters; `getrandom` + `subtle` for the token. Matches every contract with
  the smallest set of active crates. MSRV floor 1.88.
- **Option B — same core, lighter HTTP**: replace `axum` with `warp` 0.4.3 (revived) — still tokio;
  or accept a stale `tiny_http` for a purely sync HTTP shell (not recommended for new code).
- **Option C — zero XML dependency**: hand-rolled tokenizer over `memchr` instead of `quick-xml`.
  Re-derives the offset/namespace discipline the byte-surgery PRD wants centralized; not recommended —
  quick-xml already supplies offsets + namespace resolution as the tokenizer primitive, with the walker
  staying project-owned per the PRD.
- **Container byte-identity (only if #41 tightens the gate)**: copy-if-unchanged fast path + `raw_copy_file`
  rebuild for changed entries; central-directory/EOCD regeneration (offset math) would then be the only
  project-owned ZIP code. Without the tightening, plain re-encode matches today's Python behavior.

## Project-owned gaps (no crate satisfies)

1. **The XML walker** (byte-surgery PRD): nesting stack, open/close/self-close classification, byte-range
   splicing, CJK bytes-vs-str offsets. quick-xml supplies the event stream + offsets; the walker product
   remains project code by design.
2. **Canonical rPr → SHA-256 pipeline** (ADR 0009): lexical canonicalization is project logic; sha2 only
   digests.
3. **Multi-file transactional publication** (ADR 0022 + ticket #34): tempfile gives single-file atomic
   replace; ordering (fsync file → rename → fsync parent on Unix; WRITE_THROUGH on Windows), journals,
   and idempotent recovery for workdirs/review queues are project-owned.
4. **Capability-token lifecycle**: no crate; token generation (getrandom), storage, header check, and
   constant-time comparison (subtle) assembled in project code; the Python server has no token today.
5. **Full-file byte-identical container output**: not provided by any crate on a rebuild path; only a
   copy-if-unchanged fast path (or hand-written central-directory splice) achieves it — depends on the
   #41 gate definition.

## Recommendation inputs for the downstream tickets

- **#41 (start spec / go-no-go)** — define the no-op byte-identity gate precisely: part-content level
  (current Python behavior: container rebuilt, deflate re-encoded) vs. container level (copy-if-unchanged
  + raw-copy rebuild). Decide whether `raw_copy_file`'s regenerated headers + manual extra-field re-add
  are acceptable for touched rebuilds. Decide the pinned Unicode version for grapheme segmentation and
  whether it must equal the vertical-catalog pin (ADR 0034).
- **#36 (engine interface/seams)** — adopt `quick-xml` as the tokenizer primitive while keeping the walker
  project-owned (per byte-surgery PRD); place the tokio boundary so `rmcp` stdio + `axum` don't force an
  async engine core; decide the ZIP write strategy (raw-copy vs re-encode) as an engine-level policy;
  decide deterministic JSON policy (BTreeMap ordering vs `serde_canonical_json`).
- **#34 (crash consistency/locking)** — prefer OS advisory locks (`fd-lock`/`fs4`; kernel-released on
  crash per man7/MSDN) over lock-file-with-PID schemes, so "stale lock recovery" is automatic; define the
  fsync ordering contract for multi-file publication (Unix parent-dir fsync; Windows WRITE_THROUGH rename);
  account for Windows LockFileEx unlock lag and process-kill timing in fault-injection evidence.
- **#39 (packaging/host integration)** — set MSRV policy (floor 1.88 via `zip`), confirm tokio inclusion
  in the shipped binary is acceptable, and validate `rmcp` stdio + Streamable HTTP choice against the
  existing MCP tool surface (33/33 smoke suite must map 1:1).

## Unresolved unknowns

- `quick-xml` `expand_empty_elements` effect on `buffer_position` (undocumented).
- `roxmltree` `Node::range()` subtree-span semantics (element range = whole subtree?) — undocumented detail.
- Windows "cannot fsync a directory": no primary-source statement found; Windows durability rests on
  WRITE_THROUGH/ReplaceFile semantics instead.
- `tower-http` `has_header_value` comparison is not documented as constant-time — whether localhost-only
  tokens need ct comparison is a policy choice.
- Whether corpus DOCX entries rely on ZIP general-purpose flags that `zip` auto-manages differently
  (UTF-8 bit 11, data-descriptor bit 3) — validate against the fixture corpus in design.
- `zip` 9.x stabilization timeline (9.0.0-pre3 at research date) and any raw-copy API changes.

## Sources

- Repo: `docs/adr/0004,0009,0013,0022,0027,0034`, `docs/prd/byte-surgery-layer.md`, `verification.md`,
  `scripts/typed_docx.py`, `scripts/typed_core.py`, `scripts/mcp_server.py`, `scripts/review_server.py`,
  `pyproject.toml` (paths above).
- crates.io / docs.rs / GitHub for every crate cited above (URLs inline).
- man7.org flock(2); MSDN LockFileEx/MoveFileExW/ReplaceFileW; Unicode UAX #29 via
  unicode-segmentation CHANGELOG (URLs inline).
