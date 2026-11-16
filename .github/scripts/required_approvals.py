#!/usr/bin/env python3
"""Merge gate: task PRs must be approved by their human reviewers.

Posts the commit status `merge-gate/required-approvals` on a PR's head commit.
A ruleset on the default branch requires this status, so the gate decides
whether a PR can merge.

Who must approve
  * A PR listed in .github/required-approvers.json: exactly the logins listed.
  * Otherwise, a task PR (it changes a file under a configured task path,
    by default tasks/, lite/ and adapters/): the reviewers that
    the review automation assigned in the `reviewer-slots` marker. The domain
    and technical slots must be filled; the final slot counts once assigned.
    Only markers written by github-actions[bot] are trusted.
  * Any other PR: nobody; the gate passes.

What counts as approval
  The reviewer's latest decisive review (APPROVED / CHANGES_REQUESTED /
  DISMISSED) is APPROVED and, unless the config sets
  "approval_must_match_head": false, was submitted on the current head commit,
  so commits pushed after an approval need a fresh approval. The PR author
  never counts. The reviews API includes reviewers with read access, whose
  approvals GitHub's native required-approvals rule ignores.

Usage: required_approvals.py (--pr N | --all-open) [--dry-run]
"""
import argparse
import json
import os
import re
import subprocess

CONTEXT = "merge-gate/required-approvals"
REPO = os.environ["GITHUB_REPOSITORY"]
DECISIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
MARKER_RE = re.compile(r"<!--\s*reviewer-slots:\s*(\{.*?\})\s*-->", re.DOTALL)
BOT = "github-actions[bot]"


def gh(*args):
    return subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True).stdout


def gh_lines(path, jq):
    return [json.loads(x) for x in gh("--paginate", path, "--jq", jq).splitlines() if x.strip()]


def load_config(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    listed = {int(n): [u.lower() for u in e["reviewers"]] for n, e in raw.get("pull_requests", {}).items()}
    return listed, raw.get("approval_must_match_head", True), tuple(raw.get("task_paths", ["tasks/", "lite/", "adapters/"]))


def slot_reviewers(number):
    """Merged slots from bot-written markers, newest non-empty value per slot."""
    slots = {"domain": "", "technical": "", "final": ""}
    comments = gh_lines(f"repos/{REPO}/issues/{number}/comments", ".[] | {a: .user.login, b: .body}")
    for c in comments:  # oldest first
        if c["a"] != BOT:
            continue
        m = MARKER_RE.search(c["b"] or "")
        if not m:
            continue
        try:
            raw = json.loads(m.group(1))
        except ValueError:
            continue
        raw.setdefault("technical", raw.get("general", ""))
        for k in slots:
            if raw.get(k):
                slots[k] = str(raw[k]).lstrip("@").lower()
    return slots


def evaluate(number, listed, must_match_head, task_paths):
    pr = json.loads(gh(f"repos/{REPO}/pulls/{number}"))
    sha, author = pr["head"]["sha"], pr["user"]["login"].lower()
    if number in listed:
        required, why = listed[number], "named approvers"
    else:
        files = gh_lines(f"repos/{REPO}/pulls/{number}/files?per_page=100", ".[] | {f: .filename}")
        if not any(x["f"].startswith(task_paths) for x in files):
            return sha, "success", "Not a task PR; no human approval required."
        slots = slot_reviewers(number)
        missing = [s for s in ("domain", "technical") if not slots[s]]
        if missing:
            return sha, "pending", f"Task PR: waiting for {' and '.join(missing)} reviewer assignment."
        required = [slots[s] for s in ("domain", "technical", "final") if slots[s]]
        why = "task reviewers"
    required = [u for u in dict.fromkeys(required) if u != author]
    latest = {}
    for r in gh_lines(f"repos/{REPO}/pulls/{number}/reviews",
                      ".[] | {u: (.user.login | ascii_downcase), s: .state, c: .commit_id}"):
        if r["s"] in DECISIVE:
            latest[r["u"]] = r  # API order is chronological
    approved, stale, changes, waiting = [], [], [], []
    for u in required:
        r = latest.get(u)
        if r and r["s"] == "CHANGES_REQUESTED":
            changes.append(u)
        elif r and r["s"] == "APPROVED" and (r["c"] == sha or not must_match_head):
            approved.append(u)
        elif r and r["s"] == "APPROVED":
            stale.append(u)
        else:
            waiting.append(u)
    at = lambda us: ", ".join("@" + u for u in us)
    if changes:
        return sha, "failure", f"Changes requested by {at(changes)}"[:140]
    if waiting or stale:
        text = f"{len(approved)}/{len(required)} {why} approved"
        if waiting:
            text += f"; waiting for {at(waiting)}"
        if stale:
            text += f"; re-approval needed after new commits: {at(stale)}"
        return sha, "pending", text[:140]
    return sha, "success", f"Approved by {at(required)}"[:140]


def post(number, sha, state, description, dry_run):
    print(f"PR #{number} {sha[:8]} -> {state}: {description}")
    if dry_run:
        return
    run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{REPO}"
               f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")
    gh("-X", "POST", f"repos/{REPO}/statuses/{sha}", "-f", f"state={state}", "-f", f"context={CONTEXT}",
       "-f", f"description={description}", "-f", f"target_url={run_url}")


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pr", type=int)
    group.add_argument("--all-open", action="store_true")
    ap.add_argument("--config", default=".github/required-approvers.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    listed, must_match_head, task_paths = load_config(args.config)
    numbers = [args.pr] if args.pr else [
        int(n) for n in gh("--paginate", f"repos/{REPO}/pulls?state=open&per_page=100", "--jq", ".[].number").split()]
    failed = 0
    for number in numbers:
        try:
            post(number, *evaluate(number, listed, must_match_head, task_paths), args.dry_run)
        except subprocess.CalledProcessError as exc:
            failed += 1
            print(f"PR #{number}: {(exc.stderr or '').strip()[:300]}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
