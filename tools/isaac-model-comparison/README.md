# Model harness adapters and CPU agent image

Robotics-Bench does not run GPU tasks on Modal. This directory keeps only the parts
that the Actions CPU / Robotics-Bench GPU pool trial path (`tools/isaac-remote/`) uses:

| File | Used for |
|---|---|
| `harness_common.py` | Harbor job configuration, preflight, execution and result collection for one trial |
| `profiles.py`, `routes.py` | Model/harness routes (Kimi k3.1-pro with Codex or Claude Code) |
| `deepseek_codex.py`, `deepseek_routes.py`, `dsh_agent.py`, `deepseek-model-catalog.json` | DeepSeek routes referenced by `routes.py` |
| `cpu_image.py`, `cpu.Dockerfile` | Check and smoke-test the operator's CPU agent image (`ziwu-pr8-cpu`) |
| `CPU_GPU_COHORT.md` | Status and acceptance record of the Actions CPU / RTX 4090 cohort |
| `evidence/` | Historical evidence files, including earlier Modal runs; they are not results of this cohort |

The earlier Isaac 6.0.1 / Modal A10 runner (`backend=isaac-model`) and the
Isaac reference runner (`tools/isaac-reference-modal/`, `backend=isaac`) were
removed. Their results are historical and are not counted with the RTX 4090 /
Isaac Sim 5.1 cohort.

Run trials with `GPU Task Validation` and `backend=cpu-gpu-task`; see
[`tools/isaac-remote/README.md`](../isaac-remote/README.md).

Tests: `python -m pytest -q tools/isaac-model-comparison` (needs
`requirements-host.txt`).
