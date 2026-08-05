# 0019 — Represent unsupported nodes as read-only opaque tokens

Unsupported nodes in direct-body paragraphs are extracted as typed opaque tokens with raw XML in the sidecar. Clean view shows a diagnostic placeholder; an untouched paragraph can replay the token, but any edit to that paragraph fails validation. v2.0 never flattens field results, drawings, or revisions into ordinary text.
