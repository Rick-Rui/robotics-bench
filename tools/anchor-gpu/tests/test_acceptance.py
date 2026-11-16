import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'remote'))
from acceptance import manifest, receipt, selected_gpus
from protocol import SCHEMA, digest


def completed(gpus=(1, 2)):
    rows = []
    for index, item in enumerate(manifest(gpus)):
        request = dict(workload='isaac-drop-v1', gpu=item['gpu'], parameters=item['parameters'])
        positive = item['parameters']['gravity'] > 0
        result = dict(schema=SCHEMA, request_sha256=digest(request), status='passed',
                      trajectory=[dict(step=i, z=0.1 if positive else 1.0) for i in range(121)],
                      score=float(positive), success=positive, exit_code=0)
        remote = dict(request=request, result=result, state='succeeded', assigned_gpu=item['gpu'],
                      job_id=f'{index:032x}', gpu_seconds=20, queue_seconds=0,
                      request_sha256=digest(request))
        rows.append(dict(item=item, state='succeeded', remote=remote))
    return dict(items=rows)


class AcceptanceTests(unittest.TestCase):
    def test_selected_lane_requires_both_controls_and_correct_affinity(self):
        for gpus in ((1,), (2,)):
            result = receipt(completed(gpus), gpus)
            self.assertEqual(result['selected_gpus'], list(gpus))
            self.assertEqual([r['score'] for r in result['rows']], [1, 0])
            self.assertEqual({r['assigned_gpu'] for r in result['rows']}, set(gpus))
            with self.assertRaises(ValueError):
                receipt(completed(gpus), (1, 2))

    def test_reject_unavailable_or_duplicate_gpu_selections(self):
        for value in ('0', '3', 'auto', '1,1', '2,1', '1;exit', ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                selected_gpus(value)

    def test_controls_produce_no_benchmark_claim(self):
        result = receipt(completed())
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(result['benchmark_trials'], 0)
        self.assertEqual([r['score'] for r in result['rows']], [1, 0, 1, 0])

    def test_reject_wrong_gpu_stale_result_or_failed_native_exit(self):
        for field, value in [('assigned_gpu', 0), ('exit_code', 1), ('request_sha256', 'stale')]:
            batch = completed()
            remote = batch['items'][0]['remote']
            (remote if field == 'assigned_gpu' else remote['result'])[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                receipt(batch)

    def test_missing_or_reused_jobs_cannot_count_as_four_controls(self):
        batch = completed()
        batch['items'].pop()
        with self.assertRaises(ValueError):
            receipt(batch)
        batch = completed()
        batch['items'][1]['remote']['job_id'] = batch['items'][0]['remote']['job_id']
        with self.assertRaises(ValueError):
            receipt(batch)
