#!/usr/bin/env bash
# Open the acceptance-record PR from a /validate run (maintainer, local checkout).
#
# Usage: tools/isaac-remote/land_evidence.sh <run id> <task slug> [repo]
#
# /validate uploads the record as the `gpu-acceptance-<slug>` artifact. A PR
# opened by the workflow's own token would not start CI, so a maintainer opens
# it. The PR changes only tools/isaac-remote/evidence/<slug>.json.
set -euo pipefail
RUN="$1"; SLUG="$2"; REPO="${3:-ZIWU-AI/robotics-bench}"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
gh run download "$RUN" -R "$REPO" -n "gpu-acceptance-$SLUG" -D "$TMP"
RECORD="$TMP/$SLUG.json"
python3 - "$RECORD" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
assert r['status'] == 'passed' and r['gpus'] == [1, 2], 'not a passing acceptance record'
print('task digest', r['task_digest'])
PY
DIGEST=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['task_digest'][:8])" "$RECORD")
BRANCH="evidence/$SLUG-$DIGEST"
git fetch -q origin main
git switch -c "$BRANCH" origin/main
command cp -f "$RECORD" "tools/isaac-remote/evidence/$SLUG.json"
git add "tools/isaac-remote/evidence/$SLUG.json"
git commit -q -m "Record Oracle/Nop acceptance for $SLUG (task digest $DIGEST)" \
  -m "From /validate run https://github.com/$REPO/actions/runs/$RUN."
git push -q -u origin "$BRANCH"
gh pr create -R "$REPO" --base main --head "$BRANCH" \
  --title "Record Oracle/Nop acceptance for $SLUG ($DIGEST)" \
  --body "Acceptance record produced by /validate run https://github.com/$REPO/actions/runs/$RUN (Oracle 1 and Nop 0 on GPU 1 and GPU 2). Only \`tools/isaac-remote/evidence/$SLUG.json\` changes."
