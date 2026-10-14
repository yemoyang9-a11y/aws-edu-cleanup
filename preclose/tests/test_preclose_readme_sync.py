"""The README's CloudShell block recreates common/session.py and preclose_check.py verbatim;
fail if either drifts from the real files."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _embedded(readme: str, opener: str, closer: str) -> str:
    i = readme.index(opener) + len(opener) + 1
    j = readme.index("\n" + closer + "\n", i) + 1
    return readme[i:j]


def test_preclose_readme_block_matches_sources():
    readme = (ROOT / "preclose" / "README.md").read_text(encoding="utf-8")
    assert _embedded(readme, "cat > \"$D/common/session.py\" <<'SESSION_EOF'", "SESSION_EOF") == \
        (ROOT / "common" / "session.py").read_text(encoding="utf-8")
    assert _embedded(readme, "cat > \"$D/preclose/preclose_check.py\" <<'PRECLOSE_EOF'", "PRECLOSE_EOF") == \
        (ROOT / "preclose" / "preclose_check.py").read_text(encoding="utf-8")
