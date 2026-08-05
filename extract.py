"""CLI wrapper for typed-mode extraction."""

try:
    from .typed_docx import extract, extract_workdir
except ImportError:
    from typed_docx import extract, extract_workdir

__all__ = ["extract", "extract_workdir"]
