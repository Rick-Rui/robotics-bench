"""GPU access for the author of a task whose CPU tier is merged (gpu_grant.py)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gpu_grant  # noqa: E402
import scene_trust  # noqa: E402


class GrantTest(unittest.TestCase):
    def test_cpu_tier_merged_by_the_same_author(self):
        pr = dict(number=69, user='Alice', merged=True)
        self.assertTrue(gpu_grant.decide('alice', True, pr)['granted'])
        self.assertEqual(gpu_grant.decide('alice', True, pr)['cpu_pr'], 69)

    def test_no_grant(self):
        self.assertFalse(gpu_grant.decide('alice', False, None)['granted'])          # CPU tier not on main
        self.assertFalse(gpu_grant.decide('alice', True, None)['granted'])           # inherited or pushed
        legacy = gpu_grant.decide('alice', True, dict(number=8, user='alice', merged=True), gpu_only=True)
        self.assertFalse(legacy['granted'])                                          # merged GPU-only task
        self.assertIn('GPU', legacy['reason'])
        other = gpu_grant.decide('alice', True, dict(number=5, user='bob', merged=True))
        self.assertFalse(other['granted'])
        self.assertIn('#5', other['reason'])


class QuotaTest(unittest.TestCase):
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)

    def comment(self, hours_ago, body, user=gpu_grant.BOT):
        when = (self.now - timedelta(hours=hours_ago)).isoformat().replace('+00:00', 'Z')
        return dict(user=user, created_at=when, body=body)

    def test_validate_quota_counts_the_bots_markers_of_the_last_day(self):
        mark = gpu_grant.marker('validate', 'alice')
        comments = [self.comment(1, mark), self.comment(30, mark), self.comment(2, mark, user='alice'),
                    self.comment(3, gpu_grant.marker('validate', 'bob'))]
        self.assertEqual(gpu_grant.quota(comments, 'Alice', 'validate', self.now),
                         dict(used=1, limit=2, allowed=True))
        comments.append(self.comment(4, mark))
        self.assertFalse(gpu_grant.quota(comments, 'alice', 'validate', self.now)['allowed'])

    def test_scene_marker_is_the_one_the_scene_check_writes(self):
        mark = gpu_grant.marker('scene', 'alice')
        self.assertEqual(scene_trust.MARKER.findall(mark), ['alice'])
        self.assertEqual(gpu_grant.quota([self.comment(1, mark)], 'alice', 'scene', self.now)['used'], 1)


if __name__ == '__main__':
    unittest.main()
