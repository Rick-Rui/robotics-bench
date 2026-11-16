"""/scene: command parsing and the PR comment built from a scene check's artifacts."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import build  # noqa: E402
import scene_check  # noqa: E402
import trial_command  # noqa: E402

spec = importlib.util.spec_from_file_location('tier_command', ROOT / '.github/scripts/tier_command.py')
tier_command = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tier_command)

DIGEST = '8c9bc54f71b9eda00ab45898b342e87d3075d90a5cb607b1fe6d9e79080bc8b4'
REQUEST = 'c20f7caab61042d652c0f5972f9c4d514f74d12ca28dc1e7f163f9f93c5ffc70'


def summary(**changes):
    """The fields of a real Nop control's summary.json (PR 8, run 42670083839) that the comment reads."""
    data = dict(status='not_passed', harness='nop', gpu=1, reward=0.0, exception_info=None,
                started_at='2026-11-09T18:01:44.617843Z', finished_at='2026-11-09T18:04:06.544954+00:00',
                agent_setup=dict(started_at='2026-11-09T18:03:18.456439Z', finished_at='2026-11-09T18:03:18.456540Z'),
                task_digest=DIGEST,
                physical_metrics=dict(reason='No online strategy execution was recorded',
                                      status='no_online_strategy', trusted_inputs_verified=True))
    data.update(changes)
    return data


def receipt(**result):
    data = dict(status='passed', exit_code=0, request_sha256=REQUEST, reward=0,
                network_isolation='inherited-seccomp-deny-network-sockets',
                metrics=dict(reason='No online strategy execution was recorded', status='no_online_strategy',
                             trusted_inputs_verified=True))
    data.update(result)
    return dict(state='succeeded', job_id='e69d', request_sha256=REQUEST, gpu_seconds=98.98, queue_seconds=0.03,
                assigned_gpu=1, result=data)


class Fixture:
    def __init__(self, directory):
        self.root = Path(directory)

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def stage(self, verify=None, deploy=None):
        self.write('gpu-scene-deploy-t/verify.json',
                   verify or dict(static='success', similarity='true', preflight='true'))
        if deploy is not False:
            self.write('gpu-scene-deploy-t/deploy.json', deploy or dict(state='installed', task_digest=DIGEST))

    def control(self, summary_value=None, receipt_value=None, cleanup=True, harness='nop'):
        base = f'gpu-scene-session-t/comparison-output/{harness}-gpu1-1/'
        self.write(base + 'summary.json', summary_value or summary())
        trial = base + 'trial/comparison/task__x/remote-evidence/'
        if receipt_value is not False:
            self.write(trial + 'verified.json', receipt_value or receipt())
        if cleanup is not None:
            self.write(trial + 'cleanup.json', dict(job_id='e69d', state='succeeded', cleanup_confirmed=cleanup))


class ParseTest(unittest.TestCase):
    def test_command(self):
        self.assertEqual(scene_check.parse('/scene'), dict(command='scene', gpu='auto'))
        self.assertEqual(scene_check.parse('please\n/scene gpu=2'), dict(command='scene', gpu=2))
        self.assertEqual(scene_check.parse('/scene gpu=0'), dict(command='scene', gpu=0))
        self.assertEqual(scene_check.parse('/scene gpu=3'), dict(command='scene', gpu=3))
        for bad in ('/scene gpu=4', '/scene gpu=x', '/scene trials=2', '/scene agents=codex'):
            self.assertIn('error', scene_check.parse(bad), bad)
        for none in ('nothing', 'see `/scene`', '> /scene', '```\n/scene\n```', '/scenes/1', '/validate'):
            self.assertIsNone(scene_check.parse(none), none)

    def test_other_gpu_commands_are_unchanged(self):
        # /scene is its own workflow: the trial router must keep ignoring it.
        self.assertIsNone(trial_command.parse('/scene'))
        self.assertEqual(trial_command.parse('/oracle')['lanes'], [dict(harness='oracle', gpu='auto')])

    def test_tier_reply(self):
        self.assertEqual(tier_command.reply('/scene', 'gpu'), '')
        self.assertIn('/scene', tier_command.reply('/scene', 'cpu'))
        self.assertIn('/scene', tier_command.reply('/scene gpu=2', 'real'))


SHA = 'd0e98d0dd1e782fc8ca9019e2c6fad2050354b6c'


def sticky(failed_rows, sha=SHA, user='github-actions[bot]'):
    """The static-checks sticky comment as static-checks.yml renders it (the real one on PR 69 has one failed row)."""
    base = 'https://github.com/ZIWU-AI/robotics-bench/blob/main/TASK_REVIEW_AUTOMATION.md'
    rows = ''.join(f'| [{name}]({base}#{anchor}) | (see workflow log) |\n' for name, anchor in failed_rows)
    failed = (f'<details open>\n<summary><b>{len(failed_rows)} failed</b> ❌</summary>\n\n| Check | Details |\n'
              f'|-------|---------|\n{rows}\n</details>\n\n') if failed_rows else ''
    body = (f'### Static Checks ❌\n\n{failed}<details>\n<summary><b>28 passed</b> ✅</summary>\n\n| Check |\n|-------|\n'
            f'| [Canary strings]({base}#check-canary) |\n\n</details>\n\n<sub><a href="https://x/runs/1">Ran</a> on '
            f'<a href="https://github.com/ZIWU-AI/robotics-bench/commit/{sha}"><code>{sha[:7]}</code></a>.</sub>\n\n'
            '<!-- Sticky Pull Request Commentstatic-checks -->')
    return dict(user=user, body=body)


class StaticVerdictTest(unittest.TestCase):
    PROPOSAL = ('Task proposal / issue link', 'check-task-proposal-link')

    def test_only_the_proposal_check_failing_does_not_block_a_scene_check(self):
        self.assertEqual(scene_check.static_verdict('failure', SHA, sticky([self.PROPOSAL])), 'proposal_only')
        self.assertEqual(scene_check.static_verdict('success', SHA, None), 'success')

    def test_anything_else_keeps_the_conclusion(self):
        other = ('Canary strings', 'check-canary')
        cases = [('failure', sticky([self.PROPOSAL, other])),               # proposal plus another failure
                 ('failure', sticky([other])),                              # another check only
                 ('failure', sticky([self.PROPOSAL], sha='f' * 40)),        # comment describes another commit
                 ('failure', sticky([self.PROPOSAL], user='someone')),      # not written by the bot
                 ('failure', None), ('failure', dict(user='github-actions[bot]', body='no marker')),
                 ('cancelled', sticky([self.PROPOSAL])), ('missing', sticky([self.PROPOSAL]))]
        for conclusion, value in cases:
            self.assertEqual(scene_check.static_verdict(conclusion, SHA, value), conclusion)
        self.assertEqual(scene_check.static_verdict('', SHA, None), 'missing')

    def test_the_comment_says_the_proposal_check_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(temp)
            fixture.stage(verify=dict(static='proposal_only', similarity='true', preflight='true'))
            fixture.control()
            checks, _ = scene_check.evaluate(fixture.root)
            self.assertIs(checks[0][1], True)
            self.assertIn('the proposal check has not passed (it does not affect the scene check)', checks[0][2])


class CommentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def results(self):
        checks, facts = scene_check.evaluate(self.fixture.root)
        return {name: ok for name, ok, _ in checks}, facts

    def text(self):
        return scene_check.comment(self.fixture.root, 'tasks/a/b/t', 'https://github.com/o/r/actions/runs/1', 'abcdef0123')

    def test_passing_scene(self):
        self.fixture.stage()
        self.fixture.control()
        ok, facts = self.results()
        self.assertEqual(set(ok.values()), {True})
        self.assertEqual(int(facts['ready_seconds']), 93)
        self.assertEqual(facts['frames'], 0)
        text = self.text()
        self.assertIn('scene check · `t` ✅', text)
        self.assertIn('GPU time 1.6 min', text)
        self.assertIn('a session without actions is not replayed', text)
        self.assertIn('/validate', text)

    def test_verify_failure_stops_everything(self):
        self.fixture.stage(verify=dict(static='failure', similarity='true', preflight='false',
                                       preflight_failed='❌ manifest: ValueError: manifest archive_sha256 is not a sha256'),
                           deploy=dict(state='not_verified', error='Verify stage failed'))
        ok, _ = self.results()
        self.assertEqual(ok, dict(verify=False, deploy=None, ready=None, sealed=None, nop_zero=None, cleanup=None))
        text = self.text()
        self.assertIn('archive_sha256', text)
        self.assertIn('/check', text)
        self.assertIn('scene check · `t` ❌', text)

    def test_deploy_failure(self):
        self.fixture.stage(deploy=dict(state='failed', error='Host returned 409 for POST /deployments: digest'))
        ok, _ = self.results()
        self.assertEqual((ok['verify'], ok['deploy'], ok['ready']), (True, False, None))
        self.assertIn('409', self.text())

    def test_scene_that_does_not_start(self):
        self.fixture.stage()
        self.fixture.control(summary(status='execution_error', reward=None, agent_setup=None,
                                     error='RuntimeError: Session could not start: Online simulator startup exited 1'),
                             receipt_value=False, cleanup=True)
        ok, _ = self.results()
        self.assertEqual((ok['ready'], ok['sealed'], ok['nop_zero'], ok['cleanup']), (False, None, None, True))
        self.assertIn('startup exited 1', self.text())

    def test_nop_must_score_zero_and_receipt_must_verify(self):
        self.fixture.stage()
        won = receipt(reward=1, metrics=dict(status='complete', success=True, consistency_passed=True))
        self.fixture.control(summary(status='passed', reward=1.0), won)
        ok, facts = self.results()
        self.assertEqual((ok['sealed'], ok['nop_zero']), (True, False))
        self.assertIn('replay consistent `True`', self.text())
        self.assertIn('sending no action should score 0', self.text())

    def test_unverified_receipt_and_missing_cleanup(self):
        self.fixture.stage()
        self.fixture.control(receipt_value=receipt(network_isolation='none'), cleanup=None)
        ok, _ = self.results()
        self.assertEqual((ok['ready'], ok['sealed'], ok['nop_zero'], ok['cleanup']), (True, False, None, False))

    def test_frames_are_counted_only_when_present(self):
        self.fixture.stage()
        self.fixture.control()
        frame = (self.fixture.root / 'gpu-scene-session-t/comparison-output/nop-gpu1-1/trial/comparison/task__x'
                 / 'artifacts/app/output/cam_head.png')
        frame.parent.mkdir(parents=True)
        frame.write_bytes(b'png')
        _, facts = self.results()
        self.assertEqual(facts['frames'], 1)
        self.assertIn('1 image(s) in the run artifacts', self.text())

    # ---- with a scene probe (solution/scene_probe.sh): the hold is recorded, so the host replays it
    def probe(self, metrics=None, frames=(('chest', 2), ('left_wrist', 2), ('right_wrist', 2)), reward=0, **summary_changes):
        self.fixture.stage()
        value = dict(status='complete', success=False, consistency_passed=True, trusted_inputs_verified=True,
                     replay=dict(consistent=True, compared=4, first_failure=None))
        value.update(metrics or {})
        self.fixture.control(summary(harness='scene-probe', **summary_changes), receipt(reward=reward, metrics=value),
                             harness='scene-probe')
        scene = (self.fixture.root / 'gpu-scene-session-t/comparison-output/scene-probe-gpu1-1/trial/comparison/task__x'
                 / 'artifacts/app/output/scene')
        for camera, count in frames:
            for index in range(count):
                path = scene / camera / f'{index:04d}.png'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'png')
        return scene

    def test_probe_reports_frames_replay_predicate_and_stillness(self):
        self.probe(dict(scene_stillness=dict(max_object_displacement_m=0.0004, max_object_tilt_deg=0.2, frames=4)))
        ok, facts = self.results()
        self.assertEqual(facts['mode'], 'probe')
        self.assertEqual(facts['cameras'], dict(chest=2, left_wrist=2, right_wrist=2))
        self.assertEqual({ok[k] for k in ('frames', 'replay', 'predicate', 'still', 'nop_zero')}, {True})
        text = self.text()
        self.assertIn('scene check · `t` ✅', text)
        self.assertIn('solution/scene_probe.sh', text)
        self.assertIn('chest: 2', text)
        self.assertIn('displacement 0.4 mm', text)
        self.assertIn('4 frames compared', text)
        self.assertNotIn('a session without actions is not replayed', text)

    def test_probe_without_stillness_report_still_passes_and_says_so(self):
        self.probe()
        ok, _ = self.results()
        self.assertIsNone(ok['still'])
        text = self.text()
        self.assertIn('scene check · `t` ✅', text)
        self.assertIn('scene_stillness', text)

    def test_scene_that_moves_or_already_succeeds_fails(self):
        self.probe(dict(scene_stillness=dict(max_object_displacement_m=0.08, max_object_tilt_deg=40.0)))
        ok, _ = self.results()
        self.assertEqual((ok['still'], ok['replay']), (False, True))
        self.assertIn('scene check · `t` ❌', self.text())

    def test_predicate_true_after_doing_nothing_fails(self):
        self.probe(dict(success=True), reward=1, status='passed')
        ok, _ = self.results()
        self.assertEqual((ok['predicate'], ok['nop_zero']), (False, False))
        self.assertIn('already true although nothing was done', self.text())

    def test_inconsistent_replay_fails_with_the_first_failure(self):
        self.probe(dict(consistency_passed=False, replay=dict(consistent=False, compared=4,
                                                              first_failure=dict(entry=2, object_position_m=0.02))))
        ok, _ = self.results()
        self.assertIs(ok['replay'], False)
        self.assertIn('object_position_m', self.text())

    def test_probe_that_records_nothing_or_saves_no_frame_fails(self):
        self.probe(dict(status='no_online_strategy'), frames=())
        ok, _ = self.results()
        self.assertEqual((ok['frames'], ok['replay'], ok['predicate'], ok['still']), (False, False, None, None))
        self.assertIn('left no recorded action', self.text())

    def test_stillness_field_forms(self):
        self.assertEqual(scene_check.stillness({})[0], None)
        self.assertEqual(scene_check.stillness(dict(scene_stillness=dict(applicable=False)))[0], None)
        self.assertIn('not applicable', scene_check.stillness(dict(scene_stillness=dict(applicable=False)))[1])
        for bad in (dict(max_object_displacement_m='0', max_object_tilt_deg=0), dict(max_object_displacement_m=-1, max_object_tilt_deg=0),
                    dict(max_object_displacement_m=float('nan'), max_object_tilt_deg=0), dict(max_object_tilt_deg=1)):
            self.assertEqual(scene_check.stillness(dict(scene_stillness=bad))[0], None, bad)
        self.assertIs(scene_check.stillness(dict(scene_stillness=dict(max_object_displacement_m=0.01, max_object_tilt_deg=5)))[0], True)
        self.assertIs(scene_check.stillness(dict(scene_stillness=dict(max_object_displacement_m=0.011, max_object_tilt_deg=0)))[0], False)

    def test_frames_directly_under_scene_and_images_elsewhere(self):
        scene = self.probe(frames=())
        (scene).mkdir(parents=True, exist_ok=True)
        (scene / '0001_chest.png').write_bytes(b'png')
        (scene / '0002_chest.png').write_bytes(b'png')
        (scene / 'state.jsonl').write_text('{}')
        (scene.parent / 'oracle_observations').mkdir()
        (scene.parent / 'oracle_observations/0001_chest.png').write_bytes(b'png')   # not under scene/: not counted
        _, facts = self.results()
        self.assertEqual(facts['cameras'], dict(chest=2))

    def test_author_marker(self):
        self.probe()
        plain = self.text()
        self.assertNotIn('ziwu-scene-check', plain)
        marked = scene_check.comment(self.fixture.root, 'tasks/a/b/t', 'https://x/runs/1', 'abcdef0', author='some-author')
        self.assertIn('<!-- ziwu-scene-check author=some-author -->', marked)

    def test_no_artifacts_at_all(self):
        ok, _ = self.results()
        self.assertEqual((ok['verify'], ok['deploy']), (False, None))
        self.assertIn('❌', self.text())


class ProbeBuildTest(unittest.TestCase):
    def tree(self, temp, probe=True, timeout='5400.0'):
        source, task = Path(temp) / 'source', Path(temp) / 'task'
        (source / 'solution').mkdir(parents=True)
        (source / 'solution/solve.sh').write_text('reference\n')
        if probe:
            (source / 'solution/scene_probe.sh').write_text('probe\n')
        (task / 'solution').mkdir(parents=True)
        (task / 'solution/solve.sh').write_text('reference\n')
        (task / 'task.toml').write_text(f'[agent]\ntimeout_sec = {timeout}\nuser = "agent"\n[verifier]\ntimeout_sec = 7200.0\n')
        return source, task

    def test_probe_takes_the_place_of_solve_sh_and_the_budget_is_capped(self):
        with tempfile.TemporaryDirectory() as temp:
            source, task = self.tree(temp)
            build.use_scene_probe(source, task)
            self.assertEqual((task / 'solution/solve.sh').read_text(), 'probe\n')
            self.assertEqual((source / 'solution/solve.sh').read_text(), 'reference\n')
            text = (task / 'task.toml').read_text()
            self.assertIn('[agent]\ntimeout_sec = 900.0\n', text)
            self.assertIn('[verifier]\ntimeout_sec = 7200.0', text)

    def test_shorter_budget_is_kept_and_missing_probe_is_an_error(self):
        with tempfile.TemporaryDirectory() as temp:
            source, task = self.tree(temp, timeout='300.0')
            build.use_scene_probe(source, task)
            self.assertIn('timeout_sec = 300.0', (task / 'task.toml').read_text())
        with tempfile.TemporaryDirectory() as temp:
            source, task = self.tree(temp, probe=False)
            with self.assertRaises(ValueError):
                build.use_scene_probe(source, task)

    def test_run_py_offers_the_probe_as_a_control(self):
        source = (HERE / 'run.py').read_text()
        self.assertIn("choices=('codex','claude-code','oracle','nop','scene-probe')", source)
        self.assertIn("control = a.harness in ('oracle','nop','scene-probe')", source)
        self.assertEqual(scene_check.PROBE, build.SCENE_PROBE)


if __name__ == '__main__':
    unittest.main()
