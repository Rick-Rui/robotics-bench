# Actions CPU / remote bimanual task

`run.py` executes one pinned Harbor 0.23.0 trial. The agent runs as `agent` in
Actions Docker (2 CPU, 6144 MiB). The existing numeric task client reaches a
session-scoped relay; transport administration and model credentials remain on
the CPU side. The remote supervisor holds the shared Anchor GPU lease through
online execution, sealed collection and fresh independent replay.

The registered host runtime derives from the committed task and checks every
source and asset hash. Its explicit adaptations are Isaac 5.1 identity, private
asset paths, the host Python/glibc wrapper, physical Vulkan versus logical CUDA
device selection, and supervisor-owned collection. Physics, action limits,
camera observations and scoring remain the frozen task's implementation.
Replay inherits a kernel filter denying network sockets; AF_UNIX driver IPC is
available. No solver code executes on the GPU host.

The task relay transfers native JSON with gzip and reuses its authenticated HTTP
connection between closely spaced calls. Decompression is bounded to 64 MiB;
the task client receives the original bytes, including every full PNG image.
Long model pauses open a fresh connection. An ambiguous transport failure never
retries a mutation automatically. `remote-evidence/transport.jsonl` records native
processing time, total latency, wire/decoded sizes and the acknowledged step;
Actions progress logs include the latest measurement.

Use `GPU Task Validation` with `backend=cpu-gpu-task`, `task_path=tasks/...`, `gpu=1|2`,
`harness=oracle|nop|codex|claude-code`, `model_stage=all|preflight` and `trials=1..5`.
Each trial has its own Actions job. Jobs in one request are sequential; submit
one request per lane. Reruns do not create additional attempts. The model path
requires committed `evidence/<task-slug>.json` for this exact task digest and both
GPU lanes. Native harness preflight does not allocate a GPU or count as a trial.

The task commits its own agent and verifier containers (`environment/Dockerfile`,
`tests/Dockerfile` and the files they copy), and its instruction states the
Isaac 5.1 / 5400-second settings. `build.py` copies these files unchanged and
writes only the Harbor run configuration. Any change under the task directory
changes the task digest, so the task must be redeployed (a maintainer's
`/validate` does it) before further trials. The documented `start-pen-render` command
checks the supervisor's existing episode through a read-only health request;
it cannot allocate or reset an episode. Its endpoint configuration and code
are root-owned, and sudo permits only that command without arguments.

Each Actions job publishes a task/result table in its job summary, including
measured solver and replay durations, binary reward, diagnostic native score,
recorded action count, token usage when available, and cleanup status. Missing
results remain N/A. The full JSON evidence is retained in that job's artifact.

Status: implementation under live acceptance. Do not infer task acceptance from
the historical transport fixture. Formal trials remain gated until fresh Oracle
and Nop controls, independent replay and cleanup pass.

Validation: `python -m unittest discover -s tools/isaac-remote -p 'test_*.py' -v`.

The Actions task tunnel uses the dedicated loopback forward on the alternate
operator relay (the `user@host` in the repository variable `GPU_RELAY`, port 22; local destination `127.0.0.1:18771`).
Its pinned host key is `PR8_GPU_FAST_TUNNEL_HOST_KEY`; the task credential can
only forward to that destination. The GPU host initiates a separate restricted
reverse tunnel, so public task/admin HTTP ports are never opened. Both new SSH
authorizations expire after 24 hours and must be renewed for later cohorts.

## Host deployments

`install.py --deployments "$ZIWU_TASK_DEPLOYMENTS"` installs a task revision
into `<deployments>/<task digest>/`, and the host service looks a session's
deployment up by the digest in the request. A new task revision is therefore
installed next to the previous one without restarting the service, and
sessions already running keep their own files. The older single-deployment
variable `ZIWU_BIMANUAL_DEPLOYMENT` is still honoured.

`/validate` installs automatically (`deploy_task.py` -> host `POST
/deployments` -> `install.py --bundle`). The host checks the bundle's digest,
takes the runtime archive from `ZIWU_RUNTIME_CACHE/<sha256>.tar.gz` or downloads
it once from the task's pinned `install.runtime_archive_url` (through
`ZIWU_HF_ENDPOINT`, a Hub mirror, when set), verifies `archive_sha256` and runs
the installer; no task code runs during installation. The host enables this
only when `ZIWU_TASK_DEPLOYMENTS`, `ZIWU_ENGINE_ROOT` and `ZIWU_RUNTIME_CACHE`
are set. `gpu-task-controls.yml` then runs Oracle and Nop on both lanes,
`validate_report.py` applies `accept.py`, and `land_evidence.sh` opens the
acceptance-record PR (`test_deploy.py`).

## Task host contract (`host/service.json`)

Each GPU task declares its trusted host phases in `host/service.json`
(`ziwu-gpu-service-v1`, implemented in `service.py`): `serve` starts the
session's simulation service and creates its ready file, `collect` seals the
recording, and `verify` replays it. The worker runs `verify` under the network
guard. The optional `install` section names the runtime manifest, the
registration fields and any declared runtime-archive rewrites for `install.py`.
Deployments installed before the contract have no `host/service.json` and run
with `service.LEGACY_SERVICE`, which reproduces the original bimanual worker and
installer exactly (`test_service.py`, `test_install.py`).
