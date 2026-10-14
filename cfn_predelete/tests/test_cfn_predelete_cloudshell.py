"""The generated CloudShell block recreates the files byte-for-byte."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))
import make_cloudshell_block as mcb  # noqa: E402


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_block_recreates_files_and_runs_dry_run_last(tmp_path):
    block = mcb.build_block("ap-northeast-2,us-east-1", 25)
    body, run_line = block.rstrip("\n").rsplit("\n", 1)
    assert run_line.startswith("python3 ") and "read -r -p" in run_line
    assert run_line.index("--json-out ~/cfn-predelete-dryrun.json") < run_line.index("--apply")
    env = dict(os.environ, HOME=str(tmp_path))
    subprocess.run(["bash", "-euo", "pipefail", "-c", body], check=True, env=env)
    for rel in mcb.FILES:
        assert (tmp_path / "edu-cleanup" / rel).read_bytes().rstrip(b"\n") == \
            (mcb.ROOT / rel).read_bytes().rstrip(b"\n")
    assert (tmp_path / "edu-cleanup/common/__init__.py").exists()
    # the recreated script is importable/compilable on its own
    subprocess.run([sys.executable, "-m", "py_compile",
                    str(tmp_path / "edu-cleanup/cfn_predelete/predelete_stacks.py")], check=True)
    out = subprocess.run([sys.executable, str(tmp_path / "edu-cleanup/cfn_predelete/predelete_stacks.py"), "--help"],
                         check=True, capture_output=True, text=True, env=env).stdout
    assert "--timeout-min" in out and "--cfn-role-arn" in out and "--apply" in out


def test_readme_contains_current_block():
    """README's copy-paste block must match the current script (run make_cloudshell_block.py --update-readme)."""
    expected = mcb.readme_section(mcb.build_block("ap-northeast-2,us-east-1", 25.0))
    assert mcb.readme_block() == expected
