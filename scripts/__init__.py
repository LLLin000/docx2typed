"""docx2typed — typed-mode DOCX text editing with locked structure."""

import json
import sys
import zipfile
from pathlib import Path

try:
    from .extract import extract
    from .view import view
    from .build import build
    from .verify import validate, verify
    from .typed_core import TypedError
    from .typed_docx import validate_workdir
except ImportError:  # direct script execution has no package context.
    from extract import extract
    from view import view
    from build import build
    from verify import validate, verify
    from typed_core import TypedError
    from typed_docx import validate_workdir

try:
    from .typed_normalize import normalize
except ImportError:
    from typed_normalize import normalize

try:
    from .audit import audit
except ImportError:
    from audit import audit

try:
    from .edit import edit, require_clean_edit
except ImportError:
    from edit import edit, require_clean_edit

try:
    from .decisions import decide
except ImportError:
    from decisions import decide

try:
    from .protocol import diagnostic, engine_descriptor, result_envelope, typed_path
except ImportError:
    from protocol import diagnostic, engine_descriptor, result_envelope, typed_path


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _validate_json(argv):
    if len(argv) != 1:
        _print_json(result_envelope(
            "validate",
            "failure",
            diagnostics=[diagnostic(
                "invalid-arguments",
                "validate requires exactly one workdir",
                details={"expected": ["workdir"], "actual": argv},
            )],
        ))
        return 2
    try:
        if not Path(argv[0]).is_dir():
            raise FileNotFoundError(f"typed workdir not found: {Path(argv[0]).resolve()}")
        checked = validate_workdir(argv[0])
        require_clean_edit(argv[0])
    except FileNotFoundError as exc:
        failure = diagnostic("workdir-not-found", str(exc))
    except PermissionError as exc:
        failure = diagnostic("workdir-unreadable", str(exc))
    except (zipfile.BadZipFile, TypedError) as exc:
        failure = diagnostic("workdir-invalid", str(exc))
    except OSError as exc:
        failure = diagnostic("workdir-unreadable", str(exc))
    else:
        _print_json(result_envelope(
            "validate",
            "success",
            data={
                "valid": True,
                "workdir": typed_path(checked.path),
                "warnings": checked.warnings,
            },
        ))
        return 0
    _print_json(result_envelope("validate", "failure", diagnostics=[failure]))
    return 1


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    json_mode = "--json" in argv
    argv = [arg for arg in argv if arg != "--json"]
    if argv == ["--version"]:
        descriptor = engine_descriptor()
        if json_mode:
            _print_json(descriptor)
        else:
            print(f"{descriptor['name']} {descriptor['version']} ({descriptor['build_commit']})")
        return 0
    if not argv:
        print(__doc__)
        return 1
    command = argv[0]
    if json_mode and command == "validate":
        return _validate_json(argv[1:])
    if command == "mcp":
        try:
            from .mcp_server import main as mcp_main
        except ImportError:
            from mcp_server import main as mcp_main
        mcp_main()
        return 0
    if command == "review":
        try:
            from .review_server import main as review_main
        except ImportError:
            from review_server import main as review_main
        return review_main(argv[1:])
    commands = {
        "extract": extract,
        "view": view,
        "validate": validate,
        "build": build,
        "verify": verify,
        "normalize": normalize,
        "audit": audit,
        "edit": edit,
        "decide": decide,
    }
    if command in commands:
        return commands[command](argv[1:])
    print(f"Unknown command: {command}\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
