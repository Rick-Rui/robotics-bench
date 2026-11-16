"""Vendor the case kit into a case (each build context must be self-contained).

    python tools/case-kit/sync.py <task dir>            # copy casekit/*.py into every casekit/ directory of the task
    python tools/case-kit/sync.py --check <task dir>    # fail if a copy differs or a stale file is left (CI)

Create the casekit/ directories you want (usually environment/service/casekit/ and tests/casekit/) and run it once;
never edit the copies. Files you share between your own service and verifier (a simulator module, the scene) are
yours to keep identical; the gate fails when they diverge, because the replay no longer matches.
"""
import argparse
import filecmp
import shutil
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent / "casekit"


def sources():
    return sorted(p for p in KIT.glob("*.py"))


def targets(task):
    task = Path(task).resolve()
    return sorted(d for d in task.rglob("casekit") if d.is_dir() and d.resolve() != KIT)


def problems(task):
    out = []
    dirs = targets(task)
    if not dirs:
        out.append(f"{task}: no casekit/ directory (create environment/service/casekit and tests/casekit)")
    names = {p.name for p in sources()}
    for d in dirs:
        for src in sources():
            dst = d / src.name
            if not dst.is_file() or not filecmp.cmp(src, dst, shallow=False):
                out.append(f"{dst}: differs from tools/case-kit/casekit/{src.name}")
        for f in d.iterdir():
            if f.is_file() and f.name not in names:
                out.append(f"{f}: not part of the kit")
    return out


def sync(task):
    for d in targets(task):
        for src in sources():
            shutil.copyfile(src, d / src.name)
        for f in d.iterdir():
            if f.is_file() and f.name not in {p.name for p in sources()}:
                f.unlink()
        shutil.rmtree(d / "__pycache__", ignore_errors=True)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("task")
    p.add_argument("--check", action="store_true")
    a = p.parse_args()
    if not a.check:
        sync(a.task)
    bad = problems(a.task)
    if bad:
        print("out of date (run tools/case-kit/sync.py):\n  " + "\n  ".join(bad))
        sys.exit(1)
    print("in sync" if a.check else f"synced the kit into {len(targets(a.task))} casekit/ directories")


if __name__ == "__main__":
    main()
