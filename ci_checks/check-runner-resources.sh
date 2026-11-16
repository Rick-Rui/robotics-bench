#!/bin/bash
# Fail CPU tasks that request more than a GitHub-hosted runner provides.
#
# CPU tasks (validate_env unset or "docker", no GPU) run their agent and
# verifier containers on GitHub-hosted runners. For this private repository a
# runner has 2 CPUs and 7 GB of memory. Docker refuses a container that asks
# for more CPUs than the host has ("range of CPUs is from 0.01 to 2.00"), so
# such a task never starts: auto-validation, /validate, /run and /cheat all
# fail before the oracle or the agent runs. Memory above the limit is cut off
# by the kernel mid-run. Both container sections are checked:
# [environment] (agent) and [verifier.environment] (separate verifier).
#
# GPU-pool tasks (validate_env = "ziwu-gpu") are skipped: their agent
# container is sized by the platform, not by task.toml.
#
# Usage:
#   ./check-runner-resources.sh                 # Check all tasks
#   ./check-runner-resources.sh tasks/a/b/c     # Check specific tasks

# CUSTOMIZE: what one GitHub-hosted runner can give a single container.
MAX_CPUS=2
MAX_MEMORY_MB=6144

if [ $# -eq 0 ]; then
    TASK_DIRS=$(find tasks lite -name task.toml -exec dirname {} \; 2>/dev/null | sort)
else
    TASK_DIRS="$*"
fi

FAILED=0
for task_dir in $TASK_DIRS; do
    task_dir="${task_dir%/}"
    [ -f "$task_dir/task.toml" ] || continue
    python3 - "$task_dir/task.toml" "$MAX_CPUS" "$MAX_MEMORY_MB" <<'PY' || FAILED=1
import sys, tomllib
path, max_cpus, max_mem = sys.argv[1], float(sys.argv[2]), int(sys.argv[3])
try:
    cfg = tomllib.load(open(path, "rb"))
except Exception as exc:
    print(f"FAIL {path}: cannot parse task.toml ({exc})")
    sys.exit(1)
env = cfg.get("environment", {}) or {}
if (env.get("validate_env") or "docker") != "docker" or int(env.get("gpus") or 0) >= 1:
    sys.exit(0)
bad = False
for label, section in (("[environment]", env),
                       ("[verifier.environment]", (cfg.get("verifier", {}) or {}).get("environment", {}) or {})):
    cpus, mem = section.get("cpus"), section.get("memory_mb")
    if cpus is not None and float(cpus) > max_cpus:
        print(f"FAIL {path}: {label} cpus = {cpus}; GitHub-hosted runners have {int(max_cpus)} CPUs, "
              f"so Docker refuses the container. Set cpus <= {int(max_cpus)}.")
        bad = True
    if mem is not None and int(mem) > max_mem:
        print(f"FAIL {path}: {label} memory_mb = {mem}; runners have 7 GB in total. "
              f"Set memory_mb <= {max_mem}.")
        bad = True
sys.exit(1 if bad else 0)
PY
done

if [ "$FAILED" -eq 0 ]; then
    echo "All CPU tasks fit a GitHub-hosted runner (<= $MAX_CPUS CPUs, <= $MAX_MEMORY_MB MB)"
fi
exit $FAILED
