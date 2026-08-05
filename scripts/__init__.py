"""docx2typed — typed-mode DOCX text editing with locked structure."""

import sys

try:
    from .extract import extract
    from .view import view
    from .build import build
    from .verify import validate, verify
except ImportError:  # direct script execution has no package context.
    from extract import extract
    from view import view
    from build import build
    from verify import validate, verify

try:
    from .typed_normalize import normalize
except ImportError:
    from typed_normalize import normalize

try:
    from .audit import audit
except ImportError:
    from audit import audit

try:
    from .edit import edit
except ImportError:
    from edit import edit


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    command = argv[0]
    commands = {
        "extract": extract,
        "view": view,
        "validate": validate,
        "build": build,
        "verify": verify,
        "normalize": normalize,
        "audit": audit,
        "edit": edit,
    }
    if command in commands:
        return commands[command](argv[1:])
    print(f"Unknown command: {command}\n{__doc__}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
