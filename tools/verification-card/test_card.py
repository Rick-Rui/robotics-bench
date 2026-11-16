"""State and rendering logic of the verification card, tested on fixed API data without network.

    python -m unittest discover -s tools/verification-card -p 'test_*.py' -v
"""
import unittest

import card

REPO = "ZIWU-AI/robotics-bench"
TASK = "tasks/engineering-sciences/mechanical-engineering/demo-task"
SHA = "a" * 40


def run(name, conclusion="success", status="completed", id=1):
    return {"id": id, "name": name, "status": status, "conclusion": conclusion,
            "html_url": f"https://example.test/run/{id}"}


def pr(labels=("new task", "tier:cpu"), body="", draft=False):
    return {"number": 7, "draft": draft, "body": body, "head": {"sha": SHA},
            "labels": [{"name": n} for n in labels], "base": {"repo": {"default_branch": "main"}}}


def rows(text):
    """Card table -> {item: (state, detail)}"""
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if line.startswith("| ") and len(cells) == 3 and cells[0] not in ("Item", "---"):
            out[cells[0]] = (cells[1], cells[2])
    return out


class TaskDirs(unittest.TestCase):
    def test_nested_and_flat_roots(self):
        self.assertEqual(card.task_dirs([f"{TASK}/task.toml", "lite/x/tests/test.sh", "README.md",
                                         "tasks/a/b/c", "tasks/life-sciences/x", ".github/workflows/x.yml"]),
                         ["lite/x", "tasks/a", TASK])

    def test_no_task_no_card(self):
        self.assertIsNone(card.build(REPO, pr(), ["docs/COMMANDS.md"], [], []))


class RunStates(unittest.TestCase):
    def test_states(self):
        self.assertEqual(card.run_state(run("x")), (card.PASS, ""))
        self.assertEqual(card.run_state(run("x", "failure")), (card.FAIL, ""))
        self.assertEqual(card.run_state(run("x", "cancelled")), (card.FAIL, "cancelled"))
        self.assertEqual(card.run_state(run("x", None, status="in_progress")), (card.RUNNING, ""))
        self.assertEqual(card.run_state(run("x", None, status="queued")), (card.RUNNING, ""))

    def test_skipped_is_not_passed(self):
        self.assertEqual(card.run_state(run("x", "skipped")), (card.NOT_RUN, "skipped"))

    def test_newest_run_wins(self):
        runs = [run("static-checks", "failure", id=5), run("static-checks", "success", id=9),
                run("static-checks", "failure", id=2)]
        self.assertEqual(card.check_row("Static checks", runs, {"static-checks"}, "none")[1], card.PASS)
        runs.append(run("static-checks", "failure", id=11))
        self.assertEqual(card.check_row("Static checks", runs, {"static-checks"}, "none")[1], card.FAIL)

    def test_missing_run_is_not_run(self):
        label, state, text = card.check_row("Static checks", [run("label")], {"static-checks"}, "no record")
        self.assertEqual((state, text), (card.NOT_RUN, "no record"))


class Execution(unittest.TestCase):
    def test_per_task_run_beats_the_skipped_job(self):
        runs = [run("execution-checks", "skipped", id=30), run(f"execution-checks ({TASK})", "success", id=10)]
        self.assertEqual(card.execution_row(runs, TASK)[1], card.PASS)

    def test_only_skipped_job_is_not_run(self):
        _, state, text = card.execution_row([run("execution-checks", "skipped")], TASK)
        self.assertEqual(state, card.NOT_RUN)
        self.assertIn("skipped", text)

    def test_other_tasks_run_does_not_count(self):
        runs = [run("execution-checks (lite/other)", "success")]
        self.assertEqual(card.execution_row(runs, TASK)[1], card.NOT_RUN)

    def test_newest_per_task_run_wins(self):
        runs = [run(f"execution-checks ({TASK})", "success", id=4), run(f"execution-checks ({TASK})", "failure", id=8)]
        self.assertEqual(card.execution_row(runs, TASK)[1], card.FAIL)


class Gate(unittest.TestCase):
    def test_states(self):
        def state(s):
            return card.gate_row([{"context": card.GATE, "state": s, "description": "d"}])[1]
        self.assertEqual([state(s) for s in ("success", "pending", "failure", "error")],
                         [card.PASS, card.RUNNING, card.FAIL, card.FAIL])
        self.assertEqual(card.gate_row([{"context": "other", "state": "success"}])[1], card.NOT_RUN)

    def test_description_is_shown(self):
        text = card.gate_row([{"context": card.GATE, "state": "pending", "description": "waiting for @a",
                               "target_url": "https://example.test/g"}])[2]
        self.assertEqual(text, "[`waiting for @a`](https://example.test/g)")

    def test_reviewer_names_are_not_live_mentions(self):
        text = card.gate_row([{"context": card.GATE, "state": "pending", "description": "waiting for @a, `@b`"}])[2]
        self.assertEqual(text, "`waiting for @a, '@b'`")


class Acceptance(unittest.TestCase):
    record = {"status": "passed", "task_digest": "c3313631" + "0" * 56, "task_tree": "746b8664" + "1" * 32}

    def test_no_record(self):
        self.assertEqual(card.acceptance_row("demo-task", None, "abc")[1], card.NOT_RUN)

    def test_same_tree_passes(self):
        _, state, text = card.acceptance_row("demo-task", self.record, self.record["task_tree"])
        self.assertEqual(state, card.PASS)
        self.assertIn("c3313631", text)

    def test_changed_tree_is_unverified_not_passed(self):
        _, state, text = card.acceptance_row("demo-task", self.record, "f" * 40)
        self.assertEqual(state, card.UNVERIFIED)
        self.assertIn("a record exists (digest not verified)", text)

    def test_unknown_head_tree_is_unverified(self):
        self.assertEqual(card.acceptance_row("demo-task", self.record, None)[1], card.UNVERIFIED)

    def test_record_that_did_not_pass(self):
        self.assertEqual(card.acceptance_row("demo-task", dict(self.record, status="failed"), "x")[1], card.FAIL)


class Proposal(unittest.TestCase):
    link = f"https://github.com/{REPO}/discussions/31"
    approved = {"category": "Task Proposals", "labels": ["proposal-approved ✅"]}

    def row(self, body, info=None, labels=("new task",)):
        return card.proposal_row(list(labels), body, REPO, lambda n: info)

    def test_missing(self):
        self.assertEqual(self.row("No Proposal submitted.")[1], "missing")

    def test_link_inside_html_comment_is_ignored(self):
        self.assertEqual(self.row(f"<!-- {self.link} -->")[1], "missing")

    def test_other_repo_or_bare_number_is_ignored(self):
        self.assertEqual(self.row("see #31 and https://github.com/other/repo/discussions/31")[1], "missing")

    def test_approved(self):
        self.assertEqual(self.row(self.link, self.approved)[1], "approved")

    def test_linked_but_not_approved(self):
        self.assertEqual(self.row(self.link, {"category": "Task Proposals", "labels": []})[1], "not approved")

    def test_wrong_category_or_unreadable(self):
        self.assertEqual(self.row(self.link, {"category": "General", "labels": ["proposal-approved ✅"]})[1], "linked")
        self.assertEqual(self.row(self.link, None)[1], "linked")

    def test_task_fix_needs_no_proposal(self):
        self.assertEqual(self.row("Fixes #3", labels=("task fix",))[1], "n/a")


class Render(unittest.TestCase):
    def test_cpu_card(self):
        runs = [run(f"execution-checks ({TASK})", "success", id=3)]
        statuses = [{"context": card.GATE, "state": "pending", "description": "Task PR: waiting"}]
        text = card.build(REPO, pr(body="x" * 1234 + "<!-- hidden -->"), [f"{TASK}/task.toml"], runs, statuses)
        self.assertTrue(text.startswith(card.CANARY + "\n" + card.MARKER + "\n"))
        table = rows(text)
        self.assertEqual(table["Static checks"][0], card.NOT_RUN)
        self.assertEqual(table["Execution checks (Oracle / Nop)"][0], card.PASS)
        self.assertEqual(table["Merge gate"], (card.RUNNING, "`Task PR: waiting`"))
        self.assertEqual(table["Proposal"][0], "missing")
        self.assertEqual(table["PR description"][0], "1,234 characters")
        self.assertNotIn("GPU acceptance record", table)
        self.assertIn(f"task `{TASK}`", text)
        self.assertIn("tier `tier:cpu`", text)
        self.assertIn("set `tasks/`", text)
        self.assertIn(SHA[:7], text)
        self.assertLessEqual(len(text.splitlines()), 25)

    def test_gpu_card_has_the_acceptance_row(self):
        text = card.build(REPO, pr(labels=("tier:gpu",), draft=True), [f"{TASK}/host/service.json"], [], [],
                          evidence=None, head_tree="abc")
        table = rows(text)
        self.assertEqual(table["GPU acceptance record"][0], card.NOT_RUN)
        self.assertIn("draft", text)
        self.assertEqual(set(v[0] for k, v in table.items() if k in ("Static checks", "Execution checks (Oracle / Nop)", "Merge gate")),
                         {card.NOT_RUN})

    def test_lite_and_missing_tier_label(self):
        text = card.build(REPO, pr(labels=()), ["lite/demo/instruction.md"], [], [])
        self.assertIn("set `lite/`", text)
        self.assertIn("tier not labelled", text)

    def test_several_tasks_are_flagged(self):
        text = card.build(REPO, pr(), ["lite/a/task.toml", "lite/b/task.toml"], [], [])
        self.assertIn("changes 2 tasks", text)

    def test_pipe_in_description_does_not_break_the_table(self):
        statuses = [{"context": card.GATE, "state": "pending", "description": "a | b"}]
        text = card.build(REPO, pr(), ["lite/a/task.toml"], [], statuses)
        self.assertIn("a \\| b", text)


if __name__ == "__main__":
    unittest.main()
