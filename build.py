"""CLI wrapper for typed-mode transactional building."""

try:
    from .typed_docx import build, build_workdir
except ImportError:
    from typed_docx import build, build_workdir

__all__ = ["build", "build_workdir"]
