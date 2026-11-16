"""Admission must not mistake a green workflow or partial Oracle for acceptance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import os
sys.path.insert(0,str(Path(__file__).resolve().parent))
from accept import accept

class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.controls=self.build((1,2))
    def build(self,gpus,root=None):
        root=root or self.root;controls=[]
        for gpu in gpus:
            for harness in ('oracle','nop'):
                p=root/f'{harness}-{gpu}';p.mkdir();controls.append(p)
                reward=int(harness=='oracle');jid=f'{gpu}-{harness}'
                summary=dict(gpu=gpu,harness=harness,status='passed' if reward else 'not_passed',
                             reward=reward,trial_count=0,task_digest='a'*64,source_sha='b'*40)
                if reward:
                    summary['oracle_execution']=dict(schema='physicalrsi-reference-execution-v1',
                                                     model_calls=0,executed_action_count=713)
                metrics=(dict(status='complete',success=True,consistency_passed=True) if reward else
                         dict(status='no_online_strategy',trusted_inputs_verified=True))
                job=dict(job_id=jid,state='succeeded',assigned_gpu=gpu,request_sha256='c'*64,
                         request=dict(gpu=gpu,parameters={'task_digest':'a'*64}),
                         result=dict(status='passed',exit_code=0,request_sha256='c'*64,reward=reward,
                                     network_isolation='inherited-seccomp-deny-network-sockets',
                                     implementation_version='runtime-v1',metrics=metrics))
                (p/'summary.json').write_text(json.dumps(summary))
                evidence=p/'remote-evidence';evidence.mkdir()
                (evidence/'verified.json').write_text(json.dumps(job))
                (evidence/'cleanup.json').write_text(json.dumps(dict(job_id=jid,state='succeeded',cleanup_confirmed=True)))
        return controls
    def tearDown(self):self.temp.cleanup()
    def change(self,relative,mutate):
        p=self.controls[0]/relative;d=json.loads(p.read_text());mutate(d);p.write_text(json.dumps(d))
    def test_four_matching_controls_admit_no_model_result(self):
        result=accept(self.controls,'d'*40)
        self.assertEqual(result['gpus'],[1,2]);self.assertEqual(result['model_calls'],0)
        self.assertEqual(result['benchmark_trials'],0);self.assertEqual(len(result['controls']),4)
    def test_partial_oracle_is_rejected_even_with_valid_replay(self):
        self.change('summary.json',lambda d:d.update(status='execution_error',reward=None))
        with self.assertRaisesRegex(ValueError,'Failed or incomplete'):accept(self.controls,'d'*40)
    def test_cleanup_must_be_confirmed(self):
        self.change('remote-evidence/cleanup.json',lambda d:d.update(cleanup_confirmed=False))
        with self.assertRaises(ValueError):accept(self.controls,'d'*40)
    def test_task_and_gpu_identity_are_bound(self):
        self.change('summary.json',lambda d:d.update(task_digest='e'*64))
        with self.assertRaisesRegex(ValueError,'different task'):accept(self.controls,'d'*40)
    def test_missing_or_duplicate_control_is_rejected(self):
        with self.assertRaises(ValueError):accept(self.controls[:3],'d'*40)
        with self.assertRaises(ValueError):accept(self.controls[:3]+self.controls[:1],'d'*40)
    def test_inconsistent_replay_never_admits_oracle(self):
        self.change('remote-evidence/verified.json',lambda d:d['result']['metrics'].update(consistency_passed=False))
        with self.assertRaises(ValueError):accept(self.controls,'d'*40)
    def test_missing_reference_completion_is_rejected(self):
        self.change('summary.json',lambda d:d.pop('oracle_execution'))
        with self.assertRaisesRegex(ValueError,'completion receipt'):accept(self.controls,'d'*40)
    def test_mixed_remote_implementations_are_rejected(self):
        self.change('remote-evidence/verified.json',lambda d:d['result'].update(implementation_version='runtime-v2'))
        with self.assertRaisesRegex(ValueError,'different remote'):accept(self.controls,'d'*40)
    def test_any_two_distinct_lanes_are_accepted(self):
        with tempfile.TemporaryDirectory() as other:
            result=accept(self.build((3,0),Path(other)),'d'*40)
        self.assertEqual(result['gpus'],[0,3])
    def test_lanes_must_differ_and_be_configured(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            same=self.build((2,),Path(a))+self.build((2,),Path(b))   # four controls, one GPU
            with self.assertRaises(ValueError):accept(same,'d'*40)
        with mock.patch.dict(os.environ,{'GPU_POOL_LANES':'0,3'}):
            with self.assertRaisesRegex(ValueError,'invalid control identity'):accept(self.controls,'d'*40)
if __name__=='__main__':unittest.main()
