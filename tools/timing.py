#!/usr/bin/env python3
"""How long did each step of a cleanup.sh run take?

    python3 tools/timing.py                 # latest run in ~/.edu-cleanup-runs
    python3 tools/timing.py 123456789012    # latest run of that account
    python3 tools/timing.py ~/.edu-cleanup-runs/123456789012-20261015-0615

Runs made by a cleanup.sh that records stage marks ($RUN/timing.tsv) are timed exactly.
Older runs are estimated from when each step wrote its result file (labelled "추정").
Standard library only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

RUNS = Path(os.environ.get("HOME", "~")).expanduser() / ".edu-cleanup-runs"
DIR_RE = re.compile(r"^(\d{12})-(\d{8}-\d{4})$")


def fmt(sec: float) -> str:
    sec = max(0, int(round(sec)))
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return (f"{h}h" if h else "") + (f"{m}m" if h or m else "") + f"{s:02d}s"


def dwidth(text: str) -> int:
    """Terminal width: Korean characters take two columns."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - dwidth(text))


def find_run(arg: Optional[str]) -> Optional[Path]:
    if arg and Path(arg).expanduser().is_dir():
        return Path(arg).expanduser()
    if not RUNS.is_dir():
        return None
    dirs = [d for d in RUNS.iterdir() if d.is_dir() and DIR_RE.match(d.name)
            and (not arg or d.name.startswith(arg + "-"))]
    return max(dirs, key=lambda d: d.name[13:]) if dirs else None


def load(p: Path) -> Dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mtime(p: Path) -> float:
    return p.stat().st_mtime


def exact_stages(run: Path) -> Optional[List[Tuple[str, float]]]:
    """[(label, seconds)] from timing.tsv lines '<epoch>\\t<label>' (the last label is 'end')."""
    f = run / "timing.tsv"
    if not f.is_file():
        return None
    marks = []
    for line in f.read_text(encoding="utf-8").splitlines():
        t, _, label = line.partition("\t")
        try:
            marks.append((float(t), label.strip()))
        except ValueError:
            continue
    if len(marks) < 2:
        return None
    return [(label, marks[i + 1][0] - t) for i, (t, label) in enumerate(marks[:-1])]


def _nuke_files(run: Path, tag: str) -> List[Path]:
    return sorted(run.glob(f"nuke-{tag}-*.json"), key=mtime)


def _span(files: List[Path]) -> Tuple[float, float]:
    first = files[0]
    start = mtime(first) - float(load(first).get("duration_s") or 0)
    return start, mtime(files[-1])


def estimated_stages(run: Path, log: Optional[Path]) -> List[Tuple[str, float]]:
    """Best effort for runs without timing.tsv: boundaries are result-file write times."""
    m = DIR_RE.match(run.name)
    t0 = dt.datetime.strptime(m.group(2), "%Y%m%d-%H%M").timestamp() if m else None
    out: List[Tuple[str, float]] = []
    cur = t0

    def add(label: str, until: float) -> None:
        nonlocal cur
        if cur is not None and until >= cur - 1:
            out.append((label, until - cur))
        cur = until

    prev = _nuke_files(run, "preview")
    if prev:
        s, e = _span(prev)
        add("미리보기: 해지 전 점검 + 스택 조회", s)
        add("미리보기: nuke 조회 (지우지 않음)", e)
    passes = sorted({int(x) for x in (re.findall(r"nuke-pass(\d+)-", " ".join(p.name for p in run.glob("nuke-pass*-*.json"))))})
    for n in passes:
        cfn = run / f"cfn-pass{n}.json"
        if n == 1 and not cfn.exists() and (run / "cfn.json").exists():
            cfn = run / "cfn.json"              # runs made before stacks were retried every pass
        before = "계정 번호 입력 대기 + " if n == 1 else "기본 VPC 재생성 + 확인 + 회차 사이 대기 + "
        files = _nuke_files(run, f"pass{n}")
        s, e = _span(files)
        if cfn.exists():
            add(f"{before}스택 삭제 ({n}회차)", mtime(cfn))
        else:
            add(before.rstrip(" +") + f" ({n}회차 시작 전)", s)
        add(f"nuke 삭제 ({n}회차)", e)
    if (run / "verify.json").exists():
        add("기본 VPC 재생성 + 남은 리소스 확인 (마지막 회차)", mtime(run / "verify.json"))
    if (run / "preclose.json").exists():
        add("해지 전 점검 (적용)", mtime(run / "preclose.json"))
    if log and log.exists():
        add("결과 보고서", mtime(log))
    return out


def nuke_regions(run: Path) -> List[str]:
    lines = []
    tags = ["preview"] + [f"pass{n}" for n in sorted({int(x) for x in re.findall(
        r"nuke-pass(\d+)-", " ".join(p.name for p in run.glob("nuke-pass*-*.json")))})]
    for tag in tags:
        regs: Dict[str, float] = {}
        for f in _nuke_files(run, tag):
            for r, v in (load(f).get("regions") or {}).items():
                regs[r] = float((v or {}).get("duration_s") or 0)
        if regs:
            top = sorted(regs.items(), key=lambda kv: -kv[1])[:3]
            name = "미리보기" if tag == "preview" else tag.replace("pass", "") + "회차"
            lines.append(f"  {name}: " + ", ".join(f"{r} {fmt(s)}" for r, s in top))
    return lines


def main(argv: List[str]) -> int:
    run = find_run(argv[0] if argv else None)
    if not run:
        print("실행 기록을 찾지 못했어요 (~/.edu-cleanup-runs). cleanup.sh를 실행한 CloudShell에서 실행하세요.")
        return 2
    acct, _, ts = run.name.partition("-")
    log = Path(os.environ.get("HOME", "~")).expanduser() / f"cleanup-{run.name}.log"
    stages = exact_stages(run)
    exact = stages is not None
    if not exact:
        stages = estimated_stages(run, log)
    if not stages:
        print(f"{run}: 시간을 계산할 결과 파일이 없어요 (실행이 중간에 끊겼을 수 있어요).")
        return 1
    print(f"계정 {acct} | 실행 {ts} | " + ("단계 기록 기준 (정확)" if exact else "결과 파일 시각 기준 (추정, ±1분)"))
    width = max(dwidth(label) for label, _ in stages)
    total = 0.0
    for label, sec in stages:
        total += sec
        print(f"  {pad(label, width)}  {fmt(sec):>8}")
    print(f"  {pad('총 소요', width)}  {fmt(total):>8}")
    label, sec = max(stages, key=lambda x: x[1])
    print(f"가장 오래 걸린 단계: {label} ({fmt(sec)}, 전체의 {sec / total * 100 if total else 0:.0f}%)")
    regions = nuke_regions(run)
    if regions:
        print("nuke에서 오래 걸린 리전 (상위 3개):")
        print("\n".join(regions))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
