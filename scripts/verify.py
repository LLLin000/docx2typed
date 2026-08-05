"""CLI wrapper for independent typed-mode verification."""

try:
    from .typed_docx import validate, verify, verify_workdir
except ImportError:
    from typed_docx import validate, verify, verify_workdir

__all__ = ["validate", "verify", "verify_workdir"]
