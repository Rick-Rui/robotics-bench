"""Require fresh Oracle/Nop controls on two different GPU lanes before enabling model trials."""
import argparse
import json
from pathlib import Path

# Platform receipt verifier; each task's tests/verify_receipt.py is a byte-identical copy.
from verify_receipt import verify
from lanes import lanes


def accept(directories, task_tree):
    controls=[]; identity=None; seen=set(); implementations=set()
    for directory in directories:
        directory=Path(directory)
        summaries=list(directory.glob('**/summary.json'))
        receipts=list(directory.glob('**/remote-evidence/verified.json'))
        cleanups=list(directory.glob('**/remote-evidence/cleanup.json'))
        if tuple(map(len,(summaries,receipts,cleanups))) != (1,1,1):
            raise ValueError('Each control must contain one summary, verified receipt and cleanup')
        summary=json.loads(summaries[0].read_text())
        job=json.loads(receipts[0].read_text())
        cleanup=json.loads(cleanups[0].read_text())
        reward,metrics=verify(job)
        lane,harness=summary['gpu'],summary['harness']
        if (lane,harness) in seen or type(lane) is not int or lane not in lanes() or harness not in ('oracle','nop'):
            raise ValueError('Duplicate or invalid control identity')
        seen.add((lane,harness))
        expected=int(harness=='oracle')
        if (summary['status'] != ('passed' if expected else 'not_passed')
                or summary['reward'] != expected or reward != expected
                or summary.get('exception_info') or summary['trial_count']!=0
                or job['request']['gpu'] != lane or job['assigned_gpu'] != lane
                or cleanup != dict(job_id=job['job_id'],state='succeeded',cleanup_confirmed=True)):
            raise ValueError('Failed or incomplete control')
        digest=job['request']['parameters']['task_digest']
        if summary['task_digest'] != digest or (identity is not None and identity!=digest):
            raise ValueError('Controls use different task identities')
        identity=digest
        if harness=='oracle' and (metrics.get('status')!='complete' or metrics.get('consistency_passed') is not True):
            raise ValueError('Oracle requires independent native replay')
        execution=summary.get('oracle_execution')
        if harness=='oracle' and (not isinstance(execution,dict)
                or execution.get('schema') not in ('oracle-execution-v1','physicalrsi-reference-execution-v1')
                or execution.get('model_calls')!=0
                or type(execution.get('executed_action_count')) is not int
                or execution['executed_action_count']<=0):
            raise ValueError('Oracle reference completion receipt is required')
        implementations.add(job['result']['implementation_version'])
        controls.append(dict(gpu=lane,harness=harness,job_id=job['job_id'],reward=reward,
                             source_sha=summary['source_sha'],metrics=metrics,
                             oracle_execution=execution,
                             implementation_version=job['result']['implementation_version'],
                             cleanup_confirmed=True))
    used = sorted({g for g, _ in seen})
    if len(used) != 2 or seen != {(g,h) for g in used for h in ('oracle','nop')}:
        raise ValueError('Oracle and Nop required on two different GPU lanes')
    if len(implementations)!=1:
        raise ValueError('Controls use different remote implementations')
    return dict(schema='ziwu.remote-bimanual-acceptance.v1',status='passed',gpus=used,
                task_digest=identity,task_tree=task_tree,controls=controls,
                model_calls=0,benchmark_trials=0)


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--task-tree',required=True)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('controls',nargs=4,type=Path)
    a=p.parse_args();result=accept(a.controls,a.task_tree)
    a.output.parent.mkdir(parents=True,exist_ok=True)
    a.output.write_text(json.dumps(result,indent=2)+'\n')
