"""One bounded Harbor trial on Actions CPU, with a registered remote simulator."""
import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent/'isaac-model-comparison'))
from harness_common import make_preflight, job_config, execute, collect, write_json, now, scrub_tree
from profiles import route_for, PROFILES
from build import TASK, build, materialize, prepare_image, use_scene_probe
sys.path.insert(0, str(HERE))
from lanes import parse_gpu


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--harness', choices=('codex','claude-code','oracle','nop','scene-probe'), required=True,
                   help='scene-probe: the task\'s solution/scene_probe.sh run as a control (/scene)')
    p.add_argument('--gpu', required=True, help='"auto" (any free card) or a lane listed in GPU_POOL_LANES')
    p.add_argument('--source-sha', required=True)
    p.add_argument('--request-id', required=True)
    p.add_argument('--url', default='http://127.0.0.1:18768')
    p.add_argument('--token-file', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--stage', choices=('all','preflight'), default='all')
    p.add_argument('--task-path', default=TASK, help='task directory in this repository')
    p.add_argument('--tier', choices=('', 'gpu'), default='',
                   help='gpu: run the GPU tier of a multi-tier task; empty for ziwu-gpu tasks')
    p.add_argument('--cheat', action='store_true',
                   help='model trial with the adversarial prompt (.github/hack-trial-prompt.md) in front of the instruction')
    p.add_argument('--task-repo', type=Path, default=ROOT,
                   help='git checkout containing --source-sha (default: this checkout)')
    a = p.parse_args()
    a.gpu = parse_gpu(a.gpu)
    # The task comes from the source commit, not from this (trusted) checkout.
    task_toml = tomllib.loads(subprocess.check_output(
        ['git', 'show', f'{a.source_sha}:{a.task_path}/task.toml'], cwd=a.task_repo, text=True))
    slug = Path(a.task_path).name
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    summary = dict(status='preparing', harness=a.harness, gpu=a.gpu, trial_count=0, tier=a.tier or None,
                   model='k3.1-pro' if a.harness in ('codex','claude-code') else None,
                   reward=None, started_at=now(), source_sha=a.source_sha, task_path=a.task_path,
                   isaac_version='5.1.0.0', cpus=task_toml['environment']['cpus'],
                   memory_mib=task_toml['environment']['memory_mb'],
                   agent_timeout_sec=task_toml['agent']['timeout_sec'])
    secret = os.environ.get('KIMI_CODE_API_KEY','')
    code = 1
    try:
        if version('harbor') != '0.23.0': raise ValueError('Pinned Harbor 0.23.0 required')
        prepare_image()
        with tempfile.TemporaryDirectory(prefix='ziwu-remote-') as temp:
            private = Path(temp)
            env = os.environ.copy()
            for item in PROFILES.values(): env.pop(item['secret'],None)
            env['PATH'] = str(Path(sys.executable).parent)+os.pathsep+env.get('PATH','')
            env['PYTHONPATH'] = str(HERE)+os.pathsep+str(HERE.parent/'isaac-model-comparison')
            control = a.harness in ('oracle','nop','scene-probe')
            if not control:
                if not secret: raise ValueError('KIMI_CODE_API_KEY is missing')
                route = route_for('kimi',a.harness,private)
                summary['route'] = route['provenance']
                env.update(route['env'])
                env.update({k:secret for k in route['credential_env_names']})
                preflight = private/'preflight'; make_preflight(preflight)
                (preflight/'environment/Dockerfile').write_text('FROM ziwu-pr8-cpu:verified\nWORKDIR /app\n')
                cfg = job_config(route,preflight,out/'preflight','check',secret,300)
                summary['status']='preflight_running'; write_json(out/'summary.json',summary)
                status = execute(cfg,private,out,'preflight',env)
                summary['preflight'] = collect(out/'preflight/check')
                if status or summary['preflight']['status']!='passed':
                    raise RuntimeError('Native harness preflight failed')
                if a.stage=='preflight':
                    summary['status']='preflight_passed'; return 0
            source = private/'source'
            task_digest = materialize(a.task_repo,a.source_sha,source,a.task_path)
            if not control:
                # Committed outside the task directory: the record names the task digest.
                acceptance = json.loads((HERE/'evidence'/(slug+'.json')).read_text())
                gpus = acceptance.get('gpus')
                if (acceptance.get('task_digest') != task_digest or acceptance.get('status')!='passed'
                        or not isinstance(gpus, list) or len(gpus) != 2 or len(set(gpus)) != 2):
                    raise ValueError('Fresh task acceptance on two GPUs is required')
            task = private/'task'; build(source,task,control,a.tier or None)
            if a.harness == 'scene-probe':
                use_scene_probe(source,task)
            if a.cheat:
                if control: raise ValueError('--cheat is for model trials')
                prompt = (HERE.parents[1]/'.github/hack-trial-prompt.md').read_text()
                (task/'instruction.md').write_text(prompt + (task/'instruction.md').read_text())
                summary['cheat'] = True
            if control:
                cfg = dict(job_name='comparison',jobs_dir=str(out/'trial'),n_attempts=1,
                           n_concurrent_trials=1,retry={'max_retries':0},
                           agents=[{'name':'oracle' if a.harness == 'scene-probe' else a.harness}],tasks=[{'path':str(task)}])
            else:
                cfg = job_config(route,task,out/'trial','comparison',secret,int(task_toml['agent']['timeout_sec']))
            cfg['environment'] = {'import_path':'environment:RemoteIsaacEnvironment','delete':True,
                'kwargs':dict(remote_url=a.url, remote_private=str(private), remote_gpu=a.gpu,
                              remote_request_id=a.request_id,remote_task_digest=task_digest,
                              remote_token_file=str(a.token_file.resolve()))}
            summary.update(status='trial_running',trial_count=0 if control else 1,task_digest=task_digest)
            write_json(out/'summary.json',summary)
            status = execute(cfg,private,out,'trial',env)
            summary.update(collect(out/'trial/comparison'))
            assigned = list((out/'trial').glob('**/remote-evidence/verified.json'))
            if len(assigned) == 1:
                summary['assigned_gpu'] = json.loads(assigned[0].read_text()).get('assigned_gpu')
            summary['harbor_exit_code']=status
            if a.harness == 'oracle':
                receipts = list((out/'trial/comparison').glob('*/artifacts/app/output/oracle-execution.json'))
                if len(receipts) != 1:
                    summary.update(status='execution_error', reward=None,
                                   error='Oracle reference did not finish; inspect agent/oracle.txt')
                else:
                    summary['oracle_execution'] = json.loads(receipts[0].read_text())
            if status and summary['status'] in ('passed','not_passed'):
                summary.update(status='execution_error',reward=None)
            code = 0 if summary['status'] in ('passed','not_passed') else 1
            if a.harness == 'oracle' and summary['status'] != 'passed':
                code = 1
    except Exception as exc:
        summary.update(status='execution_error',reward=None,error=type(exc).__name__+': '+str(exc))
    finally:
        summary['finished_at']=now(); write_json(out/'summary.json',summary)
        scrub_tree(out,secret)
        scrub_tree(out,a.token_file.read_text().strip())
        print(json.dumps({'status':summary['status'],'reward':summary['reward']}))
    return code


if __name__=='__main__':
    raise SystemExit(main())
