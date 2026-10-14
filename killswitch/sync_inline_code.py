#!/usr/bin/env python3
"""Copy killswitch/lambda_src.py into killswitch.yaml (Code.ZipFile block).

lambda_src.py is the source of truth; tests/test_killswitch_template.py fails
if the two drift apart. Usage:
    python3 killswitch/sync_inline_code.py           # rewrite the template
    python3 killswitch/sync_inline_code.py --check   # exit 1 if out of sync
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "killswitch.yaml"
SOURCE = HERE / "lambda_src.py"
MARKER = "ZipFile: |"


def render(template_text: str, source_text: str) -> str:
    lines = template_text.splitlines(keepends=True)
    idx = next(i for i, line in enumerate(lines) if line.strip() == MARKER)
    indent = " " * (len(lines[idx]) - len(lines[idx].lstrip()) + 2)
    end = idx + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(indent)):
        end += 1
    # keep blank lines that separate the block from the next key
    while end > idx + 1 and not lines[end - 1].strip():
        end -= 1
    body = [(indent + line if line.strip() else "\n") for line in source_text.splitlines(keepends=True)]
    return "".join(lines[: idx + 1] + body + lines[end:])


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    current = TEMPLATE.read_text(encoding="utf-8")
    wanted = render(current, SOURCE.read_text(encoding="utf-8"))
    if "--check" in argv:
        if current != wanted:
            print("killswitch.yaml inline code is out of sync with lambda_src.py", file=sys.stderr)
            return 1
        return 0
    if current != wanted:
        TEMPLATE.write_text(wanted, encoding="utf-8")
        print("updated %s" % TEMPLATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
