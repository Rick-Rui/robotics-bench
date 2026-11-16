# Robotics-Bench

Robotics-Bench evaluates AI agents on robotics tasks. Each task (a *case*) is a [Harbor](https://github.com/harbor-framework/harbor) task directory written entirely by its contributor: the world, the robot, the engine, the agent's API, the instruction, the success check and a reference solution. The platform connects agents to cases and runs them: Docker containers for the CPU tier, a GPU pool for the GPU tier. Every case is scored 0 or 1 by an independent verifier.

`CLAUDE.md` is a symlink to this file. Edit `AGENTS.md`.

## Repository layout

```
.
├── README.md                   # Overview
├── GUIDANCE.md                 # What the bench is, what a case is, tiers, submission
├── CONTRIBUTING.md             # Submission process for task authors
├── REVIEWING.md                # How reviewers review
├── TASK_REVIEW_AUTOMATION.md   # Every CI check, workflow and slash command
├── task-template.toml          # Starting point for a task.toml
├── task-readme-template.md     # Authored sections of a task README
├── task-hint-template.md       # Template for a task's optional authoring/hint.md
├── tasks/<task>/                # Cases
├── docs/                       # Rules and guides (see below)
├── rubrics/                    # Criteria for the LLM reviewers
├── ci_checks/                  # Static checks (check-*.sh, check-*.py) and test-tasks/
├── scripts/                    # Generators for derived files
├── tools/                      # Platform code (see below)
└── .github/
    ├── workflows/              # CI; each one is described in TASK_REVIEW_AUTOMATION.md
    ├── scripts/                # Helpers the workflows call
    ├── llm-config.yml          # Agents, judges and gateways
    ├── reviewer-pool.yml       # Reviewer pools for the domain, technical and final slots
    ├── required-approvers.json # Merge gate configuration
    ├── pull_request_template.md
    └── DISCUSSION_TEMPLATE/task-proposals.yml   # Proposal form
```

## Key documents

| Document | What it covers |
| --- | --- |
| [GUIDANCE.md](GUIDANCE.md) | The bench, the case, the tiers and the submission path in one place |
| [docs/CASE.md](docs/CASE.md) | What a case contains and the contract every online-control case meets |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Proposal, PR, checks, review, merge, GPU access |
| [docs/TASK_TYPES.md](docs/TASK_TYPES.md) | The task types and what each one submits |
| [docs/LADDER.md](docs/LADDER.md) | The tiers, what each step checks, and how the GPU pool is granted |
| [docs/TASK_FORMAT.md](docs/TASK_FORMAT.md) | The hard rules CI enforces, each with its check name |
| [docs/EXAMPLES.md](docs/EXAMPLES.md) | Reference examples |
| [docs/ASSETS.md](docs/ASSETS.md), [docs/LARGE-DATA.md](docs/LARGE-DATA.md) | Asset sources and licences; large files |
| [docs/COMMANDS.md](docs/COMMANDS.md) | Slash commands on a PR of each tier |
| [docs/CPU_TASK_GUIDE.md](docs/CPU_TASK_GUIDE.md), [docs/GPU_TASK_GUIDE.md](docs/GPU_TASK_GUIDE.md), [docs/SCENE_CHECK.md](docs/SCENE_CHECK.md) | Walk-throughs of the CPU tier, the GPU tier and the GPU scene check |
| [REVIEWING.md](REVIEWING.md) | Who signs off, review order, what to check before merge |
| [TASK_REVIEW_AUTOMATION.md](TASK_REVIEW_AUTOMATION.md) | Reference for every check and workflow |
| [docs/CI-INTERNALS.md](docs/CI-INTERNALS.md) | Configuration files, CI invariants, files that change together; read before changing CI |

## The rules in brief

- **A case is fully authored by its contributor.** Robot, engine, scene, action and observation spaces, and the success check are the author's choice. The bench fixes only the contract in [docs/CASE.md](docs/CASE.md): isolation, the call envelope, the hash-chained recording, replay-based verification, budgets, and calibration (reference solution 1, no-op 0).
- **One PR is one tier of one task.** Tiers are CPU (`tier:cpu`, Docker) and GPU (`tier:gpu`, the GPU pool). CI labels the tier from the changed paths.
- **CPU tier first.** Every new task starts as a CPU tier. A task that only runs on a GPU fails the `Task tier` check. Once an author's CPU tier is merged, the GPU pool is granted to that author automatically for the GPU tier of the same task.
- **A new task starts with a proposal.** The author posts it in Discussions (Task Proposals) and a reviewer approves it with `/approve`; a new-task PR links the approved proposal, and `check-task-proposal-link` fails without it.
- **No difficulty threshold.** A task that passes the checks and review can merge. Model trials are recorded; they are not a merge condition.
- **Two sign-offs**: design review (`domain` slot) and implementation review (`technical` slot). Final review is optional.
- **Every verifier is independent**: separate environment, no network, reads only declared artifacts, writes 0 or 1.

## Reviewing a task PR

If you are an agent asked to review a task PR:

1. **Find what actually ran.** Read the verification card and the check comments on the PR. A check with no result on the current head commit has not passed.
2. **Read in this order**: `instruction.md` → `tests/` → `solution/` → `environment/` → `task.toml` and `README.md`. For a GPU tier also read `host/service.json` and the host-side code under `environment/` and `tests/`.
3. **Ask the questions that matter**:
   - Does the verifier check exactly what the instruction states, and nothing the instruction does not say?
   - Does the reference solution do the task through the interface the agent has, within the same budget, closed loop?
   - Can the agent reach a passing state without doing the task: reading solution or test files, reaching the simulator's state, forging the recording, exploiting a weak success condition?
   - Does the verifier replay the recording rather than trust what the service reported?
   - Is everything pinned (images, dependencies, data, runtime archives)?
   - Are asset sources and licences recorded, and are all contributors credited?
4. **Report bugs before style.** A wrong assertion, a leaked file or an unrunnable solution matters more than a rubric detail. Name the file and line, what to change and why.
5. **Do not run a GPU command on a PR without reading its host-side code first.** The GPU host has no sandbox; task-supplied serve, collect and verify code runs on it directly.

[rubrics/task-implementation.toml](rubrics/task-implementation.toml) is the checklist the automated `/review` uses; [REVIEWING.md](REVIEWING.md) has the per-step focus.

## Platform code and task code

| Path | Owner | Notes |
| --- | --- | --- |
| `tasks/` | Task authors | Changed only through task PRs |
| `tools/case-kit/` | Platform | The case contract in code: recording, call envelope, launcher and collector helpers, verifier helpers, `gate.py` (the CPU gate), `sync.py`, and a complete example case |
| `tools/isaac-remote/` | Platform | GPU tier: preflight, deploy, acceptance, scene check, trials, GPU grant |
| `tools/isaac-model-comparison/` | Platform | Agent image, model routes and host requirements used by the GPU tier workflows |
| `tools/qoder-harbor/` | Platform | Harbor adapter for the Qoder CLI agent |
| `tools/task-readme/` | Platform | Renders the managed header of a task README from `task.toml` |
| `tools/verification-card/` | Platform | The per-PR comment stating what ran on the head commit |
| `tools/rubric-tuning/`, `tools/batch-grader/` | Platform | Evaluate and tune the LLM reviewer |
| `tools/fortify/` | Platform | `/fortify` adversarial hardening run |
| `ci_checks/`, `.github/`, `scripts/` | Platform | CI |

Do not copy another task's service code into a new task; vendor the kit with `tools/case-kit/sync.py` and write the rest yourself.

## Working on this repository

- **`main` is protected.** Every change goes through a PR. A task PR needs its assigned reviewers to approve the current head commit (`merge-gate/required-approvals`); other PRs pass the gate automatically.
- **Task changes go through a task PR, never a direct edit.** `/run` and `/validate` overlay the PR's copy of the task onto `main`'s CI code, so a task edited only on `main` is never validated.
- **Workflow changes take effect from `main`.** Workflows triggered by `pull_request_target`, `issue_comment` and `workflow_run` run the default branch's copy, so a workflow change cannot be exercised from its own PR. Test the scripts it calls locally and say in the PR what has not run.
- **Do not execute code from a PR branch next to credentials.** CI treats PR task content as data; keep it that way.
- **Keep generated files current.** After changing a `task.toml`, run `python3 tools/task-readme/generate.py --force <task-dir>`. After changing `tools/case-kit/casekit/`, run `python3 tools/case-kit/sync.py <task-dir>` for every task that vendors it.
- **Adding or changing a check** means updating the script in `ci_checks/`, the `CHECKS` list in `.github/workflows/static-checks.yml`, its `#### check-*` section in `TASK_REVIEW_AUTOMATION.md` (CI comments link to these anchors), a `ci_checks/test-tasks/fail-static-*` task, and `docs/TASK_FORMAT.md`. [docs/CI-INTERNALS.md](docs/CI-INTERNALS.md) lists the other files that change together.
- **Rubric criteria names are machine-checked.** Run `python3 ci_checks/check-rubric-consistency.py` after touching `rubrics/` or `ci_checks/test-tasks/labels.json`.

### Running checks locally

```bash
T=tasks/<task>
for s in ci_checks/check-*.sh; do bash "$s" "$T" || echo "FAILED: $s"; done
python3 tools/task-readme/generate.py --force "$T"
harbor run -p "$T" --agent oracle --env docker
harbor run -p "$T" --agent nop --env docker
python3 tools/case-kit/gate.py "$T"            # online-control cases built with the case kit
python3 -m unittest discover -s tools/case-kit/tests
python3 -m unittest discover -s tools/isaac-remote -p 'test_*.py'
```

### Test tasks

`ci_checks/test-tasks/` holds intentionally broken tasks for CI regression testing: `fail-static-*` each fail one static check, `fail-validate-*` one `/validate` runtime check, `fail-rubric-*` one criterion of `rubrics/task-implementation.toml`, and `fail-analysis-*` one `harbor analyze` check. `labels.json` records which criterion each rubric task plants.

```bash
bash ci_checks/check-canary.sh ci_checks/test-tasks/fail-static-canary   # should fail
```

### Before merging a change to CI or docs

If the PR adds, removes or renames a check, workflow, rubric or guide, update every document that refers to it: `README.md`, `GUIDANCE.md`, `CONTRIBUTING.md`, `TASK_REVIEW_AUTOMATION.md`, `REVIEWING.md`, `docs/`, and this file.
