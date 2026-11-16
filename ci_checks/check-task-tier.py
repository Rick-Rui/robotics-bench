#!/usr/bin/env python3
"""One task PR is one tier of one task: CPU, GPU or real.

A task can have up to three tiers, each submitted as its own PR, lower tiers first:

    tier:cpu   the task itself: the PR adds <task>/instruction.md (docker)
    tier:gpu   the GPU tier: the PR adds <task>/host/service.json (with gpu/, environment/gpu/, tests/gpu/);
               or a whole task that only runs on a GPU (task.toml validate_env = "ziwu-gpu"; the retired
               "modal" counts too, so a task that still declares it is labelled as what it is)
    tier:real  the real-robot tier: the PR adds <task>/real/task.json

A PR that adds none of these markers is a fix; its tier is the highest tier whose own files it changes
(real/ > host/ gpu/ environment/gpu/ tests/gpu/ > anything else). A higher tier may also change files it
shares with the lower ones (solution/, vendored libraries). The tier is read from GitHub API metadata only:
nothing from the PR is checked out or executed. It decides which slash commands work on the PR (docs/COMMANDS.md).

    check-task-tier.py <pr>            static check: prints PASS / FAIL lines, exit 1 on a problem
    check-task-tier.py <pr> --json     {"tier", "task", "tasks", "problems"}  (exit 0)
    check-task-tier.py <pr> --tier     just the tier ("" when the PR changes no task)
    check-task-tier.py 0 --color gpu   the label colour of a tier (cpu blue, gpu purple, real orange)

Problems: more than one task; two tiers introduced by one PR; a GPU or real tier whose task is not in the
PR's base branch yet (submit the lower tier first; a stacked PR, based on the lower tier's branch, is fine).

A new task that only runs on a GPU (task.toml validate_env = "ziwu-gpu") and so has no CPU tier is a problem:
every task starts as a CPU tier, and its GPU tier comes after the CPU tier is reviewed and merged (docs/LADDER.md).
The GPU-only task PRs that were open when the rule took effect (LEGACY_GPU_ONLY_PRS) get a WARN line instead
until they are migrated.
"""
import argparse
import base64
import json
import os
import subprocess
import sys

TIERS = ("cpu", "gpu", "real")
LABELS = {t: f"tier:{t}" for t in TIERS}
GPU_PREFIXES = ("host/", "gpu/", "environment/gpu/", "tests/gpu/")
REAL_PREFIXES = ("real/",)
GPU_BACKENDS = ("ziwu-gpu", "modal")
COLORS = {"cpu": "1d76db", "gpu": "8250df", "real": "d9730d"}    # blue, purple, orange
MARKERS = {"instruction.md": "cpu", "host/service.json": "gpu", "real/task.json": "real"}
# GPU-only task PRs open on 2026-11-15, when "CPU tier first" became a hard rule; they still need migrating.
LEGACY_GPU_ONLY_PRS = frozenset()


DOMAINS = ("life-sciences", "physical-sciences", "earth-sciences", "mathematical-sciences", "engineering-sciences")


def task_root(path):
    """tasks/<case>/..., lite/<task>/... (or the legacy tasks/<domain>/<field>/<task>/...) -> (task dir, path inside it); else None."""
    parts = path.split("/")
    if parts[0] == "tasks" and parts[1:2] and parts[1] in DOMAINS:     # legacy nested layout
        return ("/".join(parts[:4]), "/".join(parts[4:])) if len(parts) > 4 else None
    if parts[0] == "tasks" and len(parts) > 2:
        return "/".join(parts[:2]), "/".join(parts[2:])
    if parts[0] == "lite" and len(parts) > 2:
        return "/".join(parts[:2]), "/".join(parts[2:])
    return None


def path_tier(rel):
    if rel.startswith(REAL_PREFIXES):
        return "real"
    if rel.startswith(GPU_PREFIXES):
        return "gpu"
    return "cpu"


def classify(files, validate_env=lambda task: "", in_base=lambda task: True, legacy=False):
    """files: [{"filename", "status"}]. Returns {"tier", "task", "tasks", "problems", "warnings"}."""
    touched, added = {}, {}
    for f in files:
        hit = task_root(f.get("filename", ""))
        if not hit or hit[1].rsplit("/", 1)[-1] == ".gitkeep":
            continue
        task, rel = hit
        touched.setdefault(task, set()).add(path_tier(rel))
        if f.get("status") == "added" and rel in MARKERS:
            added.setdefault(task, set()).add(MARKERS[rel])
    tasks = sorted(touched)
    if not tasks:
        return {"tier": "", "task": "", "tasks": [], "problems": [], "warnings": []}
    problems = []
    if len(tasks) > 1:
        problems.append(f"the PR changes {len(tasks)} tasks ({', '.join(t.rsplit('/', 1)[1] for t in tasks)}); "
                        "one PR is one tier of one task")
    task = tasks[0]
    new = set().union(*added.values()) if added else set()
    whole_gpu = validate_env(task) in GPU_BACKENDS  # a task that only runs on a GPU: all of it is the GPU tier
    if whole_gpu:
        new.discard("cpu")
    if len(new) > 1:
        order = [t for t in TIERS if t in new]
        problems.append(f"the PR introduces the {' and '.join(order)} tiers together; submit one tier per PR, "
                        "lower tiers first (cpu: instruction.md; gpu: host/service.json; real: real/task.json)")
    found = new or set().union(*touched.values())
    if whole_gpu and "real" not in found:
        found = {"gpu"}
    tier = [t for t in TIERS if t in found][-1]
    if tier in ("gpu", "real") and "cpu" not in new and not whole_gpu and not in_base(task):
        problems.append(f"the {tier} tier needs the task in the base branch: submit the CPU tier first, "
                        "or base this PR on the lower tier's branch")
    warnings = []
    if whole_gpu and "cpu" in (added.get(task) or ()) and not in_base(task):
        msg = ("this new task runs only on the GPU pool and has no CPU tier: submit the CPU tier first (a docker task, "
               "MuJoCo for a simulation); after it is reviewed and merged, the GPU tier goes in its own PR and its "
               "author can run /scene and /validate on the pool (docs/LADDER.md)")
        (warnings if legacy else problems).append(msg)
    return {"tier": tier, "task": task, "tasks": tasks, "problems": problems, "warnings": warnings}


def gh(path, jq=None):
    cmd = ["gh", "api", path] + (["--paginate"] if "/files" in path else []) + (["--jq", jq] if jq else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def classify_pr(repo, pr):
    out = gh(f"repos/{repo}/pulls/{pr}/files", ".[] | {filename, status}") or ""
    files = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    info = json.loads(gh(f"repos/{repo}/pulls/{pr}") or "{}")
    head, base = info.get("head", {}).get("sha", ""), info.get("base", {}).get("sha", "")

    def task_toml(task, cache={}):
        if task not in cache:
            raw = gh(f"repos/{repo}/contents/{task}/task.toml?ref={head}", ".content")
            try:
                import tomllib
                cache[task] = tomllib.loads(base64.b64decode(raw).decode()) if raw else {}
            except Exception:
                cache[task] = {}
        return cache[task]

    def validate_env(task):
        return task_toml(task).get("environment", {}).get("validate_env") or ""

    def in_base(task):
        return gh(f"repos/{repo}/contents/{task}/instruction.md?ref={base}", ".sha") is not None

    return classify(files, validate_env, in_base, int(pr) in LEGACY_GPU_ONLY_PRS)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pr")
    p.add_argument("--repo", default=os.environ.get("REPO") or os.environ.get("GITHUB_REPOSITORY", ""))
    p.add_argument("--json", action="store_true")
    p.add_argument("--tier", action="store_true")
    p.add_argument("--color", metavar="TIER", help="print the label colour of a tier and exit")
    a = p.parse_args()
    if a.color:
        print(COLORS[a.color])
        return 0
    res = classify_pr(a.repo, a.pr)
    if a.json:
        print(json.dumps(res))
        return 0
    if a.tier:
        print(res["tier"])
        return 0
    if not res["tier"]:
        print("PASS no task content")
        return 0
    for prob in res["problems"]:
        print(f"FAIL {prob}")
    for warn in res.get("warnings", []):
        print(f"WARN {warn}")
    if res["problems"]:
        return 1
    print(f"PASS {LABELS[res['tier']]} of {res['task']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
