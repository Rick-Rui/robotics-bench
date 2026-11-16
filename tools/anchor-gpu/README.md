# Anchor GPU access from Robotics-Bench

Robotics-Bench owns the workflow, runner registration and environment receipts.
Anchor supplies its existing four RTX 4090s and native simulator installation.
No Anchor PR, RunPod key or new GPU rental is needed.

For CPU-hosted Harbor with asynchronous native Isaac execution on this host,
use the [remote job service](remote/README.md). It runs independently of GitHub
runner availability and does not require Harbor or GPU-enabled Docker on n1.

## Select one GPU

The `ZIWU adapter GPU environment` workflow accepts physical GPU `0`, `1`, `2`
or `3`, routing to `ziwu-gpu-0` through `ziwu-gpu-3`. Maintainers can invoke:

```bash
gh workflow run ziwu-adapter-gpu.yml --repo ZIWU-AI/robotics-bench \
  --ref adapter/embodiedswe-pilot -f gpu=0 -f mode=cuda
```

Use `--ref main` after merge. Modes are `inventory` (read-only host inventory),
`cuda` (one visible device and a checked CUDA computation), and `isaac` (CUDA
plus ten headless SimulationApp updates). These do not run benchmark tasks or
models. The draft branch has a narrow bootstrap push trigger for this workflow
file only; it always runs inventory, never CUDA/Isaac. No PR triggers GPU work.

Each explicit compute probe holds the **same host file lock as Anchor**:
`$ZIWU_GPU_LOCK_DIR/anchor-robodojo-gpu-N.lock`. A busy lane fails immediately.
GitHub concurrency alone cannot coordinate two repositories. The probe masks
CUDA to the selected GPU UUID, checks the runner lane, and reaps its process
group before releasing the lock. It has a five-minute runtime limit inside a
ten-minute workflow limit. Isaac's Vulkan device is explicitly selected using
the physical index; CUDA uses logical device zero after masking.

Only `report.json` is uploaded (30 days). Raw runtime logs remain under ignored
`runs/`; `logs/` is also ignored. Inventory/CUDA success does **not** establish
EmbodiedSWE container, no-op, recorder or parity acceptance. The receipt reports
installed versions and Docker/CDI availability separately.

## Install on an existing Anchor host

Run as the same host user as Anchor so the per-card locks are shared. Set up an
operator-owned `host.env` outside the checkout with exports:

```bash
export ZIWU_GPU_LOCK_DIR="$HOME/.local/state/anchor/gpu-locks"
export ZIWU_SIM_PYTHON="$HOME/anchor-gpu-runtime/python-wrapper/bin/python"
source "$HOME/anchor-gpu-runtime/config/anchor_gpu_env.sh"
# Set HTTPS_PROXY/HTTP_PROXY here only if this host requires its existing proxy.
```

No self-hosted GitHub runners are registered on this host. The host shares one
system user (in the docker group) with Anchor and the GPU-pool service, so a
runner there would let any repository writer run code next to the service token.
GPU trials use GitHub-hosted runners and the restricted transport tunnel.

For direct host validation, source `host.env` and run
`python3 tools/anchor-gpu/probe.py --gpu 0 --mode cuda --output runs/unique-id`.
The output directory must be new so old evidence cannot be mistaken for a new run.

Offline checks: `python -m unittest discover -s tools/anchor-gpu/tests -v`.
