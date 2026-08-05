# 0004 — Guarantee template package integrity, not pagination invariance

Typed mode must preserve every DOCX package part and every non-editable XML region except the explicitly allowed paragraph changes in `word/document.xml`. extract records a per-part content manifest and a template fingerprint; build rejects a mismatched template, patches only the allowed XML, verifies the manifest, and commits output atomically. Text-length changes may cause Word's normal line/page reflow; pagination invariance is not part of the v2.0 contract.
