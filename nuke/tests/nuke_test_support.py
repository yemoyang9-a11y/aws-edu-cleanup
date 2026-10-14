"""Helpers shared by the nuke tests (unique module name: other modules also have a conftest.py)."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

NUKE_DIR = Path(__file__).resolve().parents[1]
REPO = NUKE_DIR.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(NUKE_DIR))

# aws-nuke v3.68.3 source checkout (docs/resources/*.md = one page per registered resource type)
# Default: a sibling checkout (../aws-nuke next to this repo); override with AWS_NUKE_SRC.
AWS_NUKE_SRC = Path(os.environ.get("AWS_NUKE_SRC", str(REPO.parent / "aws-nuke")))
DOCS_RESOURCES = AWS_NUKE_SRC / "docs" / "resources"


def _parse_resource_docs():
    """{type_name: {"props": [...], "settings": [...]}} from docs/resources/*.md (v3.68.3)."""
    out = {}
    for f in sorted(DOCS_RESOURCES.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        m = re.search(r"## Resource\s+```text\s+(\S+)\s+```", text)
        if not m:
            continue
        props = re.findall(r"^- `([^`]+)`:", text, re.M)
        sm = re.search(r"## Settings\s+((?:- `[^`]+`\s*)+)", text)
        settings = re.findall(r"`([^`]+)`", sm.group(1)) if sm else []
        out[m.group(1)] = {"props": props, "settings": settings, "file": f.name}
    return out


@pytest.fixture(scope="session")
def resource_docs():
    if not DOCS_RESOURCES.is_dir():
        pytest.skip(f"aws-nuke source not found at {AWS_NUKE_SRC} (set AWS_NUKE_SRC)")
    docs = _parse_resource_docs()
    assert len(docs) > 500, "docs/resources looks incomplete"
    return docs


@pytest.fixture(scope="session")
def aws_nuke_bin():
    """A real aws-nuke binary if one is available ($AWS_NUKE_BIN, PATH, or the run_nuke.sh cache)."""
    cands = [os.environ.get("AWS_NUKE_BIN"), shutil.which("aws-nuke"),
             str(Path.home() / ".cache/edu-cleanup/bin/aws-nuke")]
    for c in cands:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            try:
                out = subprocess.run([c, "version"], capture_output=True, text=True, timeout=30).stdout
            except (OSError, subprocess.SubprocessError):
                continue
            if "v3." in out:
                return c
    pytest.skip("no aws-nuke v3 binary (set AWS_NUKE_BIN)")


def run_jq(filter_file, data_path=None, *, raw_input=False, null_input=False, args=None, stdin=None):
    jq = shutil.which("jq")
    if not jq:
        pytest.skip("jq not installed")
    cmd = [jq]
    if raw_input:
        cmd += ["-R", "-s"]
    if null_input:
        cmd += ["-n"]
    cmd += ["-f", str(filter_file)]
    for kind, name, value in (args or []):
        cmd += [kind, name, value if isinstance(value, str) else json.dumps(value)]
    if data_path:
        cmd.append(str(data_path))
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)
