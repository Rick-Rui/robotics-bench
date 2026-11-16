"""Tiers and commands: one PR is one tier of one task (docs/COMMANDS.md)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import build  # noqa: E402
import preflight  # noqa: E402
import trial_command  # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tier = load(ROOT / 'ci_checks/check-task-tier.py', 'check_task_tier')
tier_command = load(ROOT / '.github/scripts/tier_command.py', 'tier_command')
T = 'tasks/t/'


def files(*names):
    return [dict(filename=T + n.lstrip('+'), status='added' if n.startswith('+') else 'modified') for n in names]


class TierTest(unittest.TestCase):
    def test_task_roots(self):
        self.assertEqual(tier.task_root('tasks/t/host/service.json'), ('tasks/t', 'host/service.json'))
        nested = 'tasks/engineering-sciences/mechanical-engineering/t'
        self.assertEqual(tier.task_root(nested + '/instruction.md'), (nested, 'instruction.md'))
        self.assertEqual(tier.task_root('lite/t/task.toml'), ('lite/t', 'task.toml'))
        for path in ('tasks/README.md', 'tasks/engineering-sciences/t/x', 'docs/x.md'):
            self.assertIsNone(tier.task_root(path))

    def test_new_tiers(self):
        self.assertEqual(tier.classify(files('+instruction.md', '+task.toml', '+tests/test.sh'))['tier'], 'cpu')
        gpu = tier.classify(files('+host/service.json', '+gpu/instruction-gpu.md', 'solution/solve.sh'))
        self.assertEqual((gpu['tier'], gpu['problems']), ('gpu', []))
        real = tier.classify(files('+real/task.json'))
        self.assertEqual((real['tier'], real['problems']), ('real', []))

    def test_whole_gpu_task(self):
        whole = tier.classify(files('+instruction.md', '+host/service.json'), validate_env=lambda t: 'ziwu-gpu')
        self.assertEqual((whole['tier'], whole['problems']), ('gpu', []))

    def test_gpu_only_task_without_a_cpu_tier_fails(self):
        new = files('+instruction.md', '+host/service.json')
        gpu_only = dict(validate_env=lambda t: 'ziwu-gpu', in_base=lambda t: False)
        failed = tier.classify(new, **gpu_only)
        self.assertEqual((failed['tier'], len(failed['problems']), failed['warnings']), ('gpu', 1, []))
        self.assertIn('CPU tier first', failed['problems'][0])
        # a GPU-only PR open when the rule took effect is warned; a fix to a merged GPU-only task and a tiered task pass
        legacy = tier.classify(new, legacy=True, **gpu_only)
        self.assertEqual((legacy['problems'], len(legacy['warnings'])), ([], 1))
        self.assertEqual(tier.classify(files('tests/verify.py'), validate_env=lambda t: 'ziwu-gpu')['problems'], [])
        self.assertEqual(tier.classify(files('+host/service.json'))['problems'], [])
        self.assertEqual(tier.classify(files('solution/a'), validate_env=lambda t: 'ziwu-gpu')['tier'], 'gpu')
        retired = tier.classify(files('+instruction.md', '+task.toml'), validate_env=lambda t: 'modal')
        self.assertEqual((retired['tier'], retired['problems']), ('gpu', []))
        self.assertEqual(len(set(tier.COLORS.values())), 3)

    def test_problems(self):
        self.assertTrue(tier.classify(files('+instruction.md', '+host/service.json'))['problems'])
        self.assertTrue(tier.classify(files('+instruction.md', '+real/task.json'))['problems'])
        self.assertTrue(tier.classify(files('+host/service.json'), in_base=lambda t: False)['problems'])
        two = files('+instruction.md') + [dict(filename='tasks/u/instruction.md', status='added')]
        self.assertTrue(tier.classify(two)['problems'])

    def test_fix_prs(self):
        self.assertEqual(tier.classify(files('real/task.json'))['tier'], 'real')
        self.assertEqual(tier.classify(files('tests/gpu/x.py', 'solution/a'))['tier'], 'gpu')
        self.assertEqual(tier.classify(files('README.md'))['tier'], 'cpu')
        self.assertEqual(tier.classify([dict(filename='docs/x.md', status='modified')])['tier'], '')


class CommandTest(unittest.TestCase):
    def test_wrong_tier_gets_a_reply(self):
        self.assertIn('/real', tier_command.reply('/real', 'cpu'))
        self.assertIn('/run', tier_command.reply('/run trials=3', 'real'))
        self.assertIn('/logs', tier_command.reply('/logs', 'gpu'))

    def test_right_tier_and_quotes_are_silent(self):
        for body, t in (('/validate', 'cpu'), ('/run', 'gpu'), ('/real harness=nop', 'real'), ('/logs', 'real'),
                        ('see `/real`', 'cpu'), ('> /real', 'cpu'), ('```\n/real\n```', 'gpu'), ('/runs/123', 'real')):
            self.assertEqual(tier_command.reply(body, t), '', (body, t))

    def test_gpu_trial_commands(self):
        run = trial_command.parse('/run')
        self.assertEqual(run['lanes'], [dict(harness='codex', gpu='auto'), dict(harness='claude-code', gpu='auto')])
        self.assertEqual(trial_command.parse('/cheat agents=codex trials=3'),
                         dict(command='cheat', lanes=[dict(harness='codex', gpu='auto')], trials=3))
        self.assertEqual(trial_command.parse('/oracle')['lanes'], [dict(harness='oracle', gpu='auto')])
        self.assertEqual(trial_command.parse('/test')['lanes'], [dict(harness='claude-code', gpu='auto')])
        for bad in ('/run env=modal', '/run trials=9', '/run agents=qoder', '/test trials=2', '/oracle trials=2'):
            self.assertIn('error', trial_command.parse(bad), bad)
        for none in ('nothing', 'see `/run`', '/runs/1', '> /run'):
            self.assertIsNone(trial_command.parse(none), none)


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name) / 'repo'
        (self.repo / 'tasks/x/y/z/real').mkdir(parents=True)
        (self.repo / 'tasks/x/y/z/instruction.md').write_text('do it\n')
        self.git('init', '-q')
        self.commit('cpu')

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        return subprocess.check_output(('git',) + args, cwd=self.repo, env=env, text=True).strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)
        return self.git('rev-parse', 'HEAD')

    def digest(self, sha, name):
        return build.materialize(self.repo, sha, Path(self.temp.name) / name, 'tasks/x/y/z')

    def test_real_tier_does_not_change_the_gpu_digest(self):
        before = self.digest(self.git('rev-parse', 'HEAD'), 'a')
        (self.repo / 'tasks/x/y/z/real/task.json').write_text('{}\n')
        after = self.digest(self.commit('real tier'), 'b')
        self.assertEqual(before, after)
        self.assertFalse((Path(self.temp.name) / 'b/real').exists())
        (self.repo / 'tasks/x/y/z/instruction.md').write_text('do it differently\n')
        self.assertNotEqual(after, self.digest(self.commit('change'), 'c'))

    def test_preflight_stops_a_task_without_a_gpu_tier(self):
        result = preflight.preflight(self.repo, self.git('rev-parse', 'HEAD'), 'tasks/x/y/z', offline=True)
        self.assertFalse(result['passed'])
        self.assertFalse([c for c in result['checks'] if c['name'] == 'tier'][0]['ok'])
        json.dumps(result)


if __name__ == '__main__':
    unittest.main()
