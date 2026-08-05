# 0022 — Commit DOCX output transactionally

Build writes a temporary DOCX beside the requested output, runs typed validation, byte patching, package-manifest checks, and independent verify, then atomically replaces the final output only after all pass. Failed or interrupted builds must not publish a partial document.
