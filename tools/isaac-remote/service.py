"""Task-declared host service contract (host/service.json, ziwu-gpu-service-v1).

A GPU task tells the platform how to run its three trusted host phases:

- serve:   start the simulation service for one session; it must create its
           ready file (the session endpoint) once it accepts tool calls;
- collect: seal the online recording after the agent phase;
- verify:  replay the sealed recording in a fresh, network-denied process and
           write metrics.json and reward.txt into {verification}.

Session protocol the serve phase must implement: write {online}/endpoint.json
as {"url": "http://127.0.0.1:<port>", "token": "<secret>"} once ready; serve
POST /call (JSON body <= 64 KiB, reply <= 64 MiB) and GET /health (JSON with
boolean observation_available and online_phase_open), both with Bearer token.
The host proxies only these two routes to the agent. After the agent phase the
platform runs collect, which must stop the session and seal {recording}; serve
must then exit 0 within 30 s. verify writes metrics.json with status
("complete", or "no_online_strategy" for an idle session with
trusted_inputs_verified), success and consistency_passed, and reward.txt equal
to int(success and consistency_passed).

Commands are argv lists. Allowed placeholders: {root} (installed deployment),
{python} (platform task interpreter), {online} (session directory), {recording}
(sealed recording), {verification} (verifier output), {online_limit} (seconds).
Paths are written with {root}; nothing outside the deployment is named.

The optional "install" section tells install.py where the runtime manifest is,
which registration fields the task's verifier expects, and any rewrite of a
runtime-archive file the installation must make ({runtime} = installed runtime).
Its optional runtime_archive_url (a Hugging Face dataset file pinned to a
commit) lets /validate install the task automatically: the host downloads the
archive once and checks it against the manifest's archive_sha256.
Task files are never rewritten.

A runtime can also be a shared base package plus a small task archive. The
task's manifest then carries {"base": {"id", "archive_sha256"}} and lists only
its own files. The base is a platform record, bases/<id>.json next to this
file (schema ziwu-runtime-base-v1): the archive's sha256, size and pinned URL,
the sha256 of every file in it, the rewrites it needs at installation, and
task_prefixes, the only places a task archive may add files. A task file that
is also a base file must be identical to it.

Deployments installed before this contract have no host/service.json; they get
LEGACY_SERVICE, which reproduces the original bimanual worker and installer.
"""
import json
from pathlib import Path
import re

SCHEMA = 'ziwu-gpu-service-v1'
PLACEHOLDERS = {'root', 'python', 'online', 'recording', 'verification', 'online_limit'}
PHASES = ('serve', 'collect', 'verify')
INSTALL_PLACEHOLDERS = {'runtime'}

_KIT_ARGS = ('--enable isaacsim.replicator.behavior --enable isaacsim.sensors.camera '
             '--/app/extensions/registryEnabled=false')

LEGACY_SERVICE = {
    'schema': SCHEMA,
    'cwd': 'runtime',
    'pythonpath': ['runtime', 'runtime/XPolicyLab',
                   'runtime/third_party/IsaacLab/source/isaaclab',
                   'runtime/third_party/IsaacLab/source/isaaclab_assets',
                   'runtime/third_party/IsaacLab/source/isaaclab_tasks',
                   'runtime/third_party/curobo/src'],
    'serve': {
        'argv': ['{python}', '{root}/environment/supervise_service.py',
                 '--registration', '{root}/online-registration.json',
                 '--tool-profile', '{root}/environment/payload/tool-profile.json',
                 '--output', '{online}', '--wall-limit', '{online_limit}',
                 '--headless', '--enable_cameras', '--experience',
                 '{root}/runtime/third_party/IsaacLab/apps/isaaclab.python.headless.rendering.kit',
                 '--kit_args', _KIT_ARGS],
        'ready_file': '{online}/endpoint.json',
        'startup_timeout_sec': 900,
    },
    'collect': {
        # The original collector had no command line; call its function.
        'argv': ['{python}', '-c',
                 "import sys; sys.path.insert(0, '{root}/environment'); "
                 "from collect_recording import collect; collect('{online}', '{recording}')"],
        'timeout_sec': 7250,
    },
    'verify': {
        'argv': ['{python}', '{root}/tests/payload/verify.py',
                 '--registration', '{root}/replay-registration.json',
                 '--candidate', '{recording}/submission.json', '--reference', '{recording}',
                 '--output', '{verification}', '--python', '{python}'],
        'timeout_sec': 7250,
    },
    'install': {
        'manifest': 'environment/bundle/manifest.json',
        'archive_rewrites': [
            {'path': 'Assets/Robots/x5/curobo.yml',
             'before': '/root/autodl-tmp/LXC/RoboDojo/Assets/Robots/x5',
             'after': '{runtime}/Assets/Robots/x5', 'count': 2,
             'reason': 'Two absolute deployment paths only'},
            {'path': 'third_party/IsaacLab/source/isaaclab/isaaclab/app/app_launcher.py',
             'before': 'launcher_args["active_gpu"] = self.device_id',
             'after': 'launcher_args["active_gpu"] = int(os.environ["ZIWU_GPU_INDEX"])', 'count': 1,
             'reason': 'Map Vulkan to the selected physical GPU; CUDA stays logical zero'},
        ],
        'registration': {'schema': 'robodojo-pen-case-runtime-v1', 'case_id': 'pen-holder-seed0-layout0',
                         'native_action_limit': 1100, 'verifier_timeout_sec': 7200},
        'manifest_fields': ['layout', 'action_limits'],
        'registrations': [{'name': 'online', 'verifier_root': 'environment/runtime_common'},
                          {'name': 'replay', 'verifier_root': 'tests/payload'}],
    },
}

_TOKEN = re.compile(r'\{([a-z_]*)\}')
# Pinned dataset file on the Hugging Face Hub: the revision is a commit hash.
RUNTIME_URL = re.compile(r'https://huggingface\.co/datasets/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+'
                         r'/resolve/[0-9a-f]{40}/[A-Za-z0-9_./-]+')


def _check_text(value, where):
    if not isinstance(value, str) or not value:
        raise ValueError(where + ' must be a non-empty string')
    for name in _TOKEN.findall(value):
        if name not in PLACEHOLDERS:
            raise ValueError(f'{where}: unknown placeholder {{{name}}}')
    if value.startswith('/'):
        raise ValueError(f'{where}: absolute host paths are not allowed; use {{root}}')


def _relative(value, where):
    if not isinstance(value, str) or not value or value.startswith('/') or '..' in Path(value).parts:
        raise ValueError(where + ' must be a relative path inside the deployment')


def validate(service):
    if not isinstance(service, dict) or service.get('schema') != SCHEMA:
        raise ValueError('host/service.json must declare schema ' + SCHEMA)
    _relative(service.get('cwd', '.'), 'cwd')
    paths = service.get('pythonpath', [])
    if not isinstance(paths, list):
        raise ValueError('pythonpath must be a list')
    for i, value in enumerate(paths):
        _relative(value, f'pythonpath[{i}]')
    for phase in PHASES:
        spec = service.get(phase)
        if not isinstance(spec, dict) or not isinstance(spec.get('argv'), list) or not spec['argv']:
            raise ValueError(phase + '.argv must be a non-empty list')
        for i, value in enumerate(spec['argv']):
            _check_text(value, f'{phase}.argv[{i}]')
        for key in ('timeout_sec', 'startup_timeout_sec'):
            if key in spec and (type(spec[key]) is not int or not 0 < spec[key] <= 14400):
                raise ValueError(f'{phase}.{key} must be an integer number of seconds (1..14400)')
    # The host server proxies sessions through {online}/endpoint.json only.
    if service['serve'].get('ready_file') != '{online}/endpoint.json':
        raise ValueError('serve.ready_file must be {online}/endpoint.json')
    install = service.get('install')
    if install is not None:
        validate_install(install)
    return service


def _validate_rewrites(rewrites, prefix):
    if not isinstance(rewrites, list):
        raise ValueError(prefix + ' must be a list')
    for i, rewrite in enumerate(rewrites):
        where = f'{prefix}[{i}]'
        if not isinstance(rewrite, dict):
            raise ValueError(where + ' must be an object')
        _relative(rewrite.get('path', ''), where + '.path')
        if not isinstance(rewrite.get('before'), str) or not rewrite['before']:
            raise ValueError(where + '.before must be a non-empty string')
        if not isinstance(rewrite.get('after'), str):
            raise ValueError(where + '.after must be a string')
        if set(_TOKEN.findall(rewrite['after'])) - INSTALL_PLACEHOLDERS:
            raise ValueError(where + '.after may only use {runtime}')
        if type(rewrite.get('count', 1)) is not int or rewrite.get('count', 1) < 1:
            raise ValueError(where + '.count must be a positive integer')


def validate_install(install):
    _relative(install.get('manifest', ''), 'install.manifest')
    url = install.get('runtime_archive_url')
    if url is not None and (not isinstance(url, str) or not RUNTIME_URL.fullmatch(url) or '..' in url):
        raise ValueError('install.runtime_archive_url must be a huggingface.co dataset file pinned to a commit')
    _validate_rewrites(install.get('archive_rewrites', []), 'install.archive_rewrites')
    reg = install.get('registration', {})
    if not isinstance(reg, dict) or not {'schema', 'case_id', 'native_action_limit', 'verifier_timeout_sec'} <= set(reg):
        raise ValueError('install.registration needs schema, case_id, native_action_limit, verifier_timeout_sec')
    for i, item in enumerate(install.get('registrations', [])):
        if not isinstance(item.get('name'), str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', item['name']):
            raise ValueError(f'install.registrations[{i}].name is invalid')
        _relative(item.get('verifier_root', ''), f'install.registrations[{i}].verifier_root')
    return install


BASES = Path(__file__).resolve().with_name('bases')
BASE_SCHEMA = 'ziwu-runtime-base-v1'
_BASE_ID = re.compile(r'[a-z][a-z0-9-]{0,47}')
_SHA256 = re.compile(r'[0-9a-f]{64}')


def _file_map(files, where):
    if not isinstance(files, dict) or not files:
        raise ValueError(where + ' must be a non-empty object of path -> sha256')
    for name, value in files.items():
        _relative(name, where + ' path ' + repr(name))
        if name.endswith('/') or not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise ValueError(where + ': ' + repr(name) + ' needs a sha256')


def validate_base(record, base_id):
    """A base package record of the platform registry (bases/<id>.json)."""
    if not isinstance(record, dict) or record.get('schema') != BASE_SCHEMA:
        raise ValueError('base record must declare schema ' + BASE_SCHEMA)
    if record.get('id') != base_id:
        raise ValueError('base record id does not match its file name: ' + base_id)
    if not _SHA256.fullmatch(str(record.get('archive_sha256', ''))):
        raise ValueError('base archive_sha256 is not a sha256')
    if type(record.get('archive_bytes')) is not int or record['archive_bytes'] <= 0:
        raise ValueError('base archive_bytes must be a positive integer')
    url = record.get('archive_url')
    if url is not None and (not isinstance(url, str) or not RUNTIME_URL.fullmatch(url) or '..' in url):
        raise ValueError('base archive_url must be a huggingface.co dataset file pinned to a commit, or null')
    _file_map(record.get('files'), 'base files')
    _validate_rewrites(record.get('archive_rewrites', []), 'base archive_rewrites')
    for rewrite in record.get('archive_rewrites', []):
        if rewrite['path'] not in record['files']:
            raise ValueError('base archive_rewrites names a file outside the base: ' + rewrite['path'])
    prefixes = record.get('task_prefixes')
    if not isinstance(prefixes, list) or not prefixes:
        raise ValueError('base task_prefixes must be a non-empty list')
    for i, prefix in enumerate(prefixes):
        _relative(prefix, f'base task_prefixes[{i}]')
        if not prefix.endswith('/'):
            raise ValueError(f'base task_prefixes[{i}] must end with /')
    return record


def load_base(base_id, directory=None):
    if not isinstance(base_id, str) or not _BASE_ID.fullmatch(base_id):
        raise ValueError('Invalid runtime base id')
    path = Path(directory or BASES) / (base_id + '.json')
    if not path.is_file():
        raise ValueError('Unknown runtime base: ' + base_id)
    return validate_base(json.loads(path.read_text()), base_id)


def runtime_base(manifest, directory=None):
    """The base package a runtime manifest builds on, or None for a single-archive runtime.

    The manifest pins the base by id and archive sha256, so a task digest covers the
    exact base it was accepted with; the base's content comes from the registry.
    """
    declared = manifest.get('base')
    if declared is None:
        return None
    if not isinstance(declared, dict) or set(declared) != {'id', 'archive_sha256'}:
        raise ValueError('manifest base must be {"id", "archive_sha256"}')
    base = load_base(declared['id'], directory)
    if base['archive_sha256'] != declared['archive_sha256']:
        raise ValueError('manifest pins runtime base ' + base['id'] + ' at ' + str(declared['archive_sha256'])[:12]
                         + ', the registry holds ' + base['archive_sha256'][:12])
    return base


def check_task_files(base, files):
    """What a task archive may contain on top of a base: new files under task_prefixes only."""
    _file_map(files, 'manifest files')
    prefixes = tuple(base['task_prefixes'])
    for name, value in files.items():
        if name in base['files']:
            if base['files'][name] != value:
                raise ValueError('Task archive replaces a base file: ' + name)
        elif not name.startswith(prefixes):
            raise ValueError('Task archive file outside the base\'s task paths (' + ', '.join(prefixes) + '): ' + name)


def load_task(task):
    """Contract declared by a task tree (before installation)."""
    path = Path(task) / 'host/service.json'
    if not path.is_file():
        return LEGACY_SERVICE
    service = validate(json.loads(path.read_text()))
    if 'install' not in service:
        raise ValueError('host/service.json needs an install section')
    return service


def load(root):
    """The deployment's declared contract, or the legacy bimanual contract."""
    path = Path(root) / 'host/service.json'
    if not path.is_file():
        return LEGACY_SERVICE
    return validate(json.loads(path.read_text()))


def expand(value, values):
    """Substitute placeholders in one string (values maps name -> str)."""
    return _TOKEN.sub(lambda m: values[m.group(1)], value)


def command(service, phase, values):
    return [expand(item, values) for item in service[phase]['argv']]
