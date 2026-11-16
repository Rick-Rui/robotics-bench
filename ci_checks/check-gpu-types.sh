#!/bin/bash
# Validate task.toml GPU declarations:
#
#   1. gpu_types values name hardware the Robotics-Bench GPU pool provides. The
#      pool is the RTX 4090 host of the ziwu-gpu-v0 environment; Modal GPU
#      types are no longer accepted.
#   2. A task requesting gpus >= 1 sets validate_env = "ziwu-gpu". The GitHub
#      runner backing env=docker has no GPU, so a GPU task left on the default
#      backend would silently run GPU-less, and Robotics-Bench no longer validates
#      on Modal.

set -e

# CUSTOMIZE: GPU types the Robotics-Bench GPU pool provides (ziwu-gpu-v0).
ALLOWED_GPU_TYPES=(
    "RTX-4090"
)

# CUSTOMIZE: the only backend that can run a GPU task.
GPU_BACKEND="ziwu-gpu"

if [ $# -eq 0 ]; then
    FILES_TO_CHECK=$(find tasks -type f -name "task.toml")
else
    FILES_TO_CHECK=""
    for task_dir in "$@"; do
        if [ -d "$task_dir" ] && [ -f "$task_dir/task.toml" ]; then
            FILES_TO_CHECK="$FILES_TO_CHECK $task_dir/task.toml"
        fi
    done
fi

if [ -z "$FILES_TO_CHECK" ]; then
    echo "No task.toml files to check"
    exit 0
fi

FAILED=0
for file in $FILES_TO_CHECK; do
    if [ ! -f "$file" ]; then
        continue
    fi

    # A GPU task must run on the Robotics-Bench GPU pool. env=docker runs on the
    # GitHub runner, which has no GPU, so gpus >= 1 there is silently wrong: the
    # trial runs GPU-less instead of failing. Modal and other hosted backends
    # are not part of the ziwu-gpu-v0 environment.
    gpus=$(grep -E '^[[:space:]]*gpus[[:space:]]*=' "$file" | head -1 | grep -Eo '[0-9]+' | head -1 || true)
    if [ -n "$gpus" ] && [ "$gpus" -ge 1 ] 2>/dev/null; then
        venv=$(grep -E '^[[:space:]]*validate_env[[:space:]]*=' "$file" | head -1 \
            | sed 's/.*=[[:space:]]*//' | tr -d '"'"'"' ' || true)
        if [ "$venv" != "$GPU_BACKEND" ]; then
            echo "FAIL $file: gpus = $gpus but validate_env is ${venv:-unset (defaults to docker)}"
            echo "       GPU tasks run on the Robotics-Bench GPU pool. Set validate_env = \"$GPU_BACKEND\"."
            FAILED=1
        fi
    fi

    # Extract the gpu_types array value. Only lines like:
    #   gpu_types = ["H100", "A100-80GB"]
    # We don't support multi-line arrays (Harbor task.toml convention is single-line).
    line=$(grep -E '^[[:space:]]*gpu_types[[:space:]]*=' "$file" || true)
    if [ -z "$line" ]; then
        continue
    fi

    # Extract the part inside [...], strip quotes and whitespace, split on commas.
    inner=$(echo "$line" | sed -n 's/.*\[\(.*\)\].*/\1/p')
    if [ -z "$inner" ]; then
        # Empty array is allowed (equivalent to "any").
        continue
    fi

    # Split on commas, strip quotes/whitespace per value
    IFS=',' read -r -a values <<< "$inner"
    for raw in "${values[@]}"; do
        value=$(echo "$raw" | tr -d '"' | tr -d "'" | xargs)
        if [ -z "$value" ]; then
            continue
        fi
        allowed=0
        for ok in "${ALLOWED_GPU_TYPES[@]}"; do
            if [ "$value" = "$ok" ]; then
                allowed=1
                break
            fi
        done
        if [ "$allowed" -eq 0 ]; then
            echo "FAIL $file: gpu_types contains unsupported value \"$value\""
            echo "       Allowed: ${ALLOWED_GPU_TYPES[*]}"
            FAILED=1
        fi
    done
done

if [ "$FAILED" -eq 1 ]; then
    echo ""
    echo "gpu_types must name hardware in the Robotics-Bench GPU pool (${ALLOWED_GPU_TYPES[*]}),"
    echo "and any task with gpus >= 1 must set validate_env = \"$GPU_BACKEND\"."
    exit 1
fi

echo "All task.toml GPU declarations are valid"
