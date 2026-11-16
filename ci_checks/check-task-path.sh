#!/usr/bin/env bash
#
# Validate a task folder path.
#
# - A case lives at tasks/<case>/ (exactly two segments). <case> may not be one
#   of the reserved domain names below.
# - lite/<task>/ and archive/<task>/ are flat as well; their domain/field are
#   read from task.toml [metadata].
# - The nested layout tasks/<domain>/<field>/<task>/ is accepted only while the
#   base branch's .github/reviewer-pool.yml lists fields under
#   `reviewers_by_field` (single source of truth, read from the *base* branch
#   checkout so a PR cannot add a field to get past this check). <domain> is one
#   of the reserved domain names; if task.toml exists, its [metadata].domain and
#   [metadata].field must match the folder path.
#
# Usage: check-task-path.sh <task-dir>

set -e

TASK_DIR="${1:-}"
if [ -z "$TASK_DIR" ]; then
    echo "Usage: check-task-path.sh <task-dir>"
    exit 2
fi
TASK_DIR="${TASK_DIR%/}"

# In CI under static-checks.yml the repo is checked out to
# $GITHUB_WORKSPACE/base (main) and $GITHUB_WORKSPACE/pr (the PR head); the pool
# is always read from base. Local invocation uses cwd.
POOL_FILE=""
for candidate in \
    "$GITHUB_WORKSPACE/base/.github/reviewer-pool.yml" \
    ".github/reviewer-pool.yml"; do
    if [ -f "$candidate" ]; then
        POOL_FILE="$candidate"
        break
    fi
done

python3 - "$TASK_DIR" "$POOL_FILE" <<'PYEOF'
import os, re, sys

task_dir, pool_file = sys.argv[1], sys.argv[2]

DOMAINS = [
    "life-sciences", "physical-sciences", "earth-sciences",
    "mathematical-sciences", "engineering-sciences",
]


def slug(s):
    # "Life Sciences" / "life sciences" / "life-sciences" all normalize alike.
    return "-".join((s or "").lower().split())


def pool_fields():
    """Fields listed under reviewers_by_field; empty when there is no pool or no fields."""
    if not pool_file or not os.path.isfile(pool_file):
        return set()
    try:
        import yaml
        with open(pool_file) as f:
            data = yaml.safe_load(f) or {}
        return set((data.get("reviewers_by_field") or {}).keys())
    except ImportError:
        text = open(pool_file).read()
        m = re.search(r"^reviewers_by_field:[ \t]*\n((?:[ \t]+.*\n|[ \t]*\n|[ \t]*#.*\n)*)", text, re.MULTILINE)
        return set(re.findall(r"^[ \t]+([a-z0-9-]+)[ \t]*:", m.group(1), re.MULTILINE)) if m else set()


def read_meta(toml_path):
    try:
        import tomllib
        with open(toml_path, "rb") as f:
            return tomllib.load(f).get("metadata") or {}
    except ImportError:
        # Python < 3.11 (local runs): a forgiving regex over the [metadata] table.
        text = open(toml_path, encoding="utf-8").read()
        m = re.search(r"^\[metadata\]\s*$(.*?)(?=^\[|\Z)", text, re.MULTILINE | re.DOTALL)
        meta = {}
        for key in ("domain", "field"):
            mm = re.search(rf'^\s*{key}\s*=\s*"([^"]*)"', m.group(1) if m else "", re.MULTILINE)
            if mm:
                meta[key] = mm.group(1)
        return meta


def fail(*lines):
    for line in lines:
        print(line)
    sys.exit(1)


parts = task_dir.split("/")
fields = pool_fields()
case_hint = f"tasks/{parts[-1]}/"

if parts[0] in ("lite", "archive"):
    # Flat roots: no domain/field in the path; they must be declared in task.toml [metadata].
    if len(parts) != 2:
        fail(f"FAIL {task_dir}: {parts[0]}/ is flat; expected {parts[0]}/<task>/ (got {len(parts)} segments).")
    try:
        meta = read_meta(os.path.join(task_dir, "task.toml"))
    except Exception as exc:  # noqa: BLE001
        fail(f"FAIL {task_dir}: a {parts[0]}/ task needs a readable task.toml with [metadata].domain/field ({exc})")
    domain, field = slug(meta.get("domain")), slug(meta.get("field"))
    if not domain or not field:
        fail(f"FAIL {task_dir}: task.toml [metadata] must declare domain and field (a {parts[0]}/ path carries neither)")
    if domain not in DOMAINS:
        fail(f"FAIL {task_dir}: '{domain}' is not a valid domain.", f"     Valid: {', '.join(DOMAINS)}")
    if fields and field not in fields:
        fail(f"FAIL {task_dir}: '{field}' is not a valid field.", f"     Valid fields: {', '.join(sorted(fields))}")
    sys.exit(0)

if parts[0] != "tasks":
    fail(f"FAIL {task_dir}: not under tasks/, lite/ or archive/")

if len(parts) == 2:
    if parts[1] in DOMAINS:
        fail(f"FAIL {task_dir}: '{parts[1]}' is a reserved name; choose another task folder name.")
    sys.exit(0)                     # a case: tasks/<case>/

if not fields:
    fail(f"FAIL {task_dir}: a case lives directly under tasks/; expected {case_hint} (got {len(parts)} segments).")

# Legacy nested layout: tasks/<domain>/<field>/<task>/
if len(parts) != 4 or parts[1] not in DOMAINS:
    fail(f"FAIL {task_dir}: expected {case_hint} for a case, or tasks/<domain>/<field>/<task>/ "
         f"(got {len(parts)} segments).", f"     Valid domains: {', '.join(DOMAINS)}")
domain, field = parts[1], parts[2]
if field not in fields:
    fail(f"FAIL {task_dir}: '{field}' is not a valid field.", f"     Valid fields: {', '.join(sorted(fields))}")

toml_path = os.path.join(task_dir, "task.toml")
if os.path.isfile(toml_path):
    try:
        meta = read_meta(toml_path)
    except Exception:
        sys.exit(0)  # task.toml parse failures are reported by check-task-fields.sh
    errs = []
    if (meta.get("domain") or "").strip() and slug(meta.get("domain")) != domain:
        errs.append(f"task.toml [metadata].domain = '{meta['domain']}' but folder is under '{domain}'")
    if (meta.get("field") or "").strip() and slug(meta.get("field")) != field:
        errs.append(f"task.toml [metadata].field = '{meta['field']}' but folder is under '{field}'")
    if errs:
        fail(*(f"FAIL {task_dir}: {e}" for e in errs))
sys.exit(0)
PYEOF
