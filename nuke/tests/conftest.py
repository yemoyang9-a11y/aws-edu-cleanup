"""pytest fixtures for the nuke module (helpers live in nuke_test_support.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nuke_test_support import aws_nuke_bin, resource_docs  # noqa: E402,F401  (re-exported fixtures)
