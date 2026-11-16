"""Build CPU task adapters from committed task bytes; never change the policy API."""
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tomllib

TASK = 'tasks/engineering-sciences/mechanical-engineering/physicalrsi-bimanual-control'
IMAGE = 'sha256:efd2f349d4fbaf9ed64ee0339913b8cc51a6b94db237db5a9a50fe602d0b9b10'


def materialize(repo, sha, destination, task=TASK):
    """The task tree the GPU pool sees, and its digest. The real-robot tier (real/) is left out: it is
    submitted later as its own PR and never runs on the pool, so adding it must not change the digest
    the GPU acceptance record names."""
    raw = subprocess.check_output(['git', 'archive', sha+':'+task], cwd=repo)
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        archive.extractall(destination, filter='data')
    shutil.rmtree(destination/'real', ignore_errors=True)
    files = {str(p.relative_to(destination)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in destination.rglob('*') if p.is_file()}
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# The task commits its own agent and verifier container definitions; the
# adapter copies them unchanged and only writes the Harbor run configuration.
AGENT_FILES = ('Dockerfile', 'check_session.py', 'start-pen-render', 'pen-session.sudoers', 'remote-role.txt')
VERIFIER_FILES = ('Dockerfile', 'test.sh', 'verify_receipt.py', 'remote-role.txt')


# A task with several tiers (one directory, cpu / gpu / real) runs its GPU tier with
# `tier="gpu"`: the agent container is environment/gpu/agent/Dockerfile built in the environment/ context
# (only app/ and gpu/agent/ are copied), the verifier container is tests/gpu/, the instruction gpu/instruction-gpu.md,
# and the host side comes from host/service.json as for any GPU task.
TIER_GPU_FILES = ('host/service.json', 'gpu/instruction-gpu.md', 'environment/gpu/agent/Dockerfile',
                  'environment/gpu/agent/remote-role.txt', 'tests/gpu/Dockerfile', 'tests/gpu/test.sh',
                  'tests/gpu/verify_receipt.py', 'tests/gpu/remote-role.txt')
TIER_VERIFIER_FILES = ('Dockerfile', 'test.sh', 'verify_receipt.py', 'remote-role.txt')
REMOTE_COLLECT = '/usr/local/bin/collect-remote-recording'


def tiers(source):
    """Tiers a task offers: 'gpu' (ziwu-gpu task, or a multi-tier task with every GPU tier file) and/or 'cpu'."""
    source = Path(source)
    task = tomllib.loads((source/'task.toml').read_text())
    if task.get('environment', {}).get('validate_env') == 'ziwu-gpu':
        return ['gpu']
    out = ['cpu']
    if all((source/name).is_file() for name in TIER_GPU_FILES):
        out.append('gpu')
    return out


def build_tier_gpu(source, target, control=False):
    target.mkdir()
    shutil.copyfile(source/'gpu/instruction-gpu.md', target/'instruction.md')
    env = target/'environment'
    ignore = shutil.ignore_patterns('__pycache__', '*.pyc')
    shutil.copytree(source/'environment/app', env/'app', ignore=ignore)
    shutil.copytree(source/'environment/gpu/agent', env/'gpu/agent', ignore=ignore)
    shutil.copy2(source/'environment/gpu/agent/Dockerfile', env/'Dockerfile')
    shutil.copy2(source/'environment/gpu/agent/remote-role.txt', env/'remote-role.txt')
    tests = target/'tests'; tests.mkdir()
    for name in TIER_VERIFIER_FILES:
        shutil.copy2(source/'tests/gpu'/name, tests/name)
    if control:
        shutil.copytree(source/'solution', target/'solution', ignore=ignore)
    (target/'task.toml').write_text(run_config(source/'task.toml', tier='gpu'))


def build(source, target, control=False, tier=None):
    if tier == 'gpu' and tiers(source) == ['cpu', 'gpu']:
        return build_tier_gpu(Path(source), Path(target), control)
    if tier not in (None, 'gpu'):
        raise ValueError('Unsupported tier for the GPU pool: ' + str(tier))
    if tiers(source) != ['gpu']:
        raise ValueError('Task is not a ziwu-gpu task; use tier="gpu" for the GPU tier of a multi-tier task')
    target.mkdir()
    shutil.copyfile(source/'instruction.md', target/'instruction.md')
    shutil.copytree(source/'environment/payload', target/'environment/payload',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    tests = target/'tests'; tests.mkdir()
    for names, src, dst in [(AGENT_FILES, source/'environment', target/'environment'),
                            (VERIFIER_FILES, source/'tests', tests)]:
        for name in names:
            shutil.copy2(src/name, dst/name)
    if control:
        shutil.copytree(source/'solution', target/'solution')
    (target/'task.toml').write_text(run_config(source/'task.toml'))


SCENE_PROBE = 'solution/scene_probe.sh'
SCENE_PROBE_LIMIT_SEC = 900.0


def use_scene_probe(source, task):
    """Turn a control task built by build(..., control=True) into the scene probe of /scene.

    The probe is the task's own solution/scene_probe.sh: it observes, holds still for a few steps and saves the
    public camera frames under /app/output/scene/. It runs the way the Oracle control runs solution/solve.sh
    (Harbor's oracle agent), so it takes solve.sh's place in the built task; the committed task is not changed.
    The agent budget is capped: a scene check must not hold a GPU for the task's full agent budget."""
    source, task = Path(source), Path(task)
    if not (source/SCENE_PROBE).is_file():
        raise ValueError('Task has no ' + SCENE_PROBE)
    shutil.copyfile(source/SCENE_PROBE, task/'solution/solve.sh')
    lines, section = [], ''
    for line in (task/'task.toml').read_text().splitlines():
        if line.startswith('['):
            section = line
        if section == '[agent]' and line.startswith('timeout_sec = '):
            line = f"timeout_sec = {min(float(line.split('=', 1)[1]), SCENE_PROBE_LIMIT_SEC)}"
        lines.append(line)
    (task/'task.toml').write_text('\n'.join(lines) + '\n')


def run_config(task_toml, tier=None):
    """Harbor run configuration derived from the committed task.toml.

    The agent and verifier containers run on a CPU runner, so both declare
    gpus = 0 here; the GPU belongs to the remote session on the ZIWU pool.
    tier="gpu": the GPU tier of a multi-tier task (its task.toml describes the CPU tier): the collect hook is the
    pool's remote collection and the artifacts are /app/output.
    """
    task = tomllib.loads(Path(task_toml).read_text())
    env, agent, verifier = task['environment'], task['agent'], task['verifier']
    venv = verifier['environment']
    if tier == 'gpu' and env.get('validate_env') != 'ziwu-gpu':
        task = dict(task, artifacts=['/app/output'])
        verifier = dict(verifier, collect=[{'command': REMOTE_COLLECT, 'service': 'main', 'user': 'root',
                                            'timeout_sec': 14600.0}])
    elif env.get('validate_env') != 'ziwu-gpu':
        raise ValueError('Task does not declare validate_env = "ziwu-gpu"')
    q = json.dumps
    lines = ['schema_version = ' + q(task['schema_version']),
             'artifacts = ' + q(task['artifacts']),
             '[task]', 'name = ' + q(task['task']['name']), 'description = ' + q(task['task']['description']),
             '[agent]', f"timeout_sec = {float(agent['timeout_sec'])}", 'user = ' + q(agent['user']),
             '[environment]', 'network_mode = ' + q(env['network_mode']),
             f"build_timeout_sec = {float(env['build_timeout_sec'])}", f"cpus = {int(env['cpus'])}",
             f"memory_mb = {int(env['memory_mb'])}", 'gpus = 0', 'workdir = ' + q(env['workdir']),
             '[verifier]', f"timeout_sec = {float(verifier['timeout_sec'])}",
             'environment_mode = ' + q(verifier['environment_mode']), 'user = ' + q(verifier['user'])]
    for hook in verifier.get('collect', []):
        lines += ['[[verifier.collect]]', 'command = ' + q(hook['command']), 'service = ' + q(hook['service']),
                  'user = ' + q(hook['user']), f"timeout_sec = {float(hook['timeout_sec'])}"]
    lines += ['[verifier.environment]', 'network_mode = ' + q(venv['network_mode']),
              f"cpus = {int(venv['cpus'])}", f"memory_mb = {int(venv['memory_mb'])}", 'gpus = 0',
              'workdir = ' + q(venv['workdir'])]
    return '\n'.join(lines) + '\n'


def prepare_image():
    info = json.loads(subprocess.check_output(['docker','image','inspect',IMAGE]))[0]
    if info['Id'] != IMAGE: raise ValueError('CPU image identity mismatch')
    subprocess.run(['docker','tag',IMAGE,'ziwu-pr8-cpu:verified'],check=True)
