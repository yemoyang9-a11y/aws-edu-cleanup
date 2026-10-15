"""tools/timing.py: exact (timing.tsv) and estimated (result-file mtimes) stage durations."""
import json
import os
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "timing.py"
ACCT = "123456789012"


def run_tool(home, *args):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, env=env, timeout=30)


def write(p, data, t):
    p.write_text(json.dumps(data))
    os.utime(p, (t, t))


def test_exact_from_marks(tmp_path):
    run = tmp_path / ".edu-cleanup-runs" / f"{ACCT}-20261015-0615"
    run.mkdir(parents=True)
    t = 1_760_000_000
    (run / "timing.tsv").write_text(
        f"{t}\t미리보기: 해지 전 점검\n{t + 90}\tnuke 삭제 (1회차)\n{t + 90 + 245}\t남은 리소스 확인 (1회차)\n"
        f"{t + 90 + 245 + 150}\tend\n")
    write(run / "nuke-pass1-1.json", {"regions": {"ap-northeast-2": {"duration_s": 61},
                                                  "us-east-1": {"duration_s": 40}}}, t + 300)
    p = run_tool(tmp_path, ACCT)
    assert p.returncode == 0, p.stderr
    assert "정확" in p.stdout
    assert "1m30s" in p.stdout and "4m05s" in p.stdout and "2m30s" in p.stdout
    assert "총 소요" in p.stdout and "8m05s" in p.stdout
    assert "가장 오래 걸린 단계: nuke 삭제 (1회차) (4m05s" in p.stdout
    assert "1회차: ap-northeast-2 1m01s, us-east-1 40s" in p.stdout


def test_estimated_from_file_times(tmp_path):
    run = tmp_path / ".edu-cleanup-runs" / f"{ACCT}-20261015-0615"
    run.mkdir(parents=True)
    import datetime as dt
    t0 = dt.datetime(2026, 10, 15, 6, 15).timestamp()
    write(run / "nuke-preview-1.json", {"duration_s": 20}, t0 + 180)        # preview nuke starts t0+160
    write(run / "nuke-preview-z-global.json", {"duration_s": 13}, t0 + 300)
    write(run / "cfn-pass1.json", {}, t0 + 360)
    write(run / "nuke-pass1-1.json", {"duration_s": 30}, t0 + 390)
    write(run / "nuke-pass1-z-global.json", {"duration_s": 20}, t0 + 600)
    write(run / "verify.json", {}, t0 + 750)
    write(run / "preclose.json", {}, t0 + 840)
    log = tmp_path / f"cleanup-{ACCT}-20261015-0615.log"
    write(log, {}, t0 + 845)
    p = run_tool(tmp_path)
    assert p.returncode == 0, p.stderr
    assert "추정" in p.stdout
    out = p.stdout
    assert "미리보기: 해지 전 점검 + 스택 조회" in out and "2m40s" in out
    assert "계정 번호 입력 대기 + 스택 삭제 (1회차)" in out
    assert "nuke 삭제 (1회차)" in out and "4m00s" in out
    assert "총 소요" in out and "14m05s" in out


def test_older_run_with_single_cfn_file(tmp_path):
    run = tmp_path / ".edu-cleanup-runs" / f"{ACCT}-20261015-0615"
    run.mkdir(parents=True)
    import datetime as dt
    t0 = dt.datetime(2026, 10, 15, 6, 15).timestamp()
    write(run / "cfn.json", {}, t0 + 1800)
    write(run / "nuke-pass1-1.json", {"duration_s": 60}, t0 + 2000)
    p = run_tool(tmp_path, str(run))
    assert p.returncode == 0, p.stderr
    assert "스택 삭제 (1회차)" in p.stdout and "30m00s" in p.stdout


def test_no_runs(tmp_path):
    p = run_tool(tmp_path)
    assert p.returncode == 2 and "찾지 못했어요" in p.stdout
